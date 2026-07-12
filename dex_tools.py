"""Agent-oriented DEX discovery, quote, plan, submit, and event tools.

The call shapes in this module mirror ``xian-dex/dex-interface.json``. Plans
are issued into a process-local single-use registry so submission can execute
only the exact calls that this server previously planned.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any

from xian_py import Wallet, XianAsync
from xian_py.transaction import simulate_tx_async

from dex_plan_registry import ClaimedPlan, PlanRegistry
from serialization import normalize_for_transport

CHAIN_ID = os.environ.get("XIAN_CHAIN_ID", "xian-local-1")
NODE_URL = os.environ.get("XIAN_NODE_URL", "http://127.0.0.1:26657")
PAIRS_CONTRACT = "con_pairs"
ROUTER_CONTRACT = "con_dex"
PLAN_VERSION = 1
MAX_ROUTE_PAIRS = 500
PLAN_TTL_ENV = "XIAN_MCP_DEX_PLAN_TTL_SECONDS"
PLAN_CAPACITY_ENV = "XIAN_MCP_DEX_PLAN_MAX_ENTRIES"


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


PLAN_REGISTRY = PlanRegistry(
    ttl_seconds=_bounded_env_int(PLAN_TTL_ENV, 300, 30, 900),
    max_entries=_bounded_env_int(PLAN_CAPACITY_ENV, 256, 1, 1_000),
)

DEX_EVENTS: dict[str, tuple[str, ...]] = {
    PAIRS_CONTRACT: (
        "PairCreated",
        "LpTokenRegistered",
        "Mint",
        "Burn",
        "Swap",
        "Sync",
    ),
    ROUTER_CONTRACT: ("ZeroFeeTraderUpdated",),
}


def _error(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def _decimal(value: Any, *, field: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not result.is_finite() or (positive and result <= 0):
        qualifier = "a positive" if positive else "a finite"
        raise ValueError(f"{field} must be {qualifier} number")
    return result


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return parsed


def _non_negative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a non-negative integer") from exc
    if parsed < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return parsed


def _slippage_bps(value: Any) -> int:
    parsed = _non_negative_int(value, field="Slippage bps")
    if parsed > 10_000:
        raise ValueError("Slippage bps must not exceed 10000")
    return parsed


def _deadline(minutes: Any) -> tuple[dict[str, list[int]], str]:
    minutes_value = _decimal(minutes, field="Deadline minutes", positive=True)
    future = datetime.now(UTC) + timedelta(seconds=float(minutes_value * 60))
    encoded = {
        "__time__": [
            future.year,
            future.month,
            future.day,
            future.hour,
            future.minute,
            future.second,
            future.microsecond,
        ]
    }
    return encoded, future.isoformat()


async def _call(contract: str, function: str, **kwargs: Any) -> Any:
    async with XianAsync(NODE_URL, chain_id=CHAIN_ID) as xian:
        return normalize_for_transport(await xian.contract(contract).call(function, kwargs=kwargs))


async def _state(contract: str, variable: str, *keys: Any) -> Any:
    async with XianAsync(NODE_URL, chain_id=CHAIN_ID) as xian:
        return normalize_for_transport(await xian.get_state(contract, variable, *keys))


async def _pair_record(pair_id: int) -> dict[str, Any] | None:
    values = await asyncio.gather(
        *(
            _state(PAIRS_CONTRACT, "pairs", pair_id, field)
            for field in (
                "token0",
                "token1",
                "reserve0",
                "reserve1",
                "totalSupply",
                "lpToken",
                "blockTimestampLast",
                "creationTime",
            )
        )
    )
    token0, token1 = values[:2]
    if token0 in (None, 0) or token1 in (None, 0):
        return None
    return {
        "pair_id": pair_id,
        "token0": str(token0),
        "token1": str(token1),
        "reserve0": float(values[2] or 0),
        "reserve1": float(values[3] or 0),
        "total_supply": float(values[4] or 0),
        "lp_token": None if values[5] in (None, 0) else str(values[5]),
        "block_timestamp_last": values[6],
        "creation_time": values[7],
    }


async def dex_get_pair(
    pair_id: int | None = None,
    token_a: str = "",
    token_b: str = "",
) -> dict[str, Any] | str:
    """Resolve one pair by id or token contracts."""
    try:
        if pair_id is None:
            if not token_a.strip() or not token_b.strip():
                return _error("Provide pair_id or both token_a and token_b")
            resolved = await _call(
                PAIRS_CONTRACT,
                "pairFor",
                tokenA=token_a.strip(),
                tokenB=token_b.strip(),
            )
            if resolved in (None, 0):
                return {
                    "found": False,
                    "token_a": token_a.strip(),
                    "token_b": token_b.strip(),
                }
            pair_id = _positive_int(resolved, field="Pair id")
        else:
            pair_id = _positive_int(pair_id, field="Pair id")
        pair = await _pair_record(pair_id)
        return {"found": pair is not None, "pair": pair}
    except Exception as exc:
        return _error(f"Unable to get DEX pair: {exc}")


async def dex_list_pairs(
    limit: int = 100,
    offset: int = 0,
    token: str = "",
) -> dict[str, Any] | str:
    """List canonical pairs directly from con_pairs state."""
    try:
        limit = _positive_int(limit, field="Limit")
        offset = _non_negative_int(offset, field="Offset")
        if limit > MAX_ROUTE_PAIRS:
            return _error(f"Limit must not exceed {MAX_ROUTE_PAIRS}")
        total = int(await _state(PAIRS_CONTRACT, "pairs_num") or 0)
        ids = range(offset + 1, min(total, offset + limit) + 1)
        records = await asyncio.gather(*(_pair_record(pair_id) for pair_id in ids))
        token_filter = token.strip()
        items = [
            pair
            for pair in records
            if pair is not None
            and (not token_filter or token_filter in (pair["token0"], pair["token1"]))
        ]
        return {
            "items": items,
            "count": len(items),
            "total_pairs": total,
            "limit": limit,
            "offset": offset,
            "token_filter": token_filter or None,
        }
    except Exception as exc:
        return _error(f"Unable to list DEX pairs: {exc}")


async def _all_pairs() -> list[dict[str, Any]]:
    total = int(await _state(PAIRS_CONTRACT, "pairs_num") or 0)
    if total > MAX_ROUTE_PAIRS:
        raise ValueError(
            f"DEX has {total} pairs; pass an explicit path because automatic routing is capped at "
            f"{MAX_ROUTE_PAIRS} pairs"
        )
    records = await asyncio.gather(*(_pair_record(pair_id) for pair_id in range(1, total + 1)))
    return [pair for pair in records if pair is not None]


def _routes(
    pairs: list[dict[str, Any]],
    token_in: str,
    token_out: str,
    max_hops: int,
) -> list[list[int]]:
    adjacency: dict[str, list[tuple[int, str]]] = {}
    for pair in pairs:
        if pair["reserve0"] <= 0 or pair["reserve1"] <= 0:
            continue
        adjacency.setdefault(pair["token0"], []).append((pair["pair_id"], pair["token1"]))
        adjacency.setdefault(pair["token1"], []).append((pair["pair_id"], pair["token0"]))
    for edges in adjacency.values():
        edges.sort()

    found: list[list[int]] = []

    def visit(current: str, path: list[int], seen_tokens: set[str]) -> None:
        if current == token_out and path:
            found.append(path.copy())
            return
        if len(path) >= max_hops:
            return
        for pair_id, other in adjacency.get(current, []):
            if pair_id in path or other in seen_tokens:
                continue
            path.append(pair_id)
            seen_tokens.add(other)
            visit(other, path, seen_tokens)
            seen_tokens.remove(other)
            path.pop()

    visit(token_in, [], {token_in})
    return found


def _route_hops(
    pairs_by_id: dict[int, dict[str, Any]], token_in: str, path: list[int]
) -> tuple[list[dict[str, Any]], str]:
    current = token_in
    hops: list[dict[str, Any]] = []
    for pair_id in path:
        pair = pairs_by_id.get(pair_id)
        if pair is None:
            raise ValueError(f"Pair {pair_id} does not exist")
        if current == pair["token0"]:
            target = pair["token1"]
            reserve_in, reserve_out = pair["reserve0"], pair["reserve1"]
        elif current == pair["token1"]:
            target = pair["token0"]
            reserve_in, reserve_out = pair["reserve1"], pair["reserve0"]
        else:
            raise ValueError(f"Pair {pair_id} does not connect from {current}")
        if reserve_in <= 0 or reserve_out <= 0:
            raise ValueError(f"Pair {pair_id} has insufficient liquidity")
        hops.append(
            {
                "pair_id": pair_id,
                "token_in": current,
                "token_out": target,
                "reserve_in": reserve_in,
                "reserve_out": reserve_out,
            }
        )
        current = target
    return hops, current


async def _token_precision(token: str) -> int | None:
    try:
        metadata = await _call(token, "get_metadata")
    except Exception:
        return None
    if not isinstance(metadata, dict):
        return None
    precision = metadata.get("precision")
    return precision if isinstance(precision, int) and precision >= 0 else None


async def _normalize_amount(value: Decimal, token: str, *, round_up: bool = False) -> Decimal:
    precision = await _token_precision(token)
    if precision is None:
        return value
    scale = Decimal(10) ** precision
    scaled = value * scale
    if round_up:
        return scaled.to_integral_value(rounding=ROUND_CEILING) / scale
    return Decimal(int(scaled)) / scale


async def _fee_bps(account: str) -> int:
    value = await _call(
        ROUTER_CONTRACT,
        "getTradeFeeBps",
        account=account.strip() or None,
    )
    fee = int(value)
    if fee not in (0, 30):
        raise ValueError(f"Unsupported on-chain trade fee {fee} bps")
    return fee


async def _quote_path_exact_in(
    pairs_by_id: dict[int, dict[str, Any]],
    token_in: str,
    path: list[int],
    amount_in: Decimal,
    fee_bps: int,
) -> dict[str, Any]:
    hops, token_out = _route_hops(pairs_by_id, token_in, path)
    amount = await _normalize_amount(amount_in, token_in)
    amounts = [amount]
    mid_amount = amount
    detailed: list[dict[str, Any]] = []
    multiplier = Decimal(10_000 - fee_bps) / Decimal(10_000)
    for hop in hops:
        reserve_in = Decimal(str(hop["reserve_in"]))
        reserve_out = Decimal(str(hop["reserve_out"]))
        amount_with_fee = amount * multiplier
        output = (amount_with_fee * reserve_out) / (reserve_in + amount_with_fee)
        output = await _normalize_amount(output, hop["token_out"])
        mid_amount = mid_amount * reserve_out / reserve_in
        detailed.append({**hop, "amount_in": float(amount), "amount_out": float(output)})
        amount = output
        amounts.append(amount)
    impact = Decimal(0) if mid_amount <= 0 else max(Decimal(0), 1 - amount / mid_amount)
    return {
        "kind": "exact_in",
        "token_in": token_in,
        "token_out": token_out,
        "path": path,
        "amount_in": float(amounts[0]),
        "amount_out": float(amounts[-1]),
        "amounts": [float(value) for value in amounts],
        "hops": detailed,
        "fee_bps": fee_bps,
        "mid_price_output": float(mid_amount),
        "price_impact": float(impact),
        "price_impact_bps": float(impact * 10_000),
    }


async def _quote_path_exact_out(
    pairs_by_id: dict[int, dict[str, Any]],
    token_in: str,
    path: list[int],
    amount_out: Decimal,
    fee_bps: int,
) -> dict[str, Any]:
    hops, token_out = _route_hops(pairs_by_id, token_in, path)
    desired = await _normalize_amount(amount_out, token_out, round_up=True)
    amounts: list[Decimal] = [Decimal(0)] * (len(hops) + 1)
    amounts[-1] = desired
    multiplier = Decimal(10_000 - fee_bps) / Decimal(10_000)
    for index in range(len(hops) - 1, -1, -1):
        hop = hops[index]
        reserve_in = Decimal(str(hop["reserve_in"]))
        reserve_out = Decimal(str(hop["reserve_out"]))
        required_out = amounts[index + 1]
        if required_out >= reserve_out:
            raise ValueError(f"Pair {hop['pair_id']} has insufficient liquidity")
        required_in = reserve_in * required_out / ((reserve_out - required_out) * multiplier)
        amounts[index] = await _normalize_amount(required_in, hop["token_in"], round_up=True)
    mid_input = desired
    for hop in reversed(hops):
        mid_input = mid_input * Decimal(str(hop["reserve_in"])) / Decimal(str(hop["reserve_out"]))
    impact = Decimal(0) if amounts[0] <= 0 else max(Decimal(0), 1 - mid_input / amounts[0])
    detailed = [
        {**hop, "amount_in": float(amounts[index]), "amount_out": float(amounts[index + 1])}
        for index, hop in enumerate(hops)
    ]
    return {
        "kind": "exact_out",
        "token_in": token_in,
        "token_out": token_out,
        "path": path,
        "amount_in": float(amounts[0]),
        "amount_out": float(desired),
        "amounts": [float(value) for value in amounts],
        "hops": detailed,
        "fee_bps": fee_bps,
        "mid_price_input": float(mid_input),
        "price_impact": float(impact),
        "price_impact_bps": float(impact * 10_000),
        "warnings": [
            "The router has no exact-output entrypoint; this quote is used to build a capped exact-input call."
        ],
    }


async def _quote(
    *,
    kind: str,
    token_in: str,
    token_out: str,
    amount: Any,
    path: list[int] | None,
    account: str,
    max_hops: int,
) -> dict[str, Any]:
    token_in = token_in.strip()
    token_out = token_out.strip()
    if not token_in or not token_out or token_in == token_out:
        raise ValueError("token_in and token_out must be different non-empty contracts")
    amount_value = _decimal(amount, field="Amount", positive=True)
    max_hops = _positive_int(max_hops, field="Max hops")
    if max_hops > 5:
        raise ValueError("Max hops must not exceed 5")
    pairs = await _all_pairs()
    pairs_by_id = {pair["pair_id"]: pair for pair in pairs}
    paths = (
        [[_positive_int(item, field="Path pair id") for item in path]]
        if path
        else _routes(pairs, token_in, token_out, max_hops)
    )
    if not paths:
        raise ValueError(f"No liquid route from {token_in} to {token_out}")
    fee = await _fee_bps(account)
    quotes = []
    for candidate in paths:
        try:
            quote = (
                await _quote_path_exact_in(pairs_by_id, token_in, candidate, amount_value, fee)
                if kind == "exact_in"
                else await _quote_path_exact_out(
                    pairs_by_id, token_in, candidate, amount_value, fee
                )
            )
            if quote["token_out"] == token_out:
                quotes.append(quote)
        except ValueError:
            if path:
                raise
    if not quotes:
        raise ValueError(f"No valid route from {token_in} to {token_out}")
    if kind == "exact_in":
        quotes.sort(key=lambda quote: (-quote["amount_out"], len(quote["path"]), quote["path"]))
    else:
        quotes.sort(key=lambda quote: (quote["amount_in"], len(quote["path"]), quote["path"]))
    best = quotes[0]
    best["account"] = account.strip() or None
    best["route_candidates_considered"] = len(quotes)
    warnings = best.setdefault("warnings", [])
    if best["price_impact_bps"] >= 500:
        warnings.append("High price impact: at least 5%.")
    return best


async def dex_quote_exact_in(
    token_in: str = "",
    token_out: str = "",
    amount_in: float = 0,
    path: list[int] | None = None,
    account: str = "",
    max_hops: int = 3,
) -> dict[str, Any] | str:
    """Quote the best exact-input route under current reserves and signer fee tier."""
    try:
        return await _quote(
            kind="exact_in",
            token_in=token_in,
            token_out=token_out,
            amount=amount_in,
            path=path,
            account=account,
            max_hops=max_hops,
        )
    except Exception as exc:
        return _error(f"Unable to quote exact input: {exc}")


async def dex_quote_exact_out(
    token_in: str = "",
    token_out: str = "",
    amount_out: float = 0,
    path: list[int] | None = None,
    account: str = "",
    max_hops: int = 3,
) -> dict[str, Any] | str:
    """Quote input required for a desired output under current reserves."""
    try:
        return await _quote(
            kind="exact_out",
            token_in=token_in,
            token_out=token_out,
            amount=amount_out,
            path=path,
            account=account,
            max_hops=max_hops,
        )
    except Exception as exc:
        return _error(f"Unable to quote exact output: {exc}")


async def _fee_flags(tokens: list[str]) -> dict[str, bool]:
    values = await asyncio.gather(
        *(_state(ROUTER_CONTRACT, "fee_on_transfer_tokens", token) for token in tokens)
    )
    return {token: value is True for token, value in zip(tokens, values, strict=True)}


async def _allowance(token: str, account: str, spender: str) -> Decimal:
    if not account:
        return Decimal(0)
    value = await _state(token, "approvals", account, spender)
    return _decimal(value or 0, field="Allowance")


def _approval(token: str, amount: Decimal) -> dict[str, Any]:
    return {
        "kind": "approval",
        "contract": token,
        "function": "approve",
        "kwargs": {"amount": float(amount), "to": ROUTER_CONTRACT},
    }


def _plan_base(operation: str, deadline_value: dict[str, Any], deadline_iso: str) -> dict[str, Any]:
    return {
        "plan_version": PLAN_VERSION,
        "operation": operation,
        "network": {"chain_id": CHAIN_ID},
        "contracts": {"pairs": PAIRS_CONTRACT, "router": ROUTER_CONTRACT},
        "deadline": {"absolute": deadline_value, "iso": deadline_iso},
    }


async def dex_plan_swap(
    token_in: str = "",
    token_out: str = "",
    amount: float = 0,
    account: str = "",
    recipient: str = "",
    mode: str = "exact_in",
    path: list[int] | None = None,
    slippage_bps: int = 100,
    deadline_minutes: float = 5,
    fee_on_transfer: str = "auto",
    max_hops: int = 3,
) -> dict[str, Any] | str:
    """Issue a fresh single-use swap plan with exact approval and router calls."""
    try:
        if not account.strip() or not recipient.strip():
            return _error("Account and explicit recipient are required")
        if mode not in {"exact_in", "exact_out"}:
            return _error("Mode must be exact_in or exact_out")
        if fee_on_transfer not in {"auto", "plain", "supporting"}:
            return _error("fee_on_transfer must be auto, plain, or supporting")
        slip = _slippage_bps(slippage_bps)
        quote = await _quote(
            kind=mode,
            token_in=token_in,
            token_out=token_out,
            amount=amount,
            path=path,
            account=account,
            max_hops=max_hops,
        )
        route_tokens = [quote["token_in"]] + [hop["token_out"] for hop in quote["hops"]]
        flags = await _fee_flags(route_tokens)
        flagged = [token for token, enabled in flags.items() if enabled]
        supporting = fee_on_transfer == "supporting" or (
            fee_on_transfer == "auto" and bool(flagged)
        )
        if fee_on_transfer == "plain" and flagged:
            return _error("Plain routes reject flagged fee-on-transfer tokens")
        if supporting and any(flags[token] for token in route_tokens[1:-1]):
            return _error("Supporting routes reject flagged intermediate bridge tokens")
        if mode == "exact_out" and supporting:
            return _error("Exact-output intent cannot be guaranteed for fee-on-transfer routes")

        if mode == "exact_in":
            call_amount_in = Decimal(str(quote["amount_in"]))
            amount_out_min = (
                Decimal(str(quote["amount_out"])) * Decimal(10_000 - slip) / Decimal(10_000)
            )
            operation = "swap_exact_in"
        else:
            call_amount_in = (
                Decimal(str(quote["amount_in"])) * Decimal(10_000 + slip) / Decimal(10_000)
            )
            call_amount_in = await _normalize_amount(
                call_amount_in, quote["token_in"], round_up=True
            )
            amount_out_min = Decimal(str(quote["amount_out"]))
            operation = "swap_exact_out_intent"

        deadline_value, deadline_iso = _deadline(deadline_minutes)
        single = len(quote["path"]) == 1
        if supporting:
            function = (
                "swapExactTokenForTokenSupportingFeeOnTransferTokens"
                if single
                else "swapExactTokensForTokensSupportingFeeOnTransferTokens"
            )
        else:
            function = "swapExactTokenForToken" if single else "swapExactTokensForTokens"
        kwargs: dict[str, Any] = {
            "amountIn": float(call_amount_in),
            "amountOutMin": float(amount_out_min),
            "src": quote["token_in"],
            "to": recipient.strip(),
            "deadline": deadline_value,
        }
        kwargs["pair" if single else "path"] = quote["path"][0] if single else quote["path"]
        action = {
            "kind": "action",
            "contract": ROUTER_CONTRACT,
            "function": function,
            "kwargs": kwargs,
        }
        allowance = await _allowance(quote["token_in"], account.strip(), ROUTER_CONTRACT)
        approvals = (
            [] if allowance >= call_amount_in else [_approval(quote["token_in"], call_amount_in)]
        )
        plan = _plan_base(operation, deadline_value, deadline_iso)
        plan.update(
            {
                "account": account.strip(),
                "recipient": recipient.strip(),
                "route": {"path": quote["path"], "tokens": route_tokens, "hops": quote["hops"]},
                "amounts": {
                    "requested": float(_decimal(amount, field="Amount", positive=True)),
                    "quoted_input": quote["amount_in"],
                    "quoted_output": quote["amount_out"],
                    "call_input": float(call_amount_in),
                    "minimum_output": float(amount_out_min),
                },
                "fee": {"trade_fee_bps": quote["fee_bps"], "signer": account.strip()},
                "slippage": {"bps": slip},
                "fee_on_transfer": {
                    "choice": fee_on_transfer,
                    "supporting_route": supporting,
                    "flags": flags,
                },
                "price_impact": quote["price_impact"],
                "price_impact_bps": quote["price_impact_bps"],
                "approvals": approvals,
                "call": action,
                "calls": approvals + [action],
                "warnings": list(quote.get("warnings", [])),
            }
        )
        if mode == "exact_out":
            plan["warnings"].append(
                "The exact-input router spends the full call_input; any input slippage reserve is "
                "traded for additional output rather than refunded."
            )
        if supporting:
            plan["warnings"].append(
                "Fee-on-transfer output is bounded by amountOutMin but may differ from the nominal quote."
            )
        return PLAN_REGISTRY.issue(plan)
    except Exception as exc:
        return _error(f"Unable to plan swap: {exc}")


async def _pair_for_tokens(token_a: str, token_b: str) -> dict[str, Any] | None:
    pair_id = await _call(PAIRS_CONTRACT, "pairFor", tokenA=token_a, tokenB=token_b)
    return None if pair_id in (None, 0) else await _pair_record(int(pair_id))


async def dex_plan_add_liquidity(
    token_a: str = "",
    token_b: str = "",
    amount_a_desired: float = 0,
    amount_b_desired: float = 0,
    account: str = "",
    recipient: str = "",
    slippage_bps: int = 100,
    deadline_minutes: float = 10,
) -> dict[str, Any] | str:
    """Issue approvals and router addLiquidity using the current pool ratio."""
    try:
        token_a, token_b = token_a.strip(), token_b.strip()
        if not token_a or not token_b or token_a == token_b:
            return _error("token_a and token_b must be different non-empty contracts")
        if not account.strip() or not recipient.strip():
            return _error("Account and explicit recipient are required")
        desired_a = _decimal(amount_a_desired, field="Amount A desired", positive=True)
        desired_b = _decimal(amount_b_desired, field="Amount B desired", positive=True)
        slip = _slippage_bps(slippage_bps)
        pair = await _pair_for_tokens(token_a, token_b)
        lp_token = await _call(
            PAIRS_CONTRACT, "registeredLpTokenFor", tokenA=token_a, tokenB=token_b
        )
        if lp_token in (None, 0):
            return _error("No canonical LP token is registered for this pair")
        expected_a, expected_b = desired_a, desired_b
        warnings: list[str] = []
        if pair and pair["reserve0"] > 0 and pair["reserve1"] > 0:
            if token_a == pair["token0"]:
                reserve_a, reserve_b = (
                    Decimal(str(pair["reserve0"])),
                    Decimal(str(pair["reserve1"])),
                )
            else:
                reserve_a, reserve_b = (
                    Decimal(str(pair["reserve1"])),
                    Decimal(str(pair["reserve0"])),
                )
            optimal_b = desired_a * reserve_b / reserve_a
            if optimal_b <= desired_b:
                expected_b = await _normalize_amount(optimal_b, token_b)
            else:
                expected_a = await _normalize_amount(desired_b * reserve_a / reserve_b, token_a)
        else:
            warnings.append("This call creates the registered pair and seeds its initial price.")
        min_a = expected_a * Decimal(10_000 - slip) / Decimal(10_000)
        min_b = expected_b * Decimal(10_000 - slip) / Decimal(10_000)
        deadline_value, deadline_iso = _deadline(deadline_minutes)
        approvals: list[dict[str, Any]] = []
        for token, desired in ((token_a, desired_a), (token_b, desired_b)):
            if await _allowance(token, account.strip(), ROUTER_CONTRACT) < desired:
                approvals.append(_approval(token, desired))
        action = {
            "kind": "action",
            "contract": ROUTER_CONTRACT,
            "function": "addLiquidity",
            "kwargs": {
                "tokenA": token_a,
                "tokenB": token_b,
                "amountADesired": float(desired_a),
                "amountBDesired": float(desired_b),
                "amountAMin": float(min_a),
                "amountBMin": float(min_b),
                "to": recipient.strip(),
                "deadline": deadline_value,
                "lpToken": str(lp_token),
            },
        }
        flags = await _fee_flags([token_a, token_b])
        if any(flags.values()):
            warnings.append(
                "Fee-on-transfer ingress can reduce actual deposited amounts; minimums still apply."
            )
        plan = _plan_base("add_liquidity", deadline_value, deadline_iso)
        plan.update(
            {
                "account": account.strip(),
                "recipient": recipient.strip(),
                "pair": pair,
                "lp_token": str(lp_token),
                "amounts": {
                    "desired_a": float(desired_a),
                    "desired_b": float(desired_b),
                    "expected_a": float(expected_a),
                    "expected_b": float(expected_b),
                    "minimum_a": float(min_a),
                    "minimum_b": float(min_b),
                },
                "slippage": {"bps": slip},
                "fee_on_transfer": {"flags": flags},
                "price_impact": None,
                "approvals": approvals,
                "call": action,
                "calls": approvals + [action],
                "warnings": warnings,
            }
        )
        return PLAN_REGISTRY.issue(plan)
    except Exception as exc:
        return _error(f"Unable to plan add liquidity: {exc}")


async def dex_plan_remove_liquidity(
    token_a: str = "",
    token_b: str = "",
    liquidity: float = 0,
    account: str = "",
    recipient: str = "",
    slippage_bps: int = 100,
    deadline_minutes: float = 10,
) -> dict[str, Any] | str:
    """Issue LP approval and removeLiquidity from current reserves/supply."""
    try:
        token_a, token_b = token_a.strip(), token_b.strip()
        if not token_a or not token_b or token_a == token_b:
            return _error("token_a and token_b must be different non-empty contracts")
        if not account.strip() or not recipient.strip():
            return _error("Account and explicit recipient are required")
        liquidity_value = _decimal(liquidity, field="Liquidity", positive=True)
        slip = _slippage_bps(slippage_bps)
        pair = await _pair_for_tokens(token_a, token_b)
        if pair is None or not pair["lp_token"]:
            return _error("Pair or bound LP token does not exist")
        total_supply = Decimal(str(pair["total_supply"]))
        if total_supply <= 0 or liquidity_value > total_supply:
            return _error("Liquidity exceeds the pair total supply")
        if token_a == pair["token0"]:
            reserve_a, reserve_b = Decimal(str(pair["reserve0"])), Decimal(str(pair["reserve1"]))
        else:
            reserve_a, reserve_b = Decimal(str(pair["reserve1"])), Decimal(str(pair["reserve0"]))
        expected_a = liquidity_value * reserve_a / total_supply
        expected_b = liquidity_value * reserve_b / total_supply
        min_a = expected_a * Decimal(10_000 - slip) / Decimal(10_000)
        min_b = expected_b * Decimal(10_000 - slip) / Decimal(10_000)
        deadline_value, deadline_iso = _deadline(deadline_minutes)
        allowance = await _allowance(pair["lp_token"], account.strip(), ROUTER_CONTRACT)
        approvals = (
            [] if allowance >= liquidity_value else [_approval(pair["lp_token"], liquidity_value)]
        )
        action = {
            "kind": "action",
            "contract": ROUTER_CONTRACT,
            "function": "removeLiquidity",
            "kwargs": {
                "tokenA": token_a,
                "tokenB": token_b,
                "liquidity": float(liquidity_value),
                "amountAMin": float(min_a),
                "amountBMin": float(min_b),
                "to": recipient.strip(),
                "deadline": deadline_value,
            },
        }
        plan = _plan_base("remove_liquidity", deadline_value, deadline_iso)
        plan.update(
            {
                "account": account.strip(),
                "recipient": recipient.strip(),
                "pair": pair,
                "lp_token": pair["lp_token"],
                "amounts": {
                    "liquidity": float(liquidity_value),
                    "expected_a": float(expected_a),
                    "expected_b": float(expected_b),
                    "minimum_a": float(min_a),
                    "minimum_b": float(min_b),
                },
                "slippage": {"bps": slip},
                "fee_on_transfer": {"flags": await _fee_flags([token_a, token_b])},
                "price_impact": None,
                "approvals": approvals,
                "call": action,
                "calls": approvals + [action],
                "warnings": [
                    "Protocol-fee minting and fee-on-transfer output can reduce actual amounts; minimums bound the transaction."
                ],
            }
        )
        return PLAN_REGISTRY.issue(plan)
    except Exception as exc:
        return _error(f"Unable to plan remove liquidity: {exc}")


def _validate_plan(plan: dict[str, Any], allowed_operations: set[str]) -> list[dict[str, Any]]:
    if not isinstance(plan, dict) or plan.get("plan_version") != PLAN_VERSION:
        raise ValueError(f"plan_version must be {PLAN_VERSION}")
    operation = plan.get("operation")
    if operation not in allowed_operations:
        raise ValueError(f"Plan operation {operation!r} is not valid for this submit tool")
    if plan.get("network", {}).get("chain_id") != CHAIN_ID:
        raise ValueError("Plan chain_id does not match the configured chain")
    if not isinstance(plan.get("account"), str) or not plan["account"]:
        raise ValueError("Plan account is required")
    deadline = plan.get("deadline")
    if not isinstance(deadline, dict) or not isinstance(deadline.get("absolute"), dict):
        raise ValueError("Plan deadline is malformed")
    try:
        deadline_time = datetime.fromisoformat(deadline["iso"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Plan deadline is malformed") from exc
    if deadline_time.tzinfo is None or deadline_time.astimezone(UTC) <= datetime.now(UTC):
        raise ValueError("Plan deadline has expired")
    calls = plan.get("calls")
    if not isinstance(calls, list) or not calls:
        raise ValueError("Plan calls are required")
    allowed_actions = {
        "swap_exact_in": {
            "swapExactTokenForToken",
            "swapExactTokensForTokens",
            "swapExactTokenForTokenSupportingFeeOnTransferTokens",
            "swapExactTokensForTokensSupportingFeeOnTransferTokens",
        },
        "swap_exact_out_intent": {
            "swapExactTokenForToken",
            "swapExactTokensForTokens",
        },
        "add_liquidity": {"addLiquidity"},
        "remove_liquidity": {"removeLiquidity"},
    }
    for index, call in enumerate(calls):
        if not isinstance(call, dict) or not isinstance(call.get("kwargs"), dict):
            raise ValueError(f"Call {index} is malformed")
        if index < len(calls) - 1:
            if call.get("kind") != "approval" or call.get("function") != "approve":
                raise ValueError("Only token approvals may precede the DEX action")
            if call["kwargs"].get("to") != ROUTER_CONTRACT:
                raise ValueError("Approvals must target con_dex")
        elif (
            call.get("kind") != "action"
            or call.get("contract") != ROUTER_CONTRACT
            or call.get("function") not in allowed_actions[operation]
        ):
            raise ValueError("Final call does not match the planned DEX operation")
        elif call["kwargs"].get("deadline") != deadline["absolute"]:
            raise ValueError("Final call deadline does not match the plan deadline")
    action_recipient = calls[-1]["kwargs"].get("to")
    if plan.get("recipient") != action_recipient:
        raise ValueError("Final call recipient does not match the plan recipient")
    return calls


def _contract_kwargs(value: Any) -> Any:
    """Restore exact decimal values after a plan crosses a JSON transport."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _contract_kwargs(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_contract_kwargs(item) for item in value]
    return value


