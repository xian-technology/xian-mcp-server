#!/usr/bin/env python3


from datetime import UTC, datetime
from decimal import Decimal

import pytest
from xian_py.models import (
    TokenBalancePage,
    TransactionReceipt,
    TransactionSubmission,
)

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


class TestTokenGraphQL:
    @pytest.mark.asyncio
    async def test_get_token_contract_by_symbol_uses_metadata_state(
        self, monkeypatch
    ):
        async def fake_fetch_graphql(query, **_kwargs):
            assert ".metadata:token_symbol" in query
            assert "allContracts" not in query
            assert "xsc0001" not in query
            return {
                "tokenSymbols": {
                    "nodes": [
                        {
                            "key": "currency.metadata:token_symbol",
                            "value": "XIAN",
                        }
                    ]
                }
            }

        monkeypatch.setattr(xian_server, "fetch_graphql", fake_fetch_graphql)

        result = await get_token_contract_by_symbol("xian")

        assert result == {"token_contracts": ["currency"], "count": 1}

    @pytest.mark.asyncio
    async def test_get_token_data_by_contract_uses_current_state_field(
        self, monkeypatch
    ):
        async def fake_fetch_graphql(query, variables=None, **_kwargs):
            assert "updatedAt" in query
            assert "updated\n" not in query
            assert variables["symbol"] == "currency.metadata:token_symbol"
            return {
                "tokenStates": {
                    "nodes": [
                        {
                            "key": "currency.metadata:token_symbol",
                            "value": "XIAN",
                            "updatedAt": "2026-05-21T14:00:00+00:00",
                        }
                    ]
                }
            }

        monkeypatch.setattr(xian_server, "fetch_graphql", fake_fetch_graphql)

        result = await get_token_data_by_contract("currency")

        assert result["tokenStates"]["nodes"][0]["updatedAt"].startswith("2026-")

    @pytest.mark.asyncio
    async def test_graphql_token_balances_decode_fixed_values(self, monkeypatch):
        async def fake_fetch_graphql(query, variables=None, **_kwargs):
            assert "updatedAt" in query
            assert variables == {"suffix": ".balances:wallet123"}
            return {
                "allStates": {
                    "nodes": [
                        {
                            "key": "currency.balances:wallet123",
                            "value": {"__fixed__": "12.5"},
                            "updatedAt": "2026-05-21T14:00:00+00:00",
                        },
                        {
                            "key": "con_zero.balances:wallet123",
                            "value": {"__fixed__": "0"},
                            "updatedAt": "2026-05-21T14:00:01+00:00",
                        },
                    ]
                }
            }

        monkeypatch.setattr(xian_server, "fetch_graphql", fake_fetch_graphql)

        result = await xian_server._graphql_get_token_balances(
            "wallet123",
            limit=10,
            offset=0,
            include_zero=False,
        )

        assert result["total"] == 1
        assert result["items"][0]["contract"] == "currency"
        assert result["items"][0]["balance"] == "12.5"


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
    async def test_get_contract_source_uses_current_sdk_signature(self, monkeypatch):
        class FakeXianAsync:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get_contract_source(self, contract_name):
                return f"source for {contract_name}"

        monkeypatch.setattr(xian_server, "XianAsync", FakeXianAsync)

        result = await get_contract_source("currency")

        assert result == {
            "contract_name": "currency",
            "source": "source for currency",
        }

    @pytest.mark.asyncio
    async def test_get_contract_source_falls_back_to_graphql(self, monkeypatch):
        class FakeXianAsync:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get_contract_source(self, contract_name):
                assert contract_name == "currency"
                return None

        async def fake_graphql_source(contract_name):
            assert contract_name == "currency"
            return "indexed source"

        monkeypatch.setattr(xian_server, "XianAsync", FakeXianAsync)
        monkeypatch.setattr(
            xian_server,
            "_graphql_get_contract_source",
            fake_graphql_source,
        )

        result = await get_contract_source("currency")

        assert result == {
            "contract_name": "currency",
            "source": "indexed source",
        }

    @pytest.mark.asyncio
    async def test_graphql_contract_source_uses_current_schema(self, monkeypatch):
        async def fake_fetch_graphql(query, variables=None, **_kwargs):
            assert "source" in query
            assert "code" not in query
            assert variables == {"name": "currency"}
            return {"contractByName": {"source": "contract source"}}

        monkeypatch.setattr(xian_server, "fetch_graphql", fake_fetch_graphql)

        assert await xian_server._graphql_get_contract_source("currency") == (
            "contract source"
        )

    @pytest.mark.asyncio
    async def test_graphql_state_changes_use_current_schema(self, monkeypatch):
        async def fake_fetch_graphql(query, variables=None, **_kwargs):
            assert "newValue" in query
            assert "createdAt" in query
            assert "CHANGE_ID_DESC" in query
            assert "CREATED_DESC" not in query
            assert variables == {
                "condition": {"key": "currency.balances:alice"},
                "limit": 1,
                "offset": 0,
            }
            return {
                "allStateChanges": {
                    "nodes": [
                        {
                            "key": "currency.balances:alice",
                            "newValue": "5",
                            "txHash": "tx-1",
                            "createdAt": "2026-05-21T14:00:00+00:00",
                            "transactionByTxHash": {"blockHeight": "100"},
                        }
                    ]
                }
            }

        monkeypatch.setattr(xian_server, "fetch_graphql", fake_fetch_graphql)

        result = await xian_server._graphql_get_state_changes(
            condition={"key": "currency.balances:alice"},
            limit=1,
        )

        assert result == [
            {
                "key": "currency.balances:alice",
                "value": "5",
                "tx_hash": "tx-1",
                "block_height": 100,
                "created": "2026-05-21T14:00:00+00:00",
            }
        ]

    @pytest.mark.asyncio
    async def test_graphql_developer_rewards_use_current_schema(self, monkeypatch):
        async def fake_fetch_graphql(query, variables=None, **_kwargs):
            assert "recipientKey" in query
            assert "ROW_ID_ASC" in query
            assert "createdAt" in query
            assert "CREATED_ASC" not in query
            assert variables == {"key": "dev-key"}
            return {
                "allRewards": {
                    "nodes": [
                        {
                            "txHash": "tx-1",
                            "value": "2.5",
                            "createdAt": "2026-05-21T14:00:00+00:00",
                            "transactionByTxHash": {
                                "blockHeight": "100",
                                "contract": "currency",
                            },
                        }
                    ]
                }
            }

        monkeypatch.setattr(xian_server, "fetch_graphql", fake_fetch_graphql)

        result = await xian_server._graphql_get_developer_rewards("dev-key")

        assert result["total_rewards"] == "2.5"
        assert result["first_reward_at"] == "2026-05-21T14:00:00+00:00"
        assert result["last_block_height"] == 100

    @pytest.mark.asyncio
    async def test_block_reads_fall_back_to_rpc_when_bds_returns_none(
        self,
        monkeypatch,
    ):
        async def fake_indexed_read(method_name, *args, **kwargs):
            assert method_name in {"get_block", "get_block_by_hash"}
            return None

        async def fake_get_rpc_block(height):
            assert height == 100
            return {"height": height, "block_hash": "abc"}

        async def fake_get_rpc_block_by_hash(block_hash):
            assert block_hash == "abc"
            return {"height": 100, "block_hash": block_hash}

        monkeypatch.setattr(xian_server, "_call_indexed_read", fake_indexed_read)
        monkeypatch.setattr(xian_server, "_get_rpc_block", fake_get_rpc_block)
        monkeypatch.setattr(
            xian_server,
            "_get_rpc_block_by_hash",
            fake_get_rpc_block_by_hash,
        )

        assert await get_block(100) == {"height": 100, "block_hash": "abc"}
        assert await get_block_by_hash("abc") == {
            "height": 100,
            "block_hash": "abc",
        }

    @pytest.mark.asyncio
    async def test_transaction_list_falls_back_to_graphql_when_bds_fails(
        self,
        monkeypatch,
    ):
        async def fake_indexed_read(*_args, **_kwargs):
            raise RuntimeError("bds unavailable")

        async def fake_graphql_list(condition, *, limit, offset):
            assert condition == {"sender": "alice"}
            assert limit == 5
            assert offset == 2
            return [{"tx_hash": "tx-1", "sender": "alice"}]

        monkeypatch.setattr(xian_server, "_call_indexed_read", fake_indexed_read)
        monkeypatch.setattr(
            xian_server,
            "_graphql_list_txs_by_condition",
            fake_graphql_list,
        )

        result = await list_txs_by_sender("alice", limit=5, offset=2)

        assert result == [{"tx_hash": "tx-1", "sender": "alice"}]

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
                        "chi_supplied": 123,
                        "chi_estimated": 120,
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
                        "chi_supplied": 55,
                        "chi_estimated": 50,
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
        assert calls[0]["contract"] == "con_dex_helper"
        assert calls[1]["contract"] == "con_dex_helper"
        assert calls[0]["kwargs"]["sell_token"] == "currency"
        assert calls[1]["kwargs"]["buy_token"] == "currency"
        assert "deadline" in calls[0]["kwargs"]
        assert "deadline" in calls[1]["kwargs"]
        assert "deadline_min" not in calls[0]["kwargs"]
        assert "deadline_min" not in calls[1]["kwargs"]

    @pytest.mark.asyncio
    async def test_buy_on_dex_uses_decimal_payload_and_xian_deadline(
        self,
        monkeypatch,
    ):
        class FixedDateTime:
            @classmethod
            def now(cls, tz):
                assert tz is UTC
                return datetime(2026, 5, 21, 12, 0, 30, 123456, tzinfo=UTC)

        calls = []

        async def fake_send_transaction(**kwargs):
            calls.append(kwargs)
            return {"tx_hash": "buy"}

        monkeypatch.setattr(xian_server, "datetime", FixedDateTime)
        monkeypatch.setattr(xian_server, "send_transaction", fake_send_transaction)

        result = await buy_on_dex(
            private_key="1" * 64,
            buy_token=" con_token ",
            sell_token=" currency ",
            amount=1.25,
            slippage=0.95,
            deadline_min=2,
        )

        assert result == {"tx_hash": "buy"}
        assert calls[0]["contract"] == "con_dex_helper"
        assert calls[0]["function"] == "buy"
        assert calls[0]["kwargs"] == {
            "buy_token": "con_token",
            "sell_token": "currency",
            "amount": Decimal("1.25"),
            "slippage": Decimal("0.95"),
            "deadline": {"__time__": [2026, 5, 21, 12, 2, 30, 123456]},
        }

    @pytest.mark.asyncio
    async def test_get_dex_price_uses_sorted_pair_key_and_maps_reserves(
        self,
        monkeypatch,
    ):
        keys = []

        async def fake_get_state(key):
            keys.append(key)
            if key == "con_pairs.toks_to_pair:con_base:con_token":
                return {"state_value": "PAIR-1"}
            if key == "con_pairs.pairs:PAIR-1:reserve0":
                return {"state_value": Decimal("10")}
            if key == "con_pairs.pairs:PAIR-1:reserve1":
                return {"state_value": Decimal("2")}
            raise AssertionError(f"unexpected state key: {key}")

        monkeypatch.setattr(xian_server, "get_state", fake_get_state)

        result = await get_dex_price("con_token", "con_base")

        assert keys == [
            "con_pairs.toks_to_pair:con_base:con_token",
            "con_pairs.pairs:PAIR-1:reserve0",
            "con_pairs.pairs:PAIR-1:reserve1",
        ]
        assert result == {
            "token": "con_token",
            "base": "con_base",
            "price": 5.0,
            "pair_id": "PAIR-1",
            "reserve_token": 2.0,
            "reserve_base": 10.0,
        }


