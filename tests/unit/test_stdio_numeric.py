"""Exercise numeric simulation through a real MCP subprocess and the Python SDK."""

import base64
import json
import sys
from pathlib import Path

from aiohttp import web
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_stdio_simulation_preserves_exact_numbers(unused_tcp_port):
    amount = "9007199254740993.000000000000000000000000000001"
    count = str((1 << 511) + 1)
    kwargs = {"amount": {"__fixed__": amount}, "count": {"__big_int__": count}}
    requests = []

    async def query(request):
        path = json.loads(request.query["path"])
        operation, encoded = path.strip("/").split("/")
        assert operation == "simulate_tx"
        payload = json.loads(bytes.fromhex(encoded))
        requests.append(payload["kwargs"])
        value = base64.b64encode(json.dumps(payload["kwargs"]).encode()).decode()
        return web.json_response({"result": {"response": {"code": 0, "value": value}}})

    app = web.Application()
    app.router.add_post("/abci_query", query)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
        server = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(__file__).resolve().parents[2] / "xian_server.py")],
            env={"XIAN_NODE_URL": f"http://127.0.0.1:{unused_tcp_port}"},
        )
        async with stdio_client(server) as (reader, writer), ClientSession(reader, writer) as mcp:
            await mcp.initialize()
            args = {"contract": "con_numeric", "function": "echo", "address": "a" * 64}
            result = await mcp.call_tool("simulate_transaction", {**args, "kwargs": kwargs})
            assert result.isError is False
            assert json.loads(result.content[0].text) == {
                "amount": amount,
                "count": {"__big_int__": count},
            }
            error = await mcp.call_tool("simulate_transaction", {**args, "kwargs": {"amount": 0.1}})
            assert "Error" in error.content[0].text
            # MCP clients and models read the protocol flag, not the prose.
            assert error.isError is True
            unknown = await mcp.call_tool("no_such_tool", {})
            assert unknown.isError is True
        assert requests == [kwargs]
    finally:
        await runner.cleanup()
