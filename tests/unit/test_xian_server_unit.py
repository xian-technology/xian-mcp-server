#!/usr/bin/env python3

import os

import pytest

import xian_server
from serialization import normalize_for_transport
from tests.shared import (
    TEST_DEX_BASE,
    TEST_DEX_TOKEN,
    TEST_MESSAGE,
    TEST_MNEMONIC,
    TEST_PRIVATE_KEY,
    TEST_RECEIVER_PRIVATE_KEY,
    TEST_RECEIVER_PUBLIC_KEY,
    TEST_SENDER_PRIVATE_KEY,
    TEST_SENDER_PUBLIC_KEY,
    TEST_TOKEN_CONTRACT,
    TEST_TOKEN_SYMBOL,
)
from xian_py.models import (
    TokenBalancePage,
    TransactionReceipt,
    TransactionSubmission,
)
from xian_server import (
    buy_on_dex,
    create_hd_wallet,
    create_hd_wallet_from_mnemonic,
    create_wallet,
    create_wallet_from_private_key,
    decrypt_message,
    encrypt_message,
    format_success_response,
    get_balance,
    get_contract,
    get_state,
    get_token_balances,
    get_token_contract_by_symbol,
    get_token_data_by_contract,
    send_tokens,
    send_transaction,
    sell_on_dex,
    sign_message,
    verify_signature,
)


class TestWalletCreation:
    @pytest.mark.asyncio
    async def test_create_wallet(self):
        result = await create_wallet()

        assert isinstance(result, dict)
        assert "public_key" in result
        assert "private_key" in result
        assert len(result["private_key"]) == 64
        assert len(result["public_key"]) == 64

    @pytest.mark.asyncio
    async def test_create_wallet_from_private_key(self):
        result = await create_wallet_from_private_key(TEST_PRIVATE_KEY)

        assert isinstance(result, dict)
        assert result["private_key"] == TEST_PRIVATE_KEY

    @pytest.mark.asyncio
    async def test_create_wallet_from_invalid_private_key(self):
        result = await create_wallet_from_private_key("invalid_key")

        assert isinstance(result, str)
        assert result.startswith("❌")

    @pytest.mark.asyncio
    async def test_create_hd_wallet(self):
        result = await create_hd_wallet()

        assert isinstance(result, dict)
        assert "mnemonic" in result
        assert "xian_public_key" in result
        assert "xian_private_key" in result
        assert "eth_public_key" in result
        assert "eth_private_key" in result
        assert len(result["mnemonic"].split()) in [12, 24]

    @pytest.mark.asyncio
    async def test_create_hd_wallet_from_mnemonic(self):
        result = await create_hd_wallet_from_mnemonic(TEST_MNEMONIC)

        assert isinstance(result, dict)
        assert result["mnemonic"] == TEST_MNEMONIC


class TestInputValidation:
    @pytest.mark.asyncio
    async def test_get_balance_missing_address(self):
        result = await get_balance("")

        assert isinstance(result, str)
        assert result.startswith("❌")

    @pytest.mark.asyncio
    async def test_get_token_balances_missing_address(self):
        result = await get_token_balances("")

        assert result == "❌ Error: Address is required"

    @pytest.mark.asyncio
    async def test_send_transaction_missing_params(self):
        result = await send_transaction()

        assert isinstance(result, str)
        assert result.startswith("❌")

    @pytest.mark.asyncio
    async def test_send_tokens_missing_params(self):
        result = await send_tokens()

        assert isinstance(result, str)
        assert result.startswith("❌")

    @pytest.mark.asyncio
    async def test_encrypt_decrypt_missing_params(self):
        result = await encrypt_message()

        assert isinstance(result, str)
        assert result.startswith("❌")

    @pytest.mark.asyncio
    async def test_get_token_contract_by_symbol_empty(self):
        result = await get_token_contract_by_symbol("")

        assert isinstance(result, str)
        assert result.startswith("❌")

    @pytest.mark.asyncio
    async def test_get_token_data_by_contract_empty(self):
        result = await get_token_data_by_contract("")

        assert isinstance(result, str)
        assert result.startswith("❌")

    @pytest.mark.asyncio
    async def test_buy_on_dex_missing_params(self):
        result = await buy_on_dex()

        assert isinstance(result, str)
        assert result.startswith("❌")

    @pytest.mark.asyncio
    async def test_buy_on_dex_invalid_amount(self):
        result = await buy_on_dex(
            private_key=TEST_PRIVATE_KEY,
            buy_token=TEST_DEX_TOKEN,
            sell_token=TEST_DEX_BASE,
            amount=0,
        )

        assert result == "❌ Error: Amount must be positive"

    @pytest.mark.asyncio
    async def test_sell_on_dex_missing_params(self):
        result = await sell_on_dex()

        assert isinstance(result, str)
        assert result.startswith("❌")

    @pytest.mark.asyncio
    async def test_sell_on_dex_invalid_amount(self):
        result = await sell_on_dex(
            private_key=TEST_PRIVATE_KEY,
            sell_token=TEST_DEX_TOKEN,
            buy_token=TEST_DEX_BASE,
            amount=-1,
        )

        assert result == "❌ Error: Amount must be positive"


