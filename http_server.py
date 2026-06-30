#!/usr/bin/env python3
"""
Generic HTTP wrapper for MCP tool servers.

Exposes any MCP server's TOOL_SPECS as a simple REST API:

    GET  /tools              → list available tools with schemas
    POST /tools/{tool_name}  → call a tool with JSON body

This allows MCP tools to be consumed by HTTP clients (web apps,
other APIs, AI tool-calling loops) without needing stdio or SSE.

Usage:
    # Standalone
    python http_server.py

    # Or import and mount in your own FastAPI app
    from http_server import create_app
    app = create_app()

Environment:
    HTTP_HOST  — bind address (default: 127.0.0.1)
    HTTP_PORT  — bind port    (default: 8100)
    XIAN_MCP_HTTP_TOKEN — optional bearer token; required for unsafe tools
                          or non-loopback binds
    XIAN_MCP_HTTP_CORS_ORIGINS — comma-separated browser origins to allow
"""

from __future__ import annotations

import ipaddress
import logging
import os
import secrets
import sys
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from serialization import normalize_for_transport
from tool_policy import (
    UNSAFE_WALLET_TOOLS_ENV,
    tool_is_available,
    unsafe_tool_disabled_message,
    unsafe_wallet_tools_enabled,
)

logger = logging.getLogger(__name__)

HTTP_HOST_ENV = "HTTP_HOST"
HTTP_PORT_ENV = "HTTP_PORT"
HTTP_TOKEN_ENV = "XIAN_MCP_HTTP_TOKEN"
HTTP_CORS_ORIGINS_ENV = "XIAN_MCP_HTTP_CORS_ORIGINS"
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = "8100"


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _configured_cors_origins() -> list[str]:
    origins = _split_csv(os.environ.get(HTTP_CORS_ORIGINS_ENV, ""))
    if "*" in origins:
        raise RuntimeError(
            f"{HTTP_CORS_ORIGINS_ENV} must list explicit origins; wildcard CORS is not allowed"
        )
    return origins


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _token_required_reason(bind_host: str) -> str | None:
    if os.environ.get(HTTP_TOKEN_ENV, ""):
        return f"{HTTP_TOKEN_ENV} is set"
    if unsafe_wallet_tools_enabled():
        return f"{UNSAFE_WALLET_TOOLS_ENV}=1 enables wallet/signing tools"
    if not _is_loopback_host(bind_host):
        return f"{HTTP_HOST_ENV}={bind_host} is not a loopback bind"
    return None


def _require_http_auth(request: Request, *, bind_host: str) -> None:
    reason = _token_required_reason(bind_host)
    if reason is None:
        return

    expected_token = os.environ.get(HTTP_TOKEN_ENV, "")
    if not expected_token:
        raise HTTPException(
            status_code=503,
            detail=(
                f"{HTTP_TOKEN_ENV} is required because {reason}. "
                "Set a bearer token or bind HTTP to 127.0.0.1 with unsafe tools disabled."
            ),
        )

    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if (
        separator != " "
        or scheme.lower() != "bearer"
        or not secrets.compare_digest(token, expected_token)
    ):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def create_app(
    tool_specs: list[dict[str, Any]] | None = None,
    *,
    bind_host: str | None = None,
) -> FastAPI:
    """Create the HTTP wrapper app.

    Parameters
    ----------
    tool_specs:
        List of tool spec dicts, each with ``name``, ``description``,
        ``schema``, and ``handler`` keys.  If *None*, imports
        ``TOOL_SPECS`` from ``xian_server`` (the default MCP server
        in this repo).
    """
    if tool_specs is None:
        from xian_server import TOOL_SPECS

        tool_specs = TOOL_SPECS

    bind_host = bind_host or os.environ.get(HTTP_HOST_ENV, DEFAULT_HTTP_HOST)

    # Build lookup: name -> {spec, handler}
    registry: dict[str, dict[str, Any]] = {}
    for spec in tool_specs:
        registry[spec["name"]] = {
            "description": spec["description"],
            "schema": spec["schema"],
            "spec": spec,
            "handler": spec["handler"],
        }

    app = FastAPI(
        title="MCP HTTP Bridge",
        summary="REST API bridge for MCP tool servers.",
        version="1.0.0",
    )

    cors_origins = _configured_cors_origins()
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["authorization", "content-type"],
        )

    @app.get("/tools")
    async def list_tools(request: Request) -> list[dict[str, Any]]:
        """List all available tools with their schemas."""
        _require_http_auth(request, bind_host=bind_host)
        return [
            {
                "name": name,
                "description": entry["description"],
                "parameters": entry["schema"],
            }
            for name, entry in registry.items()
            if tool_is_available(entry["spec"])
        ]

    @app.post("/tools/{tool_name}")
    async def call_tool(
        request: Request,
        tool_name: str,
        body: dict[str, Any] | None = None,
    ) -> JSONResponse:
        """Call a tool by name with JSON arguments."""
        _require_http_auth(request, bind_host=bind_host)
        entry = registry.get(tool_name)
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown tool: {tool_name}. Use GET /tools to list available tools.",
            )
        if not tool_is_available(entry["spec"]):
            raise HTTPException(
                status_code=403,
                detail=unsafe_tool_disabled_message(tool_name),
            )

        handler = entry["handler"]
        body = body or {}

        try:
            result = await handler(**body)
        except TypeError as ex:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid arguments for {tool_name}: {ex}",
            )
        except Exception as ex:
            logger.error("Error executing tool %s: %s", tool_name, ex, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Tool execution failed: {ex}",
            )

        # Normalize error responses from tool handlers
        if isinstance(result, str) and result.startswith("❌"):
            return JSONResponse(
                status_code=400,
                content={"error": result[2:].strip()},
            )

        return JSONResponse(content={"result": normalize_for_transport(result)})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "tools": str(len(registry))}

    return app


def run_http_server() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
    )

    host = os.environ.get(HTTP_HOST_ENV, DEFAULT_HTTP_HOST)
    port = int(os.environ.get(HTTP_PORT_ENV, DEFAULT_HTTP_PORT))

    logger.info("Starting MCP HTTP Bridge on %s:%d", host, port)
    uvicorn.run(create_app(bind_host=host), host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_http_server()