async def _submit_plan(
    private_key: str,
    plan_id: str,
    allowed_operations: set[str],
    simulate: bool,
) -> dict[str, Any] | str:
    if not private_key.strip():
        return _error("Private key is required")
    if not isinstance(plan_id, str) or not plan_id.strip():
        return _error("plan_id is required")
    claimed: ClaimedPlan | None = None
    try:
        # Claim first: every real submission attempt is single-use even if wallet
        # validation, simulation, an approval, or the final router call fails.
        claimed = PLAN_REGISTRY.claim(plan_id)
        plan = claimed.plan
        calls = _validate_plan(plan, allowed_operations)
        wallet = Wallet(private_key.strip())
        if plan.get("account") != wallet.public_key:
            return _error("Plan account does not match the signing wallet")
        simulations: list[Any] = []
        transactions: list[Any] = []
        async with XianAsync(NODE_URL, wallet=wallet, chain_id=CHAIN_ID) as xian:
            for call in calls:
                kwargs = _contract_kwargs(call["kwargs"])
                if simulate:
                    simulation = normalize_for_transport(
                        await simulate_tx_async(
                            NODE_URL,
                            {
                                "contract": call["contract"],
                                "function": call["function"],
                                "kwargs": kwargs,
                                "sender": wallet.public_key,
                            },
                        )
                    )
                    simulations.append(simulation)
                    if isinstance(simulation, dict) and simulation.get("status") not in (
                        None,
                        0,
                    ):
                        raise ValueError(
                            f"Simulation failed for {call['contract']}.{call['function']}: "
                            f"{simulation.get('result')}"
                        )
                transaction = normalize_for_transport(
                    await xian.send_tx(
                        call["contract"],
                        call["function"],
                        kwargs,
                        mode="checktx",
                        wait_for_tx=True,
                    )
                )
                if isinstance(transaction, dict):
                    if transaction.get("accepted") is False:
                        raise ValueError(
                            f"Transaction rejected for {call['contract']}.{call['function']}: "
                            f"{transaction.get('message')}"
                        )
                    receipt = transaction.get("receipt")
                    if isinstance(receipt, dict) and receipt.get("success") is False:
                        raise ValueError(
                            f"Transaction failed for {call['contract']}.{call['function']}: "
                            f"{receipt.get('message')}"
                        )
                transactions.append(transaction)
        return {
            "plan_id": claimed.plan_id,
            "plan_digest": claimed.plan_digest,
            "issued_at": claimed.issued_at,
            "expires_at": claimed.expires_at,
            "plan_status": "consumed",
            "operation": plan["operation"],
            "plan_version": PLAN_VERSION,
            "simulated": simulate,
            "simulations": simulations,
            "transactions": transactions,
            "final_transaction": transactions[-1],
        }
    except Exception as exc:
        return _error(f"Unable to submit DEX plan: {exc}")