class TestCryptography:
    @pytest.mark.asyncio
    async def test_sign_message(self):
        result = await sign_message(TEST_PRIVATE_KEY, TEST_MESSAGE)

        assert isinstance(result, dict)
        assert "signature" in result
        assert len(result["signature"]) > 0

    @pytest.mark.asyncio
    async def test_verify_signature(self):
        signature = (await sign_message(TEST_PRIVATE_KEY, TEST_MESSAGE))["signature"]
        result = await verify_signature(
            TEST_SENDER_PUBLIC_KEY,
            TEST_MESSAGE,
            signature,
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_verify_invalid_signature(self):
        result = await verify_signature(
            TEST_SENDER_PUBLIC_KEY,
            TEST_MESSAGE,
            "invalid_signature",
        )

        if isinstance(result, str):
            assert result.startswith("❌")
        else:
            assert result is False

    @pytest.mark.asyncio
    async def test_encrypt_message(self):
        result = await encrypt_message(
            TEST_SENDER_PRIVATE_KEY,
            TEST_RECEIVER_PUBLIC_KEY,
            TEST_MESSAGE,
        )

        assert isinstance(result, dict)
        assert "encrypted_message" in result
        assert "sender_public_key" in result
        assert "receiver_public_key" in result

    @pytest.mark.asyncio
    async def test_decrypt_message(self):
        encrypted_msg = (
            await encrypt_message(
                TEST_SENDER_PRIVATE_KEY,
                TEST_RECEIVER_PUBLIC_KEY,
                TEST_MESSAGE,
            )
        )["encrypted_message"]

        result = await decrypt_message(
            TEST_RECEIVER_PRIVATE_KEY,
            TEST_SENDER_PUBLIC_KEY,
            encrypted_msg,
        )

        assert isinstance(result, dict)
        assert result["decrypted_message"] == TEST_MESSAGE


class TestCompatibilityRegressions:
    @pytest.mark.asyncio
    async def test_get_contract_uses_current_sdk_signature(self, monkeypatch):
        class FakeXianAsync:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get_contract(self, contract_name):
                return f"source for {contract_name}"

        monkeypatch.setattr(xian_server, "XianAsync", FakeXianAsync)

        result = await get_contract("currency")

        assert result == {
            "contract_name": "currency",
            "source": "source for currency",
        }

    @pytest.mark.asyncio
    async def test_get_state_preserves_requested_key(self, monkeypatch):
        class FakeXianAsync:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get_state(self, contract, variable, *keys):
                assert contract == "currency"
                assert variable == "balances"
                assert keys == ("abc123",)
                return 42

        monkeypatch.setattr(xian_server, "XianAsync", FakeXianAsync)

        state_key = "currency.balances:abc123"
        result = await get_state(state_key)

        assert result == {"state_key": state_key, "state_value": 42}

    @pytest.mark.asyncio
    async def test_send_transaction_normalizes_submission_objects(self, monkeypatch):
        class FakeWallet:
            def __init__(self, private_key):
                self.private_key = private_key
                self.public_key = "sender"

        class FakeXianAsync:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def send_tx(self, contract, function, kwargs):
                assert contract == "currency"
                assert function == "transfer"
                assert kwargs == {"to": "receiver", "amount": 1}
                return TransactionSubmission.from_dict(
                    {
                        "submitted": True,
                        "accepted": True,
                        "finalized": False,
                        "tx_hash": "abc",
                        "mode": "checktx",
                        "nonce": 7,
                        "stamps_supplied": 123,
                        "stamps_estimated": 120,
                        "message": None,
                        "response": {"result": {"hash": "abc"}},
                    }
                )

        monkeypatch.setattr(xian_server, "Wallet", FakeWallet)
        monkeypatch.setattr(xian_server, "XianAsync", FakeXianAsync)

        result = await send_transaction(
            private_key="1" * 64,
            contract="currency",
            function="transfer",
            kwargs={"to": "receiver", "amount": 1},
        )

        assert isinstance(result, dict)
        assert result["submitted"] is True
        assert result["tx_hash"] == "abc"
        assert result["response"] == {"result": {"hash": "abc"}}

    @pytest.mark.asyncio
    async def test_get_token_balances_normalizes_sdk_page(self, monkeypatch):
        class FakeXianAsync:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get_token_balances(
                self,
                address,
                *,
                limit,
                offset,
                include_zero,
            ):
                assert address == "wallet123"
                assert limit == 50
                assert offset == 10
                assert include_zero is True
                return TokenBalancePage.from_dict(
                    {
                        "available": True,
                        "address": address,
                        "total": 1,
                        "limit": limit,
                        "offset": offset,
                        "items": [
                            {
                                "contract": "currency",
                                "balance": "123.45",
                                "name": "Xian",
                                "symbol": "XIAN",
                            }
                        ],
                    }
                )

        monkeypatch.setattr(xian_server, "XianAsync", FakeXianAsync)

        result = await get_token_balances(
            address="wallet123",
            limit=50,
            offset=10,
            include_zero=True,
        )

        assert result["available"] is True
        assert result["address"] == "wallet123"
        assert result["items"][0]["contract"] == "currency"
        assert result["items"][0]["balance"] == "123.45"

    @pytest.mark.asyncio
    async def test_get_token_balances_rejects_invalid_pagination(self):
        result = await get_token_balances(address="wallet123", limit=0)
        assert result == "❌ Error: Limit must be positive"

        result = await get_token_balances(address="wallet123", offset=-1)
        assert result == "❌ Error: Offset must be zero or greater"

    @pytest.mark.asyncio
    async def test_send_tokens_defaults_to_currency(self, monkeypatch):
        class FakeWallet:
            def __init__(self, private_key):
                self.private_key = private_key
                self.public_key = "sender"

        class FakeXianAsync:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def send(self, amount, to_address, token):
                assert amount == 5
                assert to_address == "receiver"
                assert token == "currency"
                return TransactionSubmission.from_dict(
                    {
                        "submitted": True,
                        "accepted": True,
                        "finalized": False,
                        "tx_hash": "def",
                        "mode": "checktx",
                        "nonce": 8,
                        "stamps_supplied": 55,
                        "stamps_estimated": 50,
                        "message": None,
                        "response": {"result": {"hash": "def"}},
                    }
                )

        monkeypatch.setattr(xian_server, "Wallet", FakeWallet)
        monkeypatch.setattr(xian_server, "XianAsync", FakeXianAsync)

        result = await send_tokens(
            private_key="1" * 64,
            to_address="receiver",
            amount=5,
        )

        assert isinstance(result, dict)
        assert result["tx_hash"] == "def"

    @pytest.mark.asyncio
    async def test_buy_and_sell_defaults_match_tool_schema(self, monkeypatch):
        calls = []

        async def fake_send_transaction(**kwargs):
            calls.append(kwargs)
            return {"tx_hash": "ok"}

        monkeypatch.setattr(xian_server, "send_transaction", fake_send_transaction)

        buy_result = await buy_on_dex(
            private_key="1" * 64,
            buy_token="con_token",
            amount=1,
        )
        sell_result = await sell_on_dex(
            private_key="1" * 64,
            sell_token="con_token",
            amount=1,
        )

        assert buy_result == {"tx_hash": "ok"}
        assert sell_result == {"tx_hash": "ok"}
        assert calls[0]["kwargs"]["sell_token"] == "currency"
        assert calls[1]["kwargs"]["buy_token"] == "currency"


class TestTransportSerialization:
    def test_format_success_response_strips_raw_payloads(self):
        result = format_success_response(
            TransactionSubmission.from_dict(
                {
                    "submitted": True,
                    "accepted": True,
                    "finalized": False,
                    "tx_hash": "abc",
                    "mode": "checktx",
                    "nonce": 1,
                    "stamps_supplied": 2,
                    "stamps_estimated": 1,
                    "message": None,
                    "response": {"result": {"hash": "abc"}},
                    "receipt": TransactionReceipt.from_lookup(
                        {
                            "success": True,
                            "result": {"hash": "abc"},
                            "transaction": {"payload": {"contract": "currency"}},
                        }
                    ),
                }
            )
        )

        assert '"submitted": true' in result[0].text.lower()
        assert '"receipt"' in result[0].text
        assert '"raw"' not in result[0].text

    def test_normalize_for_transport_can_include_raw_payloads(self, monkeypatch):
        monkeypatch.setenv("XIAN_INCLUDE_RAW", "true")

        normalized = normalize_for_transport(
            {"value": 1, "raw": {"source": "sdk"}},
        )

        assert normalized == {"value": 1, "raw": {"source": "sdk"}}

    def test_normalize_for_transport_drops_raw_by_default(self, monkeypatch):
        monkeypatch.delenv("XIAN_INCLUDE_RAW", raising=False)

        normalized = normalize_for_transport(
            {"value": 1, "raw": {"source": "sdk"}},
        )

        assert normalized == {"value": 1}
