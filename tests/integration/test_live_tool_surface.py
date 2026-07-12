from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from typing import Any

import pytest
from xian_py import Wallet

from dex_tools import (
    dex_get_pair,
    dex_list_events,
    dex_list_pairs,
    dex_plan_add_liquidity,
    dex_plan_remove_liquidity,
    dex_plan_swap,
    dex_quote_exact_in,
    dex_quote_exact_out,
    dex_submit_add_liquidity,
    dex_submit_remove_liquidity,
    dex_submit_swap,
    dex_wait_live_event,
)
from tests.shared import TEST_MNEMONIC
from xian_server import (
    TOOL_SPECS,
    buy_on_dex,
    create_hd_wallet,
    create_hd_wallet_from_mnemonic,
    create_wallet,
    create_wallet_from_private_key,
    decrypt_message,
    encrypt_message,
    get_balance,
    get_bds_status,
    get_block,
    get_block_by_hash,
    get_contract_source,
    get_developer_rewards,
    get_dex_price,
    get_events_for_tx,
    get_indexed_tx,
    get_state,
    get_state_for_block,
    get_state_for_tx,
    get_state_history,
    get_token_balances,
    get_token_contract_by_symbol,
    get_token_data_by_contract,
    get_transaction,
    list_blocks,
    list_events,
    list_shielded_output_tags,
    list_shielded_wallet_history,
    list_txs_by_contract,
    list_txs_by_sender,
    list_txs_for_block,
    sell_on_dex,
    send_tokens,
    send_transaction,
    sign_message,
    simulate_transaction,
    verify_signature,
)

pytestmark = pytest.mark.integration


def _live_private_key() -> str:
    private_key = os.environ.get("XIAN_MCP_LIVE_PRIVATE_KEY", "").strip()
    if not private_key:
        pytest.skip("Set XIAN_MCP_LIVE_PRIVATE_KEY to run live MCP tool integration tests")
    return private_key


def _assert_success(result: Any, *, tool: str) -> Any:
    if isinstance(result, str) and result.startswith("❌"):
        pytest.fail(f"{tool} failed: {result}")
    return result


def _tx_hash(result: dict[str, Any]) -> str:
    tx_hash = result.get("tx_hash") or result.get("hash")
    if not tx_hash and isinstance(result.get("response"), dict):
        tx_hash = result["response"].get("result", {}).get("hash")
    assert isinstance(tx_hash, str) and tx_hash
    return tx_hash


async def _wait_for_indexed_tx(tx_hash: str) -> dict[str, Any]:
    for _ in range(30):
        result = await get_indexed_tx(tx_hash)
        if isinstance(result, dict) and result.get("tx_hash"):
            return result
        await asyncio.sleep(1)
    pytest.fail(f"Transaction {tx_hash} was not indexed by BDS")


