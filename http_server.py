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
    HTTP_HOST  — bind address (default: 0.0.0.0)
    HTTP_PORT  — bind port    (default: 8100)
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from serialization import normalize_for_transport

logger = logging.getLogger(__name__)


def create_app(tool_specs: list[dict[str, Any]] | None = None) -> FastAPI:
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

    # Build lookup: name → {spec, handler}
    registry: dict[str, dict[str, Any]] = {}
    for spec in tool_specs:
        registry[spec["name"]] = {
            "description": spec["description"],
            "schema": spec["schema"],
            "handler": spec["handler"],
        }

    app = FastAPI(
        title="MCP HTTP Bridge",
        summary="REST API bridge for MCP tool servers.",
        version="1.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/tools")
    async def list_tools() -> list[dict[str, Any]]:
        """List all available tools with their schemas."""
        return [
            {
                "name": name,
                "description": entry["description"],
                "parameters": entry["schema"],
            }
            for name, entry in registry.items()
        ]

    @app.post("/tools/{tool_name}")
    async def call_tool(
        tool_name: str,
        body: dict[str, Any] | None = None,
    ) -> JSONResponse:
        """Call a tool by name with JSON arguments."""
        entry = registry.get(tool_name)
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown tool: {tool_name}. Use GET /tools to list available tools.",
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

    host = os.environ.get("HTTP_HOST", "0.0.0.0")
    port = int(os.environ.get("HTTP_PORT", "8100"))

    logger.info("Starting MCP HTTP Bridge on %s:%d", host, port)
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_http_server()