class TestIndexedReadTools:
    @pytest.mark.asyncio
    async def test_indexed_reads_repair_tx_hash_from_raw_payload(self, monkeypatch):
        class FakeXianAsync:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get_indexed_tx(self, tx_hash):
                assert tx_hash == "tx-1"
                return {
                    "tx_hash": None,
                    "created": None,
                    "raw": {
                        "hash": "tx-1",
                        "created_at": "2026-05-21T14:00:00+00:00",
                    },
                    "events": [
                        {
                            "tx_hash": None,
                            "raw": {"hash": "tx-1"},
                        }
                    ],
                }

        monkeypatch.setattr(xian_server, "XianAsync", FakeXianAsync)
        monkeypatch.delenv("XIAN_INCLUDE_RAW", raising=False)

        result = await xian_server._call_indexed_read("get_indexed_tx", "tx-1")

        assert result["tx_hash"] == "tx-1"
        assert result["created"] == "2026-05-21T14:00:00+00:00"
        assert result["events"][0]["tx_hash"] == "tx-1"
        assert "raw" not in result
        assert "raw" not in result["events"][0]

    @pytest.mark.asyncio
    async def test_bds_and_developer_reward_reads(self, monkeypatch):
        class FakeXianAsync:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get_bds_status(self):
                return {
                    "available": True,
                    "synced": True,
                    "latest_height": 123,
                }

            async def get_developer_rewards(self, recipient_key):
                assert recipient_key == "dev-key"
                return {
                    "recipient_key": recipient_key,
                    "total_rewards": 42,
                }

        monkeypatch.setattr(xian_server, "XianAsync", FakeXianAsync)

        status = await get_bds_status()
        rewards = await get_developer_rewards("dev-key")

        assert status["available"] is True
        assert status["latest_height"] == 123
        assert rewards["recipient_key"] == "dev-key"
        assert rewards["total_rewards"] == 42

    @pytest.mark.asyncio
    async def test_block_and_transaction_indexed_reads(self, monkeypatch):
        class FakeXianAsync:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def list_blocks(self, *, limit, offset):
                assert limit == 2
                assert offset == 1
                return [{"height": 100}, {"height": 101}]

            async def get_block(self, height):
                assert height == 100
                return {"height": height, "hash": "block-100"}

            async def get_block_by_hash(self, block_hash):
                assert block_hash == "block-100"
                return {"height": 100, "hash": block_hash}

            async def get_indexed_tx(self, tx_hash):
                assert tx_hash == "tx-1"
                return {"hash": tx_hash, "sender": "alice"}

            async def list_txs_for_block(self, block_ref):
                assert block_ref == 100
                return [{"hash": "tx-1"}]

            async def list_txs_by_sender(self, sender, *, limit, offset):
                assert sender == "alice"
                assert limit == 5
                assert offset == 2
                return [{"hash": "tx-2", "sender": sender}]

            async def list_txs_by_contract(self, contract, *, limit, offset):
                assert contract == "currency"
                assert limit == 3
                assert offset == 0
                return [{"hash": "tx-3", "contract": contract}]

        monkeypatch.setattr(xian_server, "XianAsync", FakeXianAsync)

        blocks = await list_blocks(limit=2, offset=1)
        block = await get_block(100)
        block_by_hash = await get_block_by_hash("block-100")
        indexed_tx = await get_indexed_tx("tx-1")
        txs_for_block = await list_txs_for_block("100")
        txs_by_sender = await list_txs_by_sender("alice", limit=5, offset=2)
        txs_by_contract = await list_txs_by_contract("currency", limit=3)

        assert [item["height"] for item in blocks] == [100, 101]
        assert block["hash"] == "block-100"
        assert block_by_hash["height"] == 100
        assert indexed_tx["sender"] == "alice"
        assert txs_for_block[0]["hash"] == "tx-1"
        assert txs_by_sender[0]["sender"] == "alice"
        assert txs_by_contract[0]["contract"] == "currency"

    @pytest.mark.asyncio
    async def test_event_and_state_indexed_reads(self, monkeypatch):
        class FakeXianAsync:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get_events_for_tx(self, tx_hash):
                assert tx_hash == "tx-1"
                return [{"event": "Transfer", "tx_hash": tx_hash}]

            async def list_events(
                self,
                contract,
                event,
                *,
                limit,
                offset,
                after_id,
            ):
                assert contract == "currency"
                assert event == "Transfer"
                assert limit == 10
                assert offset == 0
                assert after_id == 25
                return [{"id": 26, "contract": contract, "event": event}]

            async def get_state_history(self, key, *, limit, offset):
                assert key == "currency.balances:alice"
                assert limit == 10
                assert offset == 5
                return [{"key": key, "value": "100"}]

            async def get_state_for_tx(self, tx_hash):
                assert tx_hash == "tx-1"
                return [{"key": "currency.balances:alice"}]

            async def get_state_for_block(self, block_ref):
                assert block_ref == "block-100"
                return [{"key": "currency.balances:bob"}]

        monkeypatch.setattr(xian_server, "XianAsync", FakeXianAsync)

        tx_events = await get_events_for_tx("tx-1")
        events = await list_events(
            contract="currency",
            event="Transfer",
            limit=10,
            after_id=25,
        )
        state_history = await get_state_history(
            "currency.balances:alice",
            limit=10,
            offset=5,
        )
        state_for_tx = await get_state_for_tx("tx-1")
        state_for_block = await get_state_for_block("block-100")

        assert tx_events[0]["event"] == "Transfer"
        assert events[0]["id"] == 26
        assert state_history[0]["key"] == "currency.balances:alice"
        assert state_for_tx[0]["key"] == "currency.balances:alice"
        assert state_for_block[0]["key"] == "currency.balances:bob"

    @pytest.mark.asyncio
    async def test_shielded_indexed_reads(self, monkeypatch):
        class FakeXianAsync:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def list_shielded_output_tags(
                self,
                tag_value,
                *,
                kind,
                limit,
                offset,
                after_id,
            ):
                assert tag_value == "sync-tag"
                assert kind == "sync_hint"
                assert limit == 20
                assert offset == 0
                assert after_id == 7
                return [{"id": 8, "tag_value": tag_value, "kind": kind}]

            async def list_shielded_wallet_history(
                self,
                tag_value,
                *,
                kind,
                limit,
                after_note_index,
            ):
                assert tag_value == "sync-tag"
                assert kind == "sync_hint"
                assert limit == 15
                assert after_note_index == 3
                return [{"note_index": 4, "tag_value": tag_value}]

        monkeypatch.setattr(xian_server, "XianAsync", FakeXianAsync)

        tags = await list_shielded_output_tags(
            tag_value="sync-tag",
            limit=20,
            after_id=7,
        )
        history = await list_shielded_wallet_history(
            tag_value="sync-tag",
            limit=15,
            after_note_index=3,
        )

        assert tags[0]["id"] == 8
        assert history[0]["note_index"] == 4

    @pytest.mark.asyncio
    async def test_indexed_read_tools_validate_inputs(self):
        assert await get_developer_rewards("") == "❌ Error: Recipient key is required"
        assert await list_blocks(limit=0) == "❌ Error: Limit must be positive"
        assert await get_block("abc") == "❌ Error: Height must be an integer"
        assert await list_txs_for_block("") == "❌ Error: Block reference is required"
        assert await list_events("currency", "") == "❌ Error: Event is required"
        assert (
            await list_shielded_wallet_history("sync-tag", after_note_index=-1)
            == "❌ Error: After note index must be zero or greater"
        )


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
                    "chi_supplied": 2,
                    "chi_estimated": 1,
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


