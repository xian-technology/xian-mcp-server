"""buy_on_dex/sell_on_dex sign con_dex_helper price bounds derived off-chain."""

from decimal import Decimal

import pytest
from xian_py import Wallet
from xian_runtime_types.decimal import ContractingDecimal

import dex_tools
import xian_server
from tests.shared import TEST_DEX_BASE, TEST_DEX_TOKEN, TEST_PRIVATE_KEY


@pytest.fixture
def market(monkeypatch):
    calls = {"quotes": [], "sent": []}

    async def pair_for_tokens(token_a, token_b):
        return {"pair_id": 7, "token0": token_a, "token1": token_b}

    async def quote(*, kind, token_in, token_out, amount, path, account, max_hops):
        calls["quotes"].append(
            {
                "kind": kind,
                "token_in": token_in,
                "token_out": token_out,
                "amount": amount,
                "path": path,
                "account": account,
                "max_hops": max_hops,
            }
        )
        if kind == "exact_out":
            return {
                "amount_in": ContractingDecimal("2.000000000000000000000000000001"),
                "amount_out": ContractingDecimal(str(amount)),
            }
        return {"amount_in": ContractingDecimal(str(amount)), "amount_out": ContractingDecimal("3")}

    async def precision(token):
        return 8 if token == TEST_DEX_BASE else None

    async def send_transaction(**kwargs):
        calls["sent"].append(kwargs)
        return {"tx_hash": "ABC", "submitted": True}

    monkeypatch.setattr(dex_tools, "_pair_for_tokens", pair_for_tokens)
    monkeypatch.setattr(dex_tools, "_quote", quote)
    monkeypatch.setattr(dex_tools, "_token_precision", precision)
    monkeypatch.setattr(xian_server, "send_transaction", send_transaction)
    return calls


@pytest.mark.asyncio
async def test_buy_signs_max_input_from_quote_and_slippage(market):
    result = await xian_server.buy_on_dex(
        private_key=TEST_PRIVATE_KEY,
        buy_token=TEST_DEX_TOKEN,
        sell_token=TEST_DEX_BASE,
        amount="1.5",
        slippage="1",
    )

    assert result["tx_hash"] == "ABC"
    [quote] = market["quotes"]
    assert quote == {
        "kind": "exact_out",
        "token_in": TEST_DEX_BASE,
        "token_out": TEST_DEX_TOKEN,
        "amount": Decimal("1.5"),
        "path": [7],
        "account": Wallet(TEST_PRIVATE_KEY).public_key,
        "max_hops": 1,
    }
    [sent] = market["sent"]
    assert sent["contract"] == "con_dex_helper" and sent["function"] == "buy"
    kwargs = sent["kwargs"]
    assert set(kwargs) == {"buy_token", "sell_token", "amount", "max_input", "deadline"}
    # 2.000...001 * 1.01 rounded up to the sell token's 8 decimals.
    assert kwargs["max_input"] == ContractingDecimal("2.02000001")
    assert kwargs["amount"] == Decimal("1.5")
    assert result["trade_bounds"] == {
        "quoted_input": "2.000000000000000000000000000001",
        "max_input": "2.02000001",
    }


@pytest.mark.asyncio
async def test_sell_signs_min_out_from_quote_and_slippage(market):
    result = await xian_server.sell_on_dex(
        private_key=TEST_PRIVATE_KEY,
        sell_token=TEST_DEX_TOKEN,
        buy_token=TEST_DEX_BASE,
        amount="4",
        slippage="0.5",
    )

    [quote] = market["quotes"]
    assert (quote["kind"], quote["token_in"], quote["token_out"], quote["path"]) == (
        "exact_in",
        TEST_DEX_TOKEN,
        TEST_DEX_BASE,
        [7],
    )
    kwargs = market["sent"][0]["kwargs"]
    assert set(kwargs) == {"sell_token", "buy_token", "amount", "min_out", "deadline"}
    assert market["sent"][0]["function"] == "sell"
    assert kwargs["min_out"] == ContractingDecimal("2.985")
    assert result["trade_bounds"] == {"quoted_output": "3", "min_out": "2.985"}


@pytest.mark.asyncio
@pytest.mark.parametrize("slippage", ["-1", "100", "150", "abc"])
async def test_trade_bounds_reject_invalid_slippage(market, slippage):
    for tool, tokens in (
        (xian_server.buy_on_dex, {"buy_token": TEST_DEX_TOKEN, "sell_token": TEST_DEX_BASE}),
        (xian_server.sell_on_dex, {"sell_token": TEST_DEX_TOKEN, "buy_token": TEST_DEX_BASE}),
    ):
        result = await tool(private_key=TEST_PRIVATE_KEY, amount="1", slippage=slippage, **tokens)
        assert isinstance(result, str) and result.startswith("❌")
    assert market["sent"] == []


@pytest.mark.asyncio
async def test_trade_refuses_when_pair_is_missing(market, monkeypatch):
    async def no_pair(token_a, token_b):
        return None

    monkeypatch.setattr(dex_tools, "_pair_for_tokens", no_pair)
    result = await xian_server.sell_on_dex(
        private_key=TEST_PRIVATE_KEY, sell_token=TEST_DEX_TOKEN, buy_token=TEST_DEX_BASE, amount="1"
    )
    assert result.startswith("❌") and "pair" in result.lower()
    assert market["sent"] == []


@pytest.mark.asyncio
async def test_sell_refuses_a_zero_min_out(market, monkeypatch):
    async def tiny_quote(**kwargs):
        return {
            "amount_in": ContractingDecimal("1"),
            "amount_out": ContractingDecimal("0.000000001"),
        }

    monkeypatch.setattr(dex_tools, "_quote", tiny_quote)
    result = await xian_server.sell_on_dex(
        private_key=TEST_PRIVATE_KEY, sell_token=TEST_DEX_TOKEN, buy_token=TEST_DEX_BASE, amount="1"
    )
    assert result.startswith("❌") and "min_out" in result
    assert market["sent"] == []