@pytest.mark.asyncio
async def test_live_mcp_tool_surface_against_dev_node() -> None:
    private_key = _live_private_key()
    wallet = Wallet(private_key)
    address = wallet.public_key
    receiver_wallet = Wallet()
    receiver = receiver_wallet.public_key
    dex_token = os.environ.get("XIAN_MCP_LIVE_DEX_TOKEN", "con_dex_demo_token")
    dex_base = os.environ.get("XIAN_MCP_LIVE_DEX_BASE", "currency")
    token_symbol = os.environ.get("XIAN_MCP_LIVE_TOKEN_SYMBOL", "XIAN")
    token_contract = os.environ.get("XIAN_MCP_LIVE_TOKEN_CONTRACT", "currency")
    shielded_tag = os.environ.get("XIAN_MCP_LIVE_SHIELDED_TAG", "live-mcp-smoke")

    expected_tools = {
        "create_wallet",
        "create_wallet_from_private_key",
        "create_hd_wallet",
        "create_hd_wallet_from_mnemonic",
        "get_balance",
        "get_token_balances",
        "get_bds_status",
        "get_developer_rewards",
        "list_blocks",
        "get_block",
        "get_block_by_hash",
        "get_indexed_tx",
        "list_txs_for_block",
        "list_txs_by_sender",
        "list_txs_by_contract",
        "get_events_for_tx",
        "list_events",
        "get_state_history",
        "get_state_for_tx",
        "get_state_for_block",
        "list_shielded_output_tags",
        "list_shielded_wallet_history",
        "send_transaction",
        "send_tokens",
        "get_transaction",
        "simulate_transaction",
        "get_state",
        "get_contract_source",
        "get_token_contract_by_symbol",
        "get_token_data_by_contract",
        "buy_on_dex",
        "sell_on_dex",
        "get_dex_price",
        "dex_list_pairs",
        "dex_get_pair",
        "dex_quote_exact_in",
        "dex_quote_exact_out",
        "dex_plan_swap",
        "dex_submit_swap",
        "dex_plan_add_liquidity",
        "dex_submit_add_liquidity",
        "dex_plan_remove_liquidity",
        "dex_submit_remove_liquidity",
        "dex_wait_live_event",
        "dex_list_events",
        "sign_message",
        "verify_signature",
        "encrypt_message",
        "decrypt_message",
    }
    assert {spec["name"] for spec in TOOL_SPECS} == expected_tools

    created_wallet = _assert_success(await create_wallet(), tool="create_wallet")
    assert len(created_wallet["private_key"]) == 64
    imported_wallet = _assert_success(
        await create_wallet_from_private_key(private_key),
        tool="create_wallet_from_private_key",
    )
    assert imported_wallet["public_key"] == address
    hd_wallet = _assert_success(await create_hd_wallet(), tool="create_hd_wallet")
    assert hd_wallet["xian_public_key"]
    restored_hd_wallet = _assert_success(
        await create_hd_wallet_from_mnemonic(TEST_MNEMONIC),
        tool="create_hd_wallet_from_mnemonic",
    )
    assert restored_hd_wallet["mnemonic"] == TEST_MNEMONIC

    status = _assert_success(await get_bds_status(), tool="get_bds_status")
    assert status.get("available", True) is True
    assert status["worker_running"] is True

    balance = _assert_success(await get_balance(address, "currency"), tool="get_balance")
    assert balance["address"] == address
    token_balances = _assert_success(
        await get_token_balances(address, limit=20, include_zero=True),
        tool="get_token_balances",
    )
    assert token_balances["available"] is True
    assert isinstance(token_balances["items"], list)
    state = _assert_success(
        await get_state(f"currency.balances:{address}"),
        tool="get_state",
    )
    assert state["state_key"] == f"currency.balances:{address}"
    source = _assert_success(await get_contract_source("currency"), tool="get_contract_source")
    assert source["source"]

    simulation = _assert_success(
        await simulate_transaction(
            address=address,
            contract="currency",
            function="balance_of",
            kwargs={"address": address},
        ),
        tool="simulate_transaction",
    )
    assert isinstance(simulation, dict)

    send_result = _assert_success(
        await send_tokens(
            private_key=private_key,
            to_address=receiver,
            token_contract="currency",
            amount=Decimal("0.0001"),
        ),
        tool="send_tokens",
    )
    send_hash = _tx_hash(send_result)
    indexed_tx = await _wait_for_indexed_tx(send_hash)
    block_height = indexed_tx["block_height"]

    tx = _assert_success(await get_transaction(send_hash), tool="get_transaction")
    assert isinstance(tx, dict)
    _assert_success(await get_indexed_tx(send_hash), tool="get_indexed_tx")
    events_for_tx = _assert_success(await get_events_for_tx(send_hash), tool="get_events_for_tx")
    assert isinstance(events_for_tx, list)
    state_for_tx = _assert_success(await get_state_for_tx(send_hash), tool="get_state_for_tx")
    assert isinstance(state_for_tx, list)
    state_for_block = _assert_success(
        await get_state_for_block(block_height),
        tool="get_state_for_block",
    )
    assert isinstance(state_for_block, list)

    blocks = _assert_success(await list_blocks(limit=2), tool="list_blocks")
    assert blocks
    block = _assert_success(await get_block(block_height), tool="get_block")
    assert block["height"] == block_height
    block_by_hash = _assert_success(
        await get_block_by_hash(block["block_hash"]),
        tool="get_block_by_hash",
    )
    assert block_by_hash["height"] == block_height
    txs_for_block = _assert_success(
        await list_txs_for_block(block_height),
        tool="list_txs_for_block",
    )
    assert any(item["tx_hash"] == send_hash for item in txs_for_block)
    txs_by_sender = _assert_success(
        await list_txs_by_sender(address, limit=20),
        tool="list_txs_by_sender",
    )
    assert isinstance(txs_by_sender, list)
    txs_by_contract = _assert_success(
        await list_txs_by_contract("currency", limit=20),
        tool="list_txs_by_contract",
    )
    assert isinstance(txs_by_contract, list)
    transfer_events = _assert_success(
        await list_events("currency", "Transfer", limit=20),
        tool="list_events",
    )
    assert isinstance(transfer_events, list)
    history = _assert_success(
        await get_state_history(f"currency.balances:{address}", limit=20),
        tool="get_state_history",
    )
    assert isinstance(history, list)
    rewards = _assert_success(await get_developer_rewards(address), tool="get_developer_rewards")
    assert rewards["recipient_key"] == address

    token_contracts = _assert_success(
        await get_token_contract_by_symbol(token_symbol),
        tool="get_token_contract_by_symbol",
    )
    assert token_contracts["count"] >= 1
    token_data = _assert_success(
        await get_token_data_by_contract(token_contract),
        tool="get_token_data_by_contract",
    )
    assert token_data["tokenStates"]["nodes"]

    dex_price = _assert_success(await get_dex_price(dex_token, dex_base), tool="get_dex_price")
    if "error" not in dex_price:
        pairs = _assert_success(await dex_list_pairs(limit=100), tool="dex_list_pairs")
        assert any(pair["pair_id"] == dex_price["pair_id"] for pair in pairs["items"])
        pair = _assert_success(
            await dex_get_pair(token_a=dex_base, token_b=dex_token),
            tool="dex_get_pair",
        )
        assert pair["pair"]["pair_id"] == dex_price["pair_id"]
        exact_in = _assert_success(
            await dex_quote_exact_in(
                dex_base,
                dex_token,
                0.001,
                account=address,
            ),
            tool="dex_quote_exact_in",
        )
        assert exact_in["amount_out"] > 0
        exact_out = _assert_success(
            await dex_quote_exact_out(
                dex_base,
                dex_token,
                exact_in["amount_out"] / 2,
                account=address,
            ),
            tool="dex_quote_exact_out",
        )
        assert exact_out["amount_in"] > 0
        swap_plan = _assert_success(
            await dex_plan_swap(
                dex_base,
                dex_token,
                0.001,
                address,
                address,
            ),
            tool="dex_plan_swap",
        )
        swap_result = _assert_success(
            await dex_submit_swap(private_key, swap_plan["plan_id"]),
            tool="dex_submit_swap",
        )
        await _wait_for_indexed_tx(_tx_hash(swap_result["final_transaction"]))
        replay = await dex_submit_swap(private_key, swap_plan["plan_id"])
        assert replay["ok"] is False
        tampered = await dex_submit_swap(private_key, f"{swap_plan['plan_id']}x")
        assert tampered["ok"] is False

        approve_result = _assert_success(
            await send_transaction(
                private_key=private_key,
                contract=dex_base,
                function="approve",
                kwargs={"to": "con_dex_helper", "amount": Decimal("0.02")},
            ),
            tool="send_transaction",
        )
        await _wait_for_indexed_tx(_tx_hash(approve_result))
        live_swap_task = asyncio.create_task(
            dex_wait_live_event(
                contract="con_pairs",
                event="Swap",
                timeout_seconds=30,
                signer=address,
            )
        )
        # Give the CometBFT subscription time to acknowledge before broadcasting
        # the swap that this bounded live wait is intended to observe.
        await asyncio.sleep(0.25)
        buy_result = _assert_success(
            await buy_on_dex(
                private_key=private_key,
                buy_token=dex_token,
                sell_token=dex_base,
                amount=0.01,
                slippage=1.0,
            ),
            tool="buy_on_dex",
        )
        live_swap = _assert_success(await live_swap_task, tool="dex_wait_live_event")
        assert live_swap["bds_required"] is False
        assert live_swap["timed_out"] is False
        assert live_swap["items"][0]["tx_hash"].upper() == _tx_hash(buy_result).upper()
        await _wait_for_indexed_tx(_tx_hash(buy_result))
        sell_result = _assert_success(
            await sell_on_dex(
                private_key=private_key,
                sell_token=dex_token,
                buy_token=dex_base,
                amount=0.001,
                slippage=1.0,
            ),
            tool="sell_on_dex",
        )
        await _wait_for_indexed_tx(_tx_hash(sell_result))

        dex_events = _assert_success(
            await dex_list_events(contract="con_pairs", event="Swap", limit=20),
            tool="dex_list_events",
        )
        assert isinstance(dex_events["items"], list)

        if os.environ.get("XIAN_MCP_LIVE_DEX_LIQUIDITY", "").strip() == "1":
            add_plan = _assert_success(
                await dex_plan_add_liquidity(
                    dex_base,
                    dex_token,
                    0.001,
                    0.001,
                    address,
                    address,
                ),
                tool="dex_plan_add_liquidity",
            )
            add_result = _assert_success(
                await dex_submit_add_liquidity(private_key, add_plan["plan_id"]),
                tool="dex_submit_add_liquidity",
            )
            await _wait_for_indexed_tx(_tx_hash(add_result["final_transaction"]))
            lp_balance = _assert_success(
                await get_balance(address, add_plan["lp_token"]),
                tool="get_balance(lp_token)",
            )["balance"]
            if Decimal(str(lp_balance)) > 0:
                remove_plan = _assert_success(
                    await dex_plan_remove_liquidity(
                        dex_base,
                        dex_token,
                        Decimal(str(lp_balance)) / 10,
                        address,
                        address,
                    ),
                    tool="dex_plan_remove_liquidity",
                )
                remove_result = _assert_success(
                    await dex_submit_remove_liquidity(private_key, remove_plan["plan_id"]),
                    tool="dex_submit_remove_liquidity",
                )
                await _wait_for_indexed_tx(_tx_hash(remove_result["final_transaction"]))

    output_tags = _assert_success(
        await list_shielded_output_tags(shielded_tag, limit=5),
        tool="list_shielded_output_tags",
    )
    assert isinstance(output_tags, list)
    wallet_history = _assert_success(
        await list_shielded_wallet_history(shielded_tag, limit=5),
        tool="list_shielded_wallet_history",
    )
    assert isinstance(wallet_history, list)

    message = "live MCP smoke"
    signature = _assert_success(await sign_message(private_key, message), tool="sign_message")
    assert (
        _assert_success(
            await verify_signature(address, message, signature["signature"]),
            tool="verify_signature",
        )
        is True
    )
    encrypted = _assert_success(
        await encrypt_message(private_key, receiver, message),
        tool="encrypt_message",
    )
    decrypted = _assert_success(
        await decrypt_message(receiver_wallet.private_key, address, encrypted["encrypted_message"]),
        tool="decrypt_message",
    )
    assert decrypted["decrypted_message"] == message