async def dex_submit_swap(
    private_key: str = "", plan_id: str = "", simulate: bool = True
) -> dict[str, Any] | str:
    return await _submit_plan(
        private_key,
        plan_id,
        {"swap_exact_in", "swap_exact_out_intent"},
        simulate,
    )


async def dex_submit_add_liquidity(
    private_key: str = "", plan_id: str = "", simulate: bool = True
) -> dict[str, Any] | str:
    return await _submit_plan(private_key, plan_id, {"add_liquidity"}, simulate)


async def dex_submit_remove_liquidity(
    private_key: str = "", plan_id: str = "", simulate: bool = True
) -> dict[str, Any] | str:
    return await _submit_plan(private_key, plan_id, {"remove_liquidity"}, simulate)


async def dex_wait_live_event(
    contract: str = "",
    event: str = "",
    timeout_seconds: float = 30,
    max_events: int = 1,
    tx_hash: str = "",
    signer: str = "",
    caller: str = "",
) -> dict[str, Any] | str:
    """Wait for finalized DEX events directly from the CometBFT websocket.

    This is deliberately a bounded, non-durable live wait. It avoids the BDS
    indexing delay, but callers that need replay or restart-safe cursors must
    use :func:`dex_list_events` as their recovery path.
    """
    try:
        contract = contract.strip()
        event = event.strip()
        if not contract or not event:
            return _error("Contract and event are required for a live DEX event wait")
        known = DEX_EVENTS.get(contract)
        if known is not None and event not in known:
            return _error(f"{event} is not declared by {contract}")

        timeout = _decimal(timeout_seconds, field="Timeout seconds", positive=True)
        if timeout > 120:
            return _error("Timeout seconds must not exceed 120")
        max_events = _positive_int(max_events, field="Max events")
        if max_events > 50:
            return _error("Max events must not exceed 50")

        tx_hash_filter = tx_hash.strip().upper()
        signer_filter = signer.strip()
        caller_filter = caller.strip()
        items: list[dict[str, Any]] = []

        async def collect() -> None:
            async with XianAsync(NODE_URL, chain_id=CHAIN_ID) as xian:
                async for live_event in xian.watch_live_events(contract, event):
                    item = normalize_for_transport(live_event)
                    if not isinstance(item, dict):
                        continue
                    item_tx_hash = str(item.get("tx_hash") or "").upper()
                    if tx_hash_filter and item_tx_hash != tx_hash_filter:
                        continue
                    if signer_filter and item.get("signer") != signer_filter:
                        continue
                    if caller_filter and item.get("caller") != caller_filter:
                        continue
                    items.append(item)
                    if len(items) >= max_events:
                        return

        timed_out = False
        try:
            await asyncio.wait_for(collect(), timeout=float(timeout))
        except TimeoutError:
            timed_out = True

        return {
            "delivery": "cometbft_websocket",
            "durable": False,
            "bds_required": False,
            "timed_out": timed_out,
            "items": items,
            "count": len(items),
            "contract": contract,
            "event": event,
            "filters": {
                "tx_hash": tx_hash_filter or None,
                "signer": signer_filter or None,
                "caller": caller_filter or None,
            },
            "warning": (
                "Live events have no replay cursor. Use dex_list_events with after_id "
                "for restart-safe recovery and history."
            ),
        }
    except Exception as exc:
        return _error(f"Unable to wait for live DEX event: {exc}")