class TestUnsafeToolGating:
    @pytest.mark.asyncio
    async def test_list_tools_hides_unsafe_tools_by_default(self, monkeypatch):
        monkeypatch.delenv("XIAN_MCP_ENABLE_UNSAFE_WALLET_TOOLS", raising=False)

        tools = await xian_server.list_tools()
        tool_names = {tool.name for tool in tools}

        assert "create_wallet" not in tool_names
        assert "send_transaction" not in tool_names
        assert "sign_message" not in tool_names
        assert "get_balance" in tool_names

    @pytest.mark.asyncio
    async def test_call_tool_rejects_unsafe_tools_by_default(self, monkeypatch):
        monkeypatch.delenv("XIAN_MCP_ENABLE_UNSAFE_WALLET_TOOLS", raising=False)

        result = await xian_server.call_tool("create_wallet", {})

        assert len(result) == 1
        assert "disabled by default" in result[0].text

    @pytest.mark.asyncio
    async def test_list_tools_exposes_unsafe_tools_when_enabled(self, monkeypatch):
        monkeypatch.setenv("XIAN_MCP_ENABLE_UNSAFE_WALLET_TOOLS", "1")

        tools = await xian_server.list_tools()
        tool_names = {tool.name for tool in tools}

        assert "create_wallet" in tool_names
        assert "send_transaction" in tool_names
        assert "sign_message" in tool_names
