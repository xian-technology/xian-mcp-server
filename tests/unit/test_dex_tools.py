from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from xian_py import Wallet

import dex_tools
from dex_plan_registry import PlanRegistry, PlanRegistryError
from http_server import HTTP_TOKEN_ENV, create_app
from tests.shared import TEST_ADDRESS, TEST_PRIVATE_KEY
from tool_policy import UNSAFE_WALLET_TOOLS_ENV

PAIRS = [
    {
        "pair_id": 1,
        "token0": "token_a",
        "token1": "token_b",
        "reserve0": 1000.0,
        "reserve1": 1000.0,
        "total_supply": 100.0,
        "lp_token": "lp_ab",
        "block_timestamp_last": None,
        "creation_time": None,
    },
    {
        "pair_id": 2,
        "token0": "token_b",
        "token1": "token_c",
        "reserve0": 1000.0,
        "reserve1": 2000.0,
        "total_supply": 200.0,
        "lp_token": "lp_bc",
        "block_timestamp_last": None,
        "creation_time": None,
    },
]


@pytest.fixture(autouse=True)
def isolated_plan_registry(monkeypatch):
    registry = PlanRegistry()
    monkeypatch.setattr(dex_tools, "PLAN_REGISTRY", registry)
    return registry


@pytest.fixture
def quote_chain(monkeypatch):
    async def all_pairs():
        return PAIRS

    async def fee_bps(account: str):
        assert account == TEST_ADDRESS
        return 30

    async def precision(token: str):
        return None

    monkeypatch.setattr(dex_tools, "_all_pairs", all_pairs)
    monkeypatch.setattr(dex_tools, "_fee_bps", fee_bps)
    monkeypatch.setattr(dex_tools, "_token_precision", precision)


@pytest.mark.asyncio
async def test_exact_in_and_out_quotes_select_structured_route(quote_chain):
    exact_in = await dex_tools.dex_quote_exact_in("token_a", "token_c", 100, account=TEST_ADDRESS)
    assert exact_in["path"] == [1, 2]
    assert exact_in["token_out"] == "token_c"
    assert exact_in["amount_out"] > 0
    assert exact_in["fee_bps"] == 30
    assert len(exact_in["hops"]) == 2
    assert exact_in["price_impact_bps"] > 0

    exact_out = await dex_tools.dex_quote_exact_out("token_a", "token_c", 100, account=TEST_ADDRESS)
    assert exact_out["path"] == [1, 2]
    assert exact_out["amount_in"] > 0
    assert "no exact-output entrypoint" in exact_out["warnings"][0]


