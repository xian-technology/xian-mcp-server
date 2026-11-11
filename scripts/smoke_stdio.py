"""Start the installed server and verify the public tool catalog over stdio."""

import asyncio

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def smoke() -> None:
    async with asyncio.timeout(20):
        server = StdioServerParameters(command="xian-mcp-server")
        async with (
            stdio_client(server) as (reader, writer),
            ClientSession(reader, writer) as client,
        ):
            await client.initialize()
            tools = (await client.list_tools()).tools
            if not any(tool.name == "simulate_transaction" for tool in tools):
                raise RuntimeError("simulate_transaction missing from tools/list")
            print(f"Initialized MCP server and listed {len(tools)} tools.")


if __name__ == "__main__":
    asyncio.run(smoke())
