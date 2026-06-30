"""Shared tool exposure policy for MCP transports."""

from __future__ import annotations

import os
from typing import Any

UNSAFE_WALLET_TOOLS_ENV = "XIAN_MCP_ENABLE_UNSAFE_WALLET_TOOLS"


def unsafe_wallet_tools_enabled() -> bool:
    value = os.environ.get(UNSAFE_WALLET_TOOLS_ENV, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def tool_is_available(spec: dict[str, Any]) -> bool:
    return not spec.get("unsafe") or unsafe_wallet_tools_enabled()


def unsafe_tool_disabled_message(name: str) -> str:
    return (
        f"{name} is disabled by default. Set {UNSAFE_WALLET_TOOLS_ENV}=1 "
        "to enable unsafe wallet/signing tools on a trusted local host."
    )
