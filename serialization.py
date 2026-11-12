from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from typing import Any

from xian_runtime_types.decimal import ContractingDecimal, canonical_fixed
from xian_runtime_types.encoding import Encoder
from xian_runtime_types.time import Datetime, Timedelta

# Decimal arguments cross JSON as text. Integers are also accepted for whole amounts.
AMOUNT_SCHEMA = {"type": ["string", "integer"]}
# Larger integers lose precision in JavaScript clients and cross as {"__big_int__": text}.
MAX_SAFE_JSON_INTEGER = (1 << 53) - 1


def exact_amount(value: Any, *, field: str, positive: bool = False) -> Decimal:
    try:
        amount = Decimal(canonical_fixed(value))
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise ValueError(f"{field} must be an exact decimal string or integer: {exc}") from exc
    if positive and amount <= 0:
        raise ValueError(f"{field} must be positive")
    return amount


def _include_raw_payloads() -> bool:
    value = os.environ.get("XIAN_INCLUDE_RAW", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def normalize_for_transport(
    value: Any,
    *,
    include_raw: bool | None = None,
) -> Any:
    """Convert tool results into plain JSON data; unknown types raise TypeError."""
    if include_raw is None:
        include_raw = _include_raw_payloads()
    return _encode(value, include_raw)


def _encode(value: Any, include_raw: bool) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return _encode_scalar(value)
    if isinstance(value, (Decimal, ContractingDecimal)):
        return canonical_fixed(value)
    if isinstance(value, (Datetime, Timedelta)):
        # The same {"__time__"}/{"__delta__"} form tool arguments accept.
        return Encoder().default(value)
    return _encode_container(value, include_raw)


def _encode_scalar(value: Any) -> Any:
    if isinstance(value, int) and abs(value) > MAX_SAFE_JSON_INTEGER:
        return {"__big_int__": str(value)}
    return value


def _encode_container(value: Any, include_raw: bool) -> Any:
    if is_dataclass(value):
        return _encode(asdict(value), include_raw)
    if isinstance(value, Mapping):
        return {
            str(key): _encode(item, include_raw)
            for key, item in value.items()
            if include_raw or str(key) != "raw"
        }
    if isinstance(value, (list, tuple, set)):
        return [_encode(item, include_raw) for item in value]
    raise TypeError(f"no MCP transport encoding for {type(value).__name__}")