async def dex_list_events(
    contract: str = "",
    event: str = "",
    limit: int = 100,
    after_id: int | None = None,
) -> dict[str, Any] | str:
    """List canonical DEX events, optionally across pair/router contracts."""
    try:
        limit = _positive_int(limit, field="Limit")
        if limit > 500:
            return _error("Limit must not exceed 500")
        if after_id is not None:
            after_id = _non_negative_int(after_id, field="After id")
        contract = contract.strip()
        event = event.strip()
        if contract:
            known = DEX_EVENTS.get(contract)
            if known is None:
                # LP token contracts are accepted only with an explicit event.
                if not event:
                    return _error("An event is required for an LP token contract")
                sources = [(contract, event)]
            elif event:
                if event not in known:
                    return _error(f"{event} is not declared by {contract}")
                sources = [(contract, event)]
            else:
                sources = [(contract, name) for name in known]
        elif event:
            sources = [(name, event) for name, events in DEX_EVENTS.items() if event in events]
            if not sources:
                return _error("Unknown canonical DEX event; provide its LP token contract")
        else:
            sources = [
                (name, event_name) for name, events in DEX_EVENTS.items() for event_name in events
            ]
        async with XianAsync(NODE_URL, chain_id=CHAIN_ID) as xian:
            batches = await asyncio.gather(
                *(
                    xian.list_events(
                        source_contract,
                        source_event,
                        limit=limit,
                        offset=0,
                        after_id=after_id,
                    )
                    for source_contract, source_event in sources
                ),
                return_exceptions=True,
            )
        failures = [str(batch) for batch in batches if isinstance(batch, Exception)]
        items = [
            normalize_for_transport(item)
            for batch in batches
            if not isinstance(batch, Exception)
            for item in batch
        ]
        items.sort(key=lambda item: int(item.get("id") or item.get("event_id") or 0))
        items = items[:limit]
        cursor_values = [int(item.get("id") or item.get("event_id") or 0) for item in items]
        return {
            "available": len(failures) < len(batches),
            "items": items,
            "count": len(items),
            "sources": [{"contract": name, "event": name_event} for name, name_event in sources],
            "after_id": after_id,
            "next_after_id": max(cursor_values) if cursor_values else after_id,
            "warnings": (
                [
                    "Some or all indexed DEX event sources are unavailable; run a BDS-enabled node "
                    "or configure its GraphQL index."
                ]
                if failures
                else []
            ),
        }
    except Exception as exc:
        return _error(f"Unable to list DEX events: {exc}")


