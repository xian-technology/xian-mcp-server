#!/usr/bin/env python3

import pytest

from tests.shared import (
    TEST_ADDRESS,
    TEST_CONTRACT,
    TEST_DEX_BASE,
    TEST_DEX_TOKEN,
    TEST_STATE_KEY,
    TEST_TOKEN_CONTRACT,
    TEST_TOKEN_SYMBOL,
    TEST_TX_HASH,
)
from xian_server import (
    get_balance,
    get_contract,
    get_dex_price,
    get_state,
    get_token_balances,
    get_token_contract_by_symbol,
    get_token_data_by_contract,
    get_transaction,
    simulate_transaction,
)

pytestmark = pytest.mark.integration


class TestBalanceAndState:
    @pytest.mark.asyncio
    async def test_get_balance(self):
        result = await get_balance(TEST_ADDRESS, "currency")

        if isinstance(result, str) and result.startswith("❌"):
            pytest.skip("TEST_ADDRESS not set or network error")

        assert isinstance(result, dict)
        assert "address" in result
        assert "token_contract" in result
        assert "balance" in result
        assert isinstance(result["balance"], (int, float))

    @pytest.mark.asyncio
    async def test_get_token_balances(self):
        result = await get_token_balances(TEST_ADDRESS, limit=5)

        if isinstance(result, str) and result.startswith("❌"):
            pytest.skip("TEST_ADDRESS not set or token-balance endpoint unavailable")

        assert isinstance(result, dict)
        assert "available" in result
        assert "address" in result
        assert "items" in result
        assert isinstance(result["items"], list)

    @pytest.mark.asyncio
    async def test_get_state(self):
        result = await get_state(TEST_STATE_KEY)

        if isinstance(result, str) and result.startswith("❌"):
            pytest.skip("TEST_STATE_KEY not set or network error")

        assert isinstance(result, dict)
        assert "state_key" in result
        assert "state_value" in result

    @pytest.mark.asyncio
    async def test_get_contract(self):
        result = await get_contract(TEST_CONTRACT)

        if isinstance(result, str) and result.startswith("❌"):
            pytest.skip("Network error or contract not found")

        assert isinstance(result, dict)
        assert "contract_name" in result
        assert "source" in result
        assert len(result["source"]) > 0


class TestTransactions:
    @pytest.mark.asyncio
    async def test_simulate_transaction(self):
        result = await simulate_transaction(
            address=TEST_ADDRESS,
            contract="currency",
            function="balance_of",
            kwargs={"address": TEST_ADDRESS},
        )

        if isinstance(result, str) and result.startswith("❌"):
            pytest.skip("TEST_ADDRESS not set or network error")

        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_get_transaction(self):
        result = await get_transaction(TEST_TX_HASH)

        if isinstance(result, str) and result.startswith("❌"):
            pytest.skip("TEST_TX_HASH not set or network error")

        assert isinstance(result, dict)


class TestTokens:
    @pytest.mark.asyncio
    async def test_get_token_contract_by_symbol(self):
        result = await get_token_contract_by_symbol(TEST_TOKEN_SYMBOL)

        if isinstance(result, str) and result.startswith("❌"):
            pytest.skip("Network error or token symbol not found")

        assert isinstance(result, dict)
        assert "token_contracts" in result
        assert "count" in result
        assert isinstance(result["token_contracts"], list)
        assert isinstance(result["count"], int)

    @pytest.mark.asyncio
    async def test_get_token_contract_by_symbol_nonexistent(self):
        result = await get_token_contract_by_symbol("NONEXISTENT_TOKEN_XYZ_123")

        if isinstance(result, str) and result.startswith("❌"):
            pytest.skip("Network error")

        assert isinstance(result, dict)
        assert result["count"] == 0
        assert "message" in result

    @pytest.mark.asyncio
    async def test_get_token_data_by_contract(self):
        result = await get_token_data_by_contract(TEST_TOKEN_CONTRACT)

        if isinstance(result, str) and result.startswith("❌"):
            pytest.skip("Network error or token contract not found")

        assert isinstance(result, dict)


class TestDEX:
    @pytest.mark.asyncio
    async def test_get_dex_price(self):
        result = await get_dex_price(TEST_DEX_TOKEN, TEST_DEX_BASE)

        if isinstance(result, str) and result.startswith("❌"):
            pytest.skip("Network error or DEX pair not found")

        assert isinstance(result, dict)

        if "error" in result:
            assert "token" in result
            assert "base" in result
        else:
            assert "token" in result
            assert "base" in result
            assert "price" in result
            assert "pair_id" in result
            assert isinstance(result["price"], (int, float))
