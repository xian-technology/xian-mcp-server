"""Connection settings shared by MCP and HTTP tools for the fresh chain."""

import logging
import os

import aiohttp
from xian_py import XianClientConfig

from logging_setup import exception_context

CHAIN_ID = os.environ.get("XIAN_CHAIN_ID", "xian-local-1")
NODE_URL = os.environ.get("XIAN_NODE_URL", "http://127.0.0.1:26657")
GRAPHQL_URL = os.environ.get("XIAN_GRAPHQL", "http://127.0.0.1:5000/graphql")
CLIENT_CONFIG = XianClientConfig(vm_profile="xian_vm_v2")

logger = logging.getLogger(__name__)


async def fetch_graphql(
    query: str,
    variables: dict | None = None,
    endpoint: str | None = None,
    headers: dict | None = None,
    timeout: float = 5.0,
) -> dict:
    """
    Execute a GraphQL query and return the results.
    """
    variables = variables or {}
    endpoint = endpoint or GRAPHQL_URL

    default_headers = {"Content-Type": "application/json"}
    if headers:
        default_headers.update(headers)

    payload = {"query": query, "variables": variables}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                endpoint,
                json=payload,
                headers=default_headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                response.raise_for_status()
                result = await response.json()

                if "errors" in result:
                    error_msg = ", ".join(err.get("message", str(err)) for err in result["errors"])
                    raise Exception(f"GraphQL errors: {error_msg}")

                return result.get("data", {})

        except aiohttp.ClientError as ex:
            logger.error("HTTP error fetching GraphQL", extra=exception_context(ex))
            raise Exception(f"Failed to fetch GraphQL: {str(ex)}")
        except Exception as ex:
            logger.error("Error fetching GraphQL", extra=exception_context(ex))
            raise