_PATH_SCHEMA = {"type": "array", "items": {"type": "integer"}, "minItems": 1}
_PLAN_ID_SCHEMA = {
    "type": "string",
    "minLength": 32,
    "description": "Single-use opaque plan_id returned by the matching dex_plan_* tool",
}

DEX_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "dex_list_pairs",
        "description": "List canonical Xian DEX pairs and reserves",
        "schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 100},
                "offset": {"type": "integer", "default": 0},
                "token": {"type": "string"},
            },
            "required": [],
        },
        "handler": dex_list_pairs,
    },
    {
        "name": "dex_get_pair",
        "description": "Get a canonical DEX pair by id or token contracts",
        "schema": {
            "type": "object",
            "properties": {
                "pair_id": {"type": "integer"},
                "token_a": {"type": "string"},
                "token_b": {"type": "string"},
            },
            "required": [],
        },
        "handler": dex_get_pair,
    },
    {
        "name": "dex_quote_exact_in",
        "description": "Quote the best route and output for an exact input amount",
        "schema": {
            "type": "object",
            "properties": {
                "token_in": {"type": "string"},
                "token_out": {"type": "string"},
                "amount_in": {"type": "number", "exclusiveMinimum": 0},
                "path": _PATH_SCHEMA,
                "account": {
                    "type": "string",
                    "description": "Signer account used to resolve its fee tier",
                },
                "max_hops": {"type": "integer", "default": 3},
            },
            "required": ["token_in", "token_out", "amount_in"],
        },
        "handler": dex_quote_exact_in,
    },
    {
        "name": "dex_quote_exact_out",
        "description": "Quote the least input required for a desired output amount",
        "schema": {
            "type": "object",
            "properties": {
                "token_in": {"type": "string"},
                "token_out": {"type": "string"},
                "amount_out": {"type": "number", "exclusiveMinimum": 0},
                "path": _PATH_SCHEMA,
                "account": {
                    "type": "string",
                    "description": "Signer account used to resolve its fee tier",
                },
                "max_hops": {"type": "integer", "default": 3},
            },
            "required": ["token_in", "token_out", "amount_out"],
        },
        "handler": dex_quote_exact_out,
    },
    {
        "name": "dex_plan_swap",
        "description": "Issue a single-use DEX swap plan with fresh quote, exact calls, digest, and expiry",
        "schema": {
            "type": "object",
            "properties": {
                "token_in": {"type": "string"},
                "token_out": {"type": "string"},
                "amount": {"type": "number", "exclusiveMinimum": 0},
                "account": {"type": "string"},
                "recipient": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["exact_in", "exact_out"],
                    "default": "exact_in",
                },
                "path": _PATH_SCHEMA,
                "slippage_bps": {"type": "integer", "default": 100},
                "deadline_minutes": {"type": "number", "default": 5},
                "fee_on_transfer": {
                    "type": "string",
                    "enum": ["auto", "plain", "supporting"],
                    "default": "auto",
                },
                "max_hops": {"type": "integer", "default": 3},
            },
            "required": ["token_in", "token_out", "amount", "account", "recipient"],
        },
        "handler": dex_plan_swap,
    },
    {
        "name": "dex_submit_swap",
        "description": "Consume, simulate, and submit the exact server-stored dex_plan_swap plan",
        "schema": {
            "type": "object",
            "properties": {
                "private_key": {"type": "string"},
                "plan_id": _PLAN_ID_SCHEMA,
                "simulate": {"type": "boolean", "default": True},
            },
            "required": ["private_key", "plan_id"],
        },
        "unsafe": True,
        "handler": dex_submit_swap,
    },
    {
        "name": "dex_plan_add_liquidity",
        "description": "Issue a single-use addLiquidity plan with exact calls, digest, and expiry",
        "schema": {
            "type": "object",
            "properties": {
                "token_a": {"type": "string"},
                "token_b": {"type": "string"},
                "amount_a_desired": {"type": "number", "exclusiveMinimum": 0},
                "amount_b_desired": {"type": "number", "exclusiveMinimum": 0},
                "account": {"type": "string"},
                "recipient": {"type": "string"},
                "slippage_bps": {"type": "integer", "default": 100},
                "deadline_minutes": {"type": "number", "default": 10},
            },
            "required": [
                "token_a",
                "token_b",
                "amount_a_desired",
                "amount_b_desired",
                "account",
                "recipient",
            ],
        },
        "handler": dex_plan_add_liquidity,
    },
    {
        "name": "dex_submit_add_liquidity",
        "description": "Consume, simulate, and submit the exact server-stored add-liquidity plan",
        "schema": {
            "type": "object",
            "properties": {
                "private_key": {"type": "string"},
                "plan_id": _PLAN_ID_SCHEMA,
                "simulate": {"type": "boolean", "default": True},
            },
            "required": ["private_key", "plan_id"],
        },
        "unsafe": True,
        "handler": dex_submit_add_liquidity,
    },
    {
        "name": "dex_plan_remove_liquidity",
        "description": "Issue a single-use removeLiquidity plan with exact calls, digest, and expiry",
        "schema": {
            "type": "object",
            "properties": {
                "token_a": {"type": "string"},
                "token_b": {"type": "string"},
                "liquidity": {"type": "number", "exclusiveMinimum": 0},
                "account": {"type": "string"},
                "recipient": {"type": "string"},
                "slippage_bps": {"type": "integer", "default": 100},
                "deadline_minutes": {"type": "number", "default": 10},
            },
            "required": ["token_a", "token_b", "liquidity", "account", "recipient"],
        },
        "handler": dex_plan_remove_liquidity,
    },
    {
        "name": "dex_submit_remove_liquidity",
        "description": "Consume, simulate, and submit the exact server-stored remove-liquidity plan",
        "schema": {
            "type": "object",
            "properties": {
                "private_key": {"type": "string"},
                "plan_id": _PLAN_ID_SCHEMA,
                "simulate": {"type": "boolean", "default": True},
            },
            "required": ["private_key", "plan_id"],
        },
        "unsafe": True,
        "handler": dex_submit_remove_liquidity,
    },
    {
        "name": "dex_wait_live_event",
        "description": (
            "Wait for low-latency finalized DEX events over CometBFT WebSocket without BDS; "
            "non-durable, so use dex_list_events for replay"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "contract": {"type": "string"},
                "event": {"type": "string"},
                "timeout_seconds": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 120,
                    "default": 30,
                },
                "max_events": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 1,
                },
                "tx_hash": {"type": "string"},
                "signer": {"type": "string"},
                "caller": {"type": "string"},
            },
            "required": ["contract", "event"],
        },
        "handler": dex_wait_live_event,
    },
    {
        "name": "dex_list_events",
        "description": "List indexed canonical DEX or bound LP-token events with a restart-safe cursor",
        "schema": {
            "type": "object",
            "properties": {
                "contract": {"type": "string"},
                "event": {"type": "string"},
                "limit": {"type": "integer", "default": 100},
                "after_id": {"type": "integer"},
            },
            "required": [],
        },
        "handler": dex_list_events,
    },
]