@pytest.mark.asyncio
async def test_swap_plan_contains_exact_approval_and_supporting_call(monkeypatch):
    async def quote(**kwargs):
        return {
            "token_in": "token_a",
            "token_out": "token_b",
            "path": [1],
            "amount_in": 10.0,
            "amount_out": 9.0,
            "hops": [{"pair_id": 1, "token_in": "token_a", "token_out": "token_b"}],
            "fee_bps": 30,
            "price_impact": 0.01,
            "price_impact_bps": 100.0,
            "warnings": [],
        }

    async def flags(tokens):
        return {"token_a": True, "token_b": False}

    async def allowance(token, account, spender):
        return dex_tools.Decimal(0)

    monkeypatch.setattr(dex_tools, "_quote", quote)
    monkeypatch.setattr(dex_tools, "_fee_flags", flags)
    monkeypatch.setattr(dex_tools, "_allowance", allowance)

    plan = await dex_tools.dex_plan_swap(
        "token_a",
        "token_b",
        10,
        TEST_ADDRESS,
        TEST_ADDRESS,
        slippage_bps=100,
    )
    assert plan["operation"] == "swap_exact_in"
    assert plan["amounts"]["minimum_output"] == pytest.approx(8.91)
    assert plan["approvals"][0]["kwargs"] == {"amount": 10.0, "to": "con_dex"}
    assert plan["call"]["function"] == ("swapExactTokenForTokenSupportingFeeOnTransferTokens")
    assert plan["call"]["kwargs"]["deadline"] == plan["deadline"]["absolute"]
    assert plan["plan_id"]
    assert plan["plan_digest"].startswith("sha256:")
    assert datetime.fromisoformat(plan["issued_at"]) < datetime.fromisoformat(plan["expires_at"])
    canonical_plan = {
        key: value
        for key, value in plan.items()
        if key not in {"plan_id", "plan_digest", "issued_at", "expires_at"}
    }
    canonical_bytes = json.dumps(
        canonical_plan,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    assert plan["plan_digest"] == f"sha256:{hashlib.sha256(canonical_bytes).hexdigest()}"


@pytest.mark.asyncio
async def test_liquidity_plans_match_router_and_lp_semantics(monkeypatch):
    async def pair_for(token_a, token_b):
        return PAIRS[0]

    async def call(contract, function, **kwargs):
        assert function == "registeredLpTokenFor"
        return "lp_ab"

    async def allowance(token, account, spender):
        return dex_tools.Decimal(0)

    async def flags(tokens):
        return {token: False for token in tokens}

    monkeypatch.setattr(dex_tools, "_pair_for_tokens", pair_for)
    monkeypatch.setattr(dex_tools, "_call", call)
    monkeypatch.setattr(dex_tools, "_allowance", allowance)
    monkeypatch.setattr(dex_tools, "_fee_flags", flags)

    add = await dex_tools.dex_plan_add_liquidity(
        "token_a", "token_b", 10, 20, TEST_ADDRESS, TEST_ADDRESS
    )
    assert [call["contract"] for call in add["approvals"]] == ["token_a", "token_b"]
    assert add["call"]["function"] == "addLiquidity"
    assert add["amounts"]["expected_b"] == 10.0
    assert add["call"]["kwargs"]["lpToken"] == "lp_ab"

    remove = await dex_tools.dex_plan_remove_liquidity(
        "token_a", "token_b", 10, TEST_ADDRESS, TEST_ADDRESS
    )
    assert remove["approvals"][0]["contract"] == "lp_ab"
    assert remove["call"]["function"] == "removeLiquidity"
    assert remove["amounts"]["expected_a"] == 100.0
    assert remove["amounts"]["expected_b"] == 100.0


@pytest.mark.asyncio
async def test_submit_swap_simulates_and_submits_whitelisted_plan(monkeypatch):
    sent: list[tuple[str, str, dict[str, Any]]] = []

    class FakeXian:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send_tx(self, contract, function, kwargs, *, mode, wait_for_tx):
            assert mode == "checktx"
            assert wait_for_tx is True
            sent.append((contract, function, kwargs))
            return {"tx_hash": f"tx-{len(sent)}"}

    async def simulate(node_url, payload):
        return {"success": True, "function": payload["function"]}

    monkeypatch.setattr(dex_tools, "XianAsync", FakeXian)
    monkeypatch.setattr(dex_tools, "simulate_tx_async", simulate)
    account = Wallet(TEST_PRIVATE_KEY).public_key
    assert account == TEST_ADDRESS
    plan = {
        "plan_version": 1,
        "operation": "swap_exact_in",
        "network": {"chain_id": dex_tools.CHAIN_ID},
        "account": account,
        "recipient": account,
        "deadline": {
            "absolute": {"__time__": [2099, 1, 1, 0, 0, 0, 0]},
            "iso": "2099-01-01T00:00:00+00:00",
        },
        "calls": [
            {
                "kind": "approval",
                "contract": "token_a",
                "function": "approve",
                "kwargs": {"amount": 10.0, "to": "con_dex"},
            },
            {
                "kind": "action",
                "contract": "con_dex",
                "function": "swapExactTokenForToken",
                "kwargs": {
                    "amountIn": 10.0,
                    "to": account,
                    "deadline": {"__time__": [2099, 1, 1, 0, 0, 0, 0]},
                },
            },
        ],
    }
    issued = dex_tools.PLAN_REGISTRY.issue(plan)
    issued["calls"][1]["kwargs"]["amountIn"] = 999_999.0
    result = await dex_tools.dex_submit_swap(TEST_PRIVATE_KEY, issued["plan_id"])
    assert result["simulated"] is True
    assert result["plan_digest"] == issued["plan_digest"]
    assert result["plan_status"] == "consumed"
    assert len(result["simulations"]) == 2
    assert [item[1] for item in sent] == ["approve", "swapExactTokenForToken"]
    assert sent[-1][2]["amountIn"] == dex_tools.Decimal("10.0")
    assert result["final_transaction"]["tx_hash"] == "tx-2"

    replay = await dex_tools.dex_submit_swap(TEST_PRIVATE_KEY, issued["plan_id"])
    assert replay["ok"] is False
    assert "already consumed" in replay["error"]
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_plan_id_is_single_use_across_concurrent_submitters(monkeypatch):
    sent: list[str] = []

    class FakeXian:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send_tx(self, contract, function, kwargs, *, mode, wait_for_tx):
            sent.append(function)
            await asyncio.sleep(0)
            return {"tx_hash": "only-once"}

    async def simulate(node_url, payload):
        return {"status": 0}

    monkeypatch.setattr(dex_tools, "XianAsync", FakeXian)
    monkeypatch.setattr(dex_tools, "simulate_tx_async", simulate)
    plan = _server_swap_plan()
    issued = dex_tools.PLAN_REGISTRY.issue(plan)
    results = await asyncio.gather(
        dex_tools.dex_submit_swap(TEST_PRIVATE_KEY, issued["plan_id"]),
        dex_tools.dex_submit_swap(TEST_PRIVATE_KEY, issued["plan_id"]),
    )
    assert sum(result.get("plan_status") == "consumed" for result in results) == 1
    assert sum(result.get("ok") is False for result in results) == 1
    assert sent == ["swapExactTokenForToken"]


@pytest.mark.asyncio
async def test_partial_multi_call_failure_invalidates_plan(monkeypatch):
    sent: list[str] = []

    class FakeXian:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send_tx(self, contract, function, kwargs, *, mode, wait_for_tx):
            sent.append(function)
            if len(sent) == 2:
                raise RuntimeError("router unavailable")
            return {"tx_hash": "approval-committed"}

    async def simulate(node_url, payload):
        return {"status": 0}

    monkeypatch.setattr(dex_tools, "XianAsync", FakeXian)
    monkeypatch.setattr(dex_tools, "simulate_tx_async", simulate)
    plan = _server_swap_plan(with_approval=True)
    issued = dex_tools.PLAN_REGISTRY.issue(plan)
    failed = await dex_tools.dex_submit_swap(TEST_PRIVATE_KEY, issued["plan_id"])
    assert failed["ok"] is False
    assert sent == ["approve", "swapExactTokenForToken"]

    replay = await dex_tools.dex_submit_swap(TEST_PRIVATE_KEY, issued["plan_id"])
    assert replay["ok"] is False
    assert "already consumed" in replay["error"]
    assert sent == ["approve", "swapExactTokenForToken"]


def _server_swap_plan(*, with_approval: bool = False) -> dict[str, Any]:
    deadline = {"__time__": [2099, 1, 1, 0, 0, 0, 0]}
    calls: list[dict[str, Any]] = []
    if with_approval:
        calls.append(
            {
                "kind": "approval",
                "contract": "token_a",
                "function": "approve",
                "kwargs": {"amount": 10.0, "to": "con_dex"},
            }
        )
    calls.append(
        {
            "kind": "action",
            "contract": "con_dex",
            "function": "swapExactTokenForToken",
            "kwargs": {"amountIn": 10.0, "to": TEST_ADDRESS, "deadline": deadline},
        }
    )
    return {
        "plan_version": 1,
        "operation": "swap_exact_in",
        "network": {"chain_id": dex_tools.CHAIN_ID},
        "account": TEST_ADDRESS,
        "recipient": TEST_ADDRESS,
        "deadline": {"absolute": deadline, "iso": "2099-01-01T00:00:00+00:00"},
        "calls": calls,
    }


def test_registry_expiry_capacity_and_opaque_failure():
    now = datetime(2030, 1, 1, tzinfo=UTC)

    def clock():
        return now

    registry = PlanRegistry(ttl_seconds=30, max_entries=1, clock=clock)
    first = registry.issue(_server_swap_plan())
    second = registry.issue(_server_swap_plan())
    with pytest.raises(PlanRegistryError, match="Unknown, expired"):
        registry.claim(first["plan_id"])
    assert registry.claim(second["plan_id"]).plan_digest == second["plan_digest"]

    expiring = registry.issue(_server_swap_plan())
    now += timedelta(seconds=31)
    with pytest.raises(PlanRegistryError, match="Unknown, expired"):
        registry.claim(expiring["plan_id"])
    with pytest.raises(PlanRegistryError, match="Unknown, expired"):
        registry.claim("tampered-plan-id")


@pytest.mark.asyncio
async def test_submitter_consumes_plans_that_fail_binding_checks():
    wrong_chain = _server_swap_plan()
    wrong_chain["network"]["chain_id"] = "different-local-chain"
    issued = dex_tools.PLAN_REGISTRY.issue(wrong_chain)
    result = await dex_tools.dex_submit_swap(TEST_PRIVATE_KEY, issued["plan_id"])
    assert result["ok"] is False
    assert "chain_id" in result["error"]
    replay = await dex_tools.dex_submit_swap(TEST_PRIVATE_KEY, issued["plan_id"])
    assert "already consumed" in replay["error"]

    wrong_operation = _server_swap_plan()
    wrong_operation["operation"] = "add_liquidity"
    issued = dex_tools.PLAN_REGISTRY.issue(wrong_operation)
    result = await dex_tools.dex_submit_swap(TEST_PRIVATE_KEY, issued["plan_id"])
    assert result["ok"] is False
    assert "not valid for this submit tool" in result["error"]

    wrong_account = _server_swap_plan()
    wrong_account["account"] = "not-the-signing-account"
    issued = dex_tools.PLAN_REGISTRY.issue(wrong_account)
    result = await dex_tools.dex_submit_swap(TEST_PRIVATE_KEY, issued["plan_id"])
    assert result["ok"] is False
    assert "does not match the signing wallet" in result["error"]

    expired = _server_swap_plan()
    expired_deadline = {"__time__": [2020, 1, 1, 0, 0, 0, 0]}
    expired["deadline"] = {
        "absolute": expired_deadline,
        "iso": "2020-01-01T00:00:00+00:00",
    }
    expired["calls"][-1]["kwargs"]["deadline"] = expired_deadline
    with pytest.raises(ValueError, match="deadline has expired"):
        dex_tools._validate_plan(expired, {"swap_exact_in"})


@pytest.mark.asyncio
async def test_dex_events_report_unavailable_index_as_structured_json(monkeypatch):
    class FakeXian:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def list_events(self, *args, **kwargs):
            raise RuntimeError("index unavailable")

    monkeypatch.setattr(dex_tools, "XianAsync", FakeXian)
    result = await dex_tools.dex_list_events(contract="con_pairs", event="Swap", limit=5)
    assert result["available"] is False
    assert result["items"] == []
    assert result["warnings"]


@pytest.mark.asyncio
async def test_dex_wait_live_event_uses_websocket_without_bds_and_filters(monkeypatch):
    class FakeXian:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def watch_live_events(self, contract, event):
            assert (contract, event) == ("con_pairs", "Swap")
            yield {
                "tx_hash": "OTHER",
                "contract": contract,
                "event": event,
                "signer": TEST_ADDRESS,
            }
            yield {
                "tx_hash": "abc123",
                "block_height": 42,
                "contract": contract,
                "event": event,
                "signer": TEST_ADDRESS,
            }

        async def list_events(self, *args, **kwargs):
            raise AssertionError("live waits must not use the BDS event index")

    monkeypatch.setattr(dex_tools, "XianAsync", FakeXian)
    result = await dex_tools.dex_wait_live_event(
        contract="con_pairs",
        event="Swap",
        timeout_seconds=1,
        tx_hash="ABC123",
        signer=TEST_ADDRESS,
    )

    assert result["delivery"] == "cometbft_websocket"
    assert result["bds_required"] is False
    assert result["durable"] is False
    assert result["timed_out"] is False
    assert result["count"] == 1
    assert result["items"][0]["tx_hash"] == "abc123"


@pytest.mark.asyncio
async def test_dex_wait_live_event_returns_partial_batch_on_timeout(monkeypatch):
    class FakeXian:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def watch_live_events(self, contract, event):
            yield {"tx_hash": "first", "contract": contract, "event": event}
            await asyncio.Event().wait()

    monkeypatch.setattr(dex_tools, "XianAsync", FakeXian)
    result = await dex_tools.dex_wait_live_event(
        contract="con_pairs",
        event="Swap",
        timeout_seconds=0.01,
        max_events=2,
    )

    assert result["timed_out"] is True
    assert result["count"] == 1
    assert result["items"][0]["tx_hash"] == "first"


@pytest.mark.asyncio
async def test_dex_wait_live_event_validates_source_and_bounds():
    missing = await dex_tools.dex_wait_live_event(contract="con_pairs", event="")
    assert missing["ok"] is False

    unknown = await dex_tools.dex_wait_live_event(contract="con_pairs", event="Transfer")
    assert unknown["ok"] is False

    excessive = await dex_tools.dex_wait_live_event(
        contract="con_pairs", event="Swap", timeout_seconds=121
    )
    assert excessive["ok"] is False


def test_http_catalog_exposes_read_planners_and_gates_submitters(monkeypatch):
    monkeypatch.delenv(UNSAFE_WALLET_TOOLS_ENV, raising=False)
    monkeypatch.delenv(HTTP_TOKEN_ENV, raising=False)
    client = TestClient(create_app())
    names = {tool["name"] for tool in client.get("/tools").json()}
    assert "dex_quote_exact_in" in names
    assert "dex_plan_add_liquidity" in names
    assert "dex_submit_swap" not in names

    rejected = client.post(
        "/tools/dex_submit_swap", json={"private_key": "x", "plan_id": "not-issued"}
    )
    assert rejected.status_code == 403

    monkeypatch.setenv(UNSAFE_WALLET_TOOLS_ENV, "1")
    monkeypatch.setenv(HTTP_TOKEN_ENV, "dev-secret")
    enabled = TestClient(create_app()).get("/tools", headers={"Authorization": "Bearer dev-secret"})
    enabled_names = {tool["name"] for tool in enabled.json()}
    assert {
        "dex_submit_swap",
        "dex_submit_add_liquidity",
        "dex_submit_remove_liquidity",
    } <= enabled_names
    submit_schema = next(
        tool["parameters"] for tool in enabled.json() if tool["name"] == "dex_submit_swap"
    )
    assert submit_schema["required"] == ["private_key", "plan_id"]
    assert "plan" not in submit_schema["properties"]
    caller_supplied_plan = TestClient(create_app()).post(
        "/tools/dex_submit_swap",
        headers={"Authorization": "Bearer dev-secret"},
        json={"private_key": "x", "plan": {}},
    )
    assert caller_supplied_plan.status_code == 422


def test_http_plan_id_round_trip_and_replay_rejection(monkeypatch):
    async def quote(**kwargs):
        return {
            "token_in": "token_a",
            "token_out": "token_b",
            "path": [1],
            "amount_in": 1.0,
            "amount_out": 0.9,
            "hops": [{"pair_id": 1, "token_in": "token_a", "token_out": "token_b"}],
            "fee_bps": 30,
            "price_impact": 0.01,
            "price_impact_bps": 100.0,
            "warnings": [],
        }

    async def flags(tokens):
        return {token: False for token in tokens}

    async def allowance(token, account, spender):
        return dex_tools.Decimal(100)

    class FakeXian:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send_tx(self, contract, function, kwargs, *, mode, wait_for_tx):
            return {"tx_hash": "http-plan-id"}

    async def simulate(node_url, payload):
        return {"status": 0}

    monkeypatch.setattr(dex_tools, "_quote", quote)
    monkeypatch.setattr(dex_tools, "_fee_flags", flags)
    monkeypatch.setattr(dex_tools, "_allowance", allowance)
    monkeypatch.setattr(dex_tools, "XianAsync", FakeXian)
    monkeypatch.setattr(dex_tools, "simulate_tx_async", simulate)
    monkeypatch.setenv(UNSAFE_WALLET_TOOLS_ENV, "1")
    monkeypatch.setenv(HTTP_TOKEN_ENV, "dev-secret")
    client = TestClient(create_app())
    headers = {"Authorization": "Bearer dev-secret"}

    planned = client.post(
        "/tools/dex_plan_swap",
        headers=headers,
        json={
            "token_in": "token_a",
            "token_out": "token_b",
            "amount": 1,
            "account": TEST_ADDRESS,
            "recipient": TEST_ADDRESS,
        },
    )
    assert planned.status_code == 200
    plan = planned.json()["result"]
    submitted = client.post(
        "/tools/dex_submit_swap",
        headers=headers,
        json={"private_key": TEST_PRIVATE_KEY, "plan_id": plan["plan_id"]},
    )
    assert submitted.status_code == 200
    assert submitted.json()["result"]["final_transaction"]["tx_hash"] == "http-plan-id"
    replay = client.post(
        "/tools/dex_submit_swap",
        headers=headers,
        json={"private_key": TEST_PRIVATE_KEY, "plan_id": plan["plan_id"]},
    )
    assert replay.json()["result"]["ok"] is False
