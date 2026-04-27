from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from typing import Any


def _is_contracting_decimal(value: Any) -> bool:
    return value.__class__.__name__ == "ContractingDecimal"


def _include_raw_payloads() -> bool:
    value = os.environ.get("XIAN_INCLUDE_RAW", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def normalize_for_transport(
    value: Any,
    *,
    include_raw: bool | None = None,
) -> Any:
    """Convert SDK/model objects into plain JSON-friendly Python data."""
    if include_raw is None:
        include_raw = _include_raw_payloads()

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Decimal) or _is_contracting_decimal(value):
        return float(value)

    if is_dataclass(value):
        return normalize_for_transport(asdict(value), include_raw=include_raw)

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return normalize_for_transport(model_dump(), include_raw=include_raw)

    dict_method = getattr(value, "dict", None)
    if callable(dict_method):
        return normalize_for_transport(dict_method(), include_raw=include_raw)

    if isinstance(value, Mapping):
        return {
            str(key): normalize_for_transport(item, include_raw=include_raw)
            for key, item in value.items()
            if include_raw or str(key) != "raw"
        }

    if isinstance(value, (list, tuple, set)):
        return [
            normalize_for_transport(item, include_raw=include_raw) for item in value
        ]

    value_dict = getattr(value, "__dict__", None)
    if isinstance(value_dict, dict):
        return normalize_for_transport(value_dict, include_raw=include_raw)

    return value
