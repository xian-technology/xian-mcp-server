#!/usr/bin/env python3
"""
XIAN Network MCP Server

This server provides tools for interacting with the XIAN blockchain:
wallet management, token operations, smart contract interactions, DEX
trading helpers, and cryptographic operations.

SECURITY WARNING: This server handles private keys and should only be used locally.
Never expose this server to the internet or use it with production wallets.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal
from typing import Any, Awaitable, Callable, Dict, List

import aiohttp
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool
from xian_py import Wallet, XianAsync
from xian_py.crypto import decrypt_as_receiver, encrypt
from xian_py.transaction import simulate_tx_async
from xian_py.wallet import HDWallet, verify_msg
from xian_runtime_types.decimal import ContractingDecimal, canonical_fixed

import dex_tools
from dex_tools import DEX_TOOL_SPECS
from logging_setup import configure_logging, exception_context, request_context
from serialization import AMOUNT_SCHEMA, exact_amount, normalize_for_transport
from xian_connection import CHAIN_ID, CLIENT_CONFIG, NODE_URL

logger = logging.getLogger("xian-server")

# Configuration
GRAPHQL = os.environ.get("XIAN_GRAPHQL", "http://127.0.0.1:5000/graphql")
UNSAFE_WALLET_TOOLS_ENV = "XIAN_MCP_ENABLE_UNSAFE_WALLET_TOOLS"

# === MCP APP INITIALIZATION ===
app = Server("xian")


# === HELPER: RESPONSE FORMATTING ===
def format_success_response(data: Any) -> List[TextContent]:
    """
    Wrap a successful result in MCP TextContent.
    - Dict/list values are JSON-formatted for readability.
    - Strings are passed through as-is.
    - Types without an explicit transport encoding raise TypeError.
    """
    data = normalize_for_transport(data)
    text = data if isinstance(data, str) else json.dumps(data, indent=2)
    return [TextContent(type="text", text=text)]


def format_error_response(error_msg: str) -> CallToolResult:
    """Wrap an error in an MCP tool result flagged with isError."""
    return CallToolResult(
        content=[TextContent(type="text", text=f"Error: {error_msg}")], isError=True
    )


def _unsafe_wallet_tools_enabled() -> bool:
    value = os.environ.get(UNSAFE_WALLET_TOOLS_ENV, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as ex:
        raise ValueError(f"{field} must be an integer") from ex


def _parse_positive_int(value: Any, *, field: str) -> int:
    parsed = _parse_int(value, field=field)
    if parsed <= 0:
        raise ValueError(f"{field} must be positive")
    return parsed


def _parse_non_negative_int(value: Any, *, field: str) -> int:
    parsed = _parse_int(value, field=field)
    if parsed < 0:
        raise ValueError(f"{field} must be zero or greater")
    return parsed


def _normalize_block_ref(block_ref: str | int | None) -> str | int:
    if isinstance(block_ref, str):
        ref = block_ref.strip()
        if not ref:
            raise ValueError("Block reference is required")
        if ref.isdigit():
            return _parse_positive_int(ref, field="Block reference")
        return ref
    return _parse_positive_int(block_ref, field="Block reference")


def _include_raw_payloads() -> bool:
    value = os.environ.get("XIAN_INCLUDE_RAW", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _repair_indexed_payload(value: Any, *, include_raw: bool) -> Any:
    if isinstance(value, list):
        return [_repair_indexed_payload(item, include_raw=include_raw) for item in value]
    if not isinstance(value, dict):
        return value

    for item in value.values():
        _repair_indexed_payload(item, include_raw=include_raw)

    raw = value.get("raw")
    if isinstance(raw, dict):
        if value.get("created") is None and isinstance(raw.get("created_at"), str):
            value["created"] = raw["created_at"]
        if not include_raw:
            value.pop("raw", None)
    return value


async def _call_indexed_read(method_name: str, /, *args: Any, **kwargs: Any) -> Any:
    async with XianAsync(NODE_URL, chain_id=CHAIN_ID, config=CLIENT_CONFIG) as xian:
        method = getattr(xian, method_name)
        result = await method(*args, **kwargs)
        include_raw = _include_raw_payloads()
        normalized = normalize_for_transport(result, include_raw=True)
        return _repair_indexed_payload(normalized, include_raw=include_raw)


async def _fetch_rpc_json(path: str, params: dict[str, Any] | None = None) -> dict:
    """Fetch a CometBFT RPC endpoint from the configured node."""
    url = f"{NODE_URL.rstrip('/')}/{path.lstrip('/')}"
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url,
            params=params or {},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            response.raise_for_status()
            payload = await response.json()
            if not isinstance(payload, dict):
                raise ValueError("Unexpected RPC payload")
            if "error" in payload:
                raise ValueError(payload["error"])
            return payload


def _rpc_block_to_indexed(block_payload: dict[str, Any]) -> dict[str, Any] | None:
    result = block_payload.get("result", {})
    block = result.get("block")
    if not isinstance(block, dict):
        return None

    block_id = result.get("block_id", {})
    header = block.get("header", {})
    data = block.get("data", {})
    txs = data.get("txs") if isinstance(data, dict) else None

    raw_height = header.get("height")
    try:
        height = int(raw_height) if raw_height is not None else None
    except TypeError, ValueError:
        height = None

    return {
        "height": height,
        "block_hash": block_id.get("hash"),
        "tx_count": len(txs) if isinstance(txs, list) else 0,
        "app_hash": header.get("app_hash"),
        "block_time_iso": header.get("time"),
    }


async def _get_rpc_block(height: int) -> dict[str, Any] | None:
    return _rpc_block_to_indexed(await _fetch_rpc_json("block", params={"height": str(height)}))


async def _get_rpc_block_by_hash(block_hash: str) -> dict[str, Any] | None:
    normalized_hash = block_hash.strip()
    if not normalized_hash.lower().startswith("0x"):
        normalized_hash = f"0x{normalized_hash}"
    return _rpc_block_to_indexed(
        await _fetch_rpc_json("block_by_hash", params={"hash": normalized_hash})
    )


async def _list_rpc_blocks(limit: int, offset: int) -> list[dict[str, Any]]:
    status = await _fetch_rpc_json("status")
    sync_info = status.get("result", {}).get("sync_info", {})
    latest = int(sync_info["latest_block_height"])
    earliest = int(sync_info.get("earliest_block_height") or 1)
    start = max(latest - offset, earliest)
    heights = list(range(start, max(start - limit, earliest - 1), -1))

    semaphore = asyncio.Semaphore(10)

    async def fetch_height(height: int) -> dict[str, Any] | None:
        async with semaphore:
            return await _get_rpc_block(height)

    blocks = await asyncio.gather(*(fetch_height(height) for height in heights))
    return [block for block in blocks if block is not None]


def _graphql_transaction_node_to_dict(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "tx_hash": node.get("hash"),
        "block_height": _decode_graphql_int(node.get("blockHeight")),
        "block_hash": node.get("blockHash"),
        "sender": node.get("sender"),
        "nonce": _decode_graphql_int(node.get("nonce")),
        "contract": node.get("contract"),
        "function": node.get("function"),
        "success": node.get("success"),
        "chi_used": _decode_graphql_int(node.get("chiUsed")),
        "created": node.get("createdAt"),
    }


def _graphql_state_change_node_to_dict(node: dict[str, Any]) -> dict[str, Any]:
    tx = node.get("transactionByTxHash")
    return {
        "key": node.get("key"),
        "value": _decode_graphql_state_value(node.get("newValue")),
        "tx_hash": node.get("txHash"),
        "block_height": (
            _decode_graphql_int(tx.get("blockHeight")) if isinstance(tx, dict) else None
        ),
        "created": node.get("createdAt"),
    }


def _decode_graphql_int(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value
    return value


def _decode_graphql_state_value(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"__fixed__"}:
            return str(value["__fixed__"])
        return {key: _decode_graphql_state_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_graphql_state_value(item) for item in value]
    return value


_GRAPHQL_TX_FIELDS = """
hash
contract
function
sender
nonce
chiUsed
blockHash
blockHeight
success
createdAt
"""


_GRAPHQL_STATE_CHANGE_FIELDS = """
key
newValue
txHash
createdAt
transactionByTxHash {
  blockHeight
}
"""


async def _graphql_get_indexed_tx(tx_hash: str) -> dict[str, Any] | None:
    query = f"""
    query GetIndexedTx($hash: String!) {{
      transactionByHash(hash: $hash) {{
        {_GRAPHQL_TX_FIELDS}
      }}
    }}
    """
    data = await fetch_graphql(query=query, variables={"hash": tx_hash})
    node = data.get("transactionByHash")
    if not isinstance(node, dict):
        return None
    return _graphql_transaction_node_to_dict(node)


async def _graphql_list_txs_by_condition(
    condition: dict[str, Any],
    *,
    limit: int,
    offset: int = 0,
) -> list[dict[str, Any]]:
    query = f"""
    query ListTransactions(
      $condition: TransactionCondition!
      $limit: Int!
      $offset: Int!
    ) {{
      allTransactions(
        first: $limit
        offset: $offset
        orderBy: BLOCK_HEIGHT_DESC
        condition: $condition
      ) {{
        nodes {{
          {_GRAPHQL_TX_FIELDS}
        }}
      }}
    }}
    """
    data = await fetch_graphql(
        query=query,
        variables={"condition": condition, "limit": limit, "offset": offset},
    )
    nodes = data.get("allTransactions", {}).get("nodes", [])
    return [_graphql_transaction_node_to_dict(node) for node in nodes if isinstance(node, dict)]


async def _graphql_get_state_changes(
    *,
    condition: dict[str, Any],
    limit: int,
    offset: int = 0,
) -> list[dict[str, Any]]:
    query = f"""
    query ListStateChanges(
      $condition: StateChangeCondition!
      $limit: Int!
      $offset: Int!
    ) {{
      allStateChanges(
        first: $limit
        offset: $offset
        orderBy: CHANGE_ID_DESC
        condition: $condition
      ) {{
        nodes {{
          {_GRAPHQL_STATE_CHANGE_FIELDS}
        }}
      }}
    }}
    """
    data = await fetch_graphql(
        query=query,
        variables={"condition": condition, "limit": limit, "offset": offset},
    )
    nodes = data.get("allStateChanges", {}).get("nodes", [])
    return [_graphql_state_change_node_to_dict(node) for node in nodes if isinstance(node, dict)]


async def _graphql_get_state_for_block(
    block_ref: str | int,
) -> list[dict[str, Any]]:
    if isinstance(block_ref, int):
        tx_condition: dict[str, Any] = {"blockHeight": block_ref}
    else:
        tx_condition = {"blockHash": block_ref.removeprefix("0x").lower()}

    query = f"""
    query StateForBlock($condition: TransactionCondition!) {{
      allTransactions(
        first: 250
        orderBy: BLOCK_HEIGHT_DESC
        condition: $condition
      ) {{
        nodes {{
          stateChangesByTxHash(first: 1000, orderBy: CHANGE_ID_ASC) {{
            nodes {{
              {_GRAPHQL_STATE_CHANGE_FIELDS}
            }}
          }}
        }}
      }}
    }}
    """
    data = await fetch_graphql(query=query, variables={"condition": tx_condition})
    tx_nodes = data.get("allTransactions", {}).get("nodes", [])
    changes: list[dict[str, Any]] = []
    for tx_node in tx_nodes:
        if not isinstance(tx_node, dict):
            continue
        state_nodes = (
            tx_node.get("stateChangesByTxHash", {}).get("nodes", [])
            if isinstance(tx_node.get("stateChangesByTxHash"), dict)
            else []
        )
        changes.extend(
            _graphql_state_change_node_to_dict(node)
            for node in state_nodes
            if isinstance(node, dict)
        )
    return changes


async def _graphql_get_contract_source(contract_name: str) -> str | None:
    query = """
    query ContractSource($name: String!) {
      contractByName(name: $name) {
        source
      }
    }
    """
    data = await fetch_graphql(query=query, variables={"name": contract_name})
    node = data.get("contractByName")
    if not isinstance(node, dict):
        return None
    source = node.get("source")
    return source if isinstance(source, str) else None


async def _graphql_get_token_balances(
    address: str,
    *,
    limit: int,
    offset: int,
    include_zero: bool,
) -> dict[str, Any]:
    query = """
    query TokenBalances($suffix: String!) {
      allStates(
        first: 1000
        orderBy: KEY_ASC
        filter: {key: {endsWith: $suffix}}
      ) {
        nodes {
          key
          value
          updatedAt
        }
      }
    }
    """
    suffix = f".balances:{address}"
    data = await fetch_graphql(query=query, variables={"suffix": suffix})
    nodes = data.get("allStates", {}).get("nodes", [])

    def is_zero(value: Any) -> bool:
        if value is None:
            return True
        value = _decode_graphql_state_value(value)
        try:
            return Decimal(str(value)) == 0
        except Exception:
            return False

    items: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        key = node.get("key")
        if not isinstance(key, str) or ".balances:" not in key:
            continue
        value = node.get("value")
        decoded_value = _decode_graphql_state_value(value)
        if not include_zero and is_zero(decoded_value):
            continue
        contract = key.split(".balances:", 1)[0]
        items.append(
            {
                "contract": contract,
                "balance": None if decoded_value is None else str(decoded_value),
                "name": None,
                "symbol": None,
                "logo_url": None,
                "last_tx_hash": None,
                "last_block_height": None,
                "updated_at": node.get("updatedAt"),
            }
        )

    paged_items = items[offset : offset + limit]
    return {
        "available": True,
        "address": address,
        "total": len(items),
        "limit": limit,
        "offset": offset,
        "items": paged_items,
    }


async def _graphql_get_developer_rewards(recipient_key: str) -> dict[str, Any]:
    query = """
    query DeveloperRewards($key: String!) {
      allRewards(
        first: 1000
        orderBy: ROW_ID_ASC
        condition: {type: "developer", recipientKey: $key}
      ) {
        nodes {
          txHash
          value
          createdAt
          transactionByTxHash {
            blockHeight
            contract
          }
        }
      }
    }
    """
    data = await fetch_graphql(query=query, variables={"key": recipient_key})
    nodes = [node for node in data.get("allRewards", {}).get("nodes", []) if isinstance(node, dict)]
    # At most 1,000 rewards of 61 integer digits and 30 decimal places.
    reward_context = Context(prec=94)
    total = Decimal("0")
    tx_hashes: set[str] = set()
    contracts: set[str] = set()
    heights: list[int] = []
    created_values: list[str] = []

    for node in nodes:
        try:
            total = reward_context.add(total, exact_amount(node.get("value", "0"), field="value"))
        except Exception:
            pass
        tx_hash = node.get("txHash")
        if isinstance(tx_hash, str):
            tx_hashes.add(tx_hash)
        created = node.get("createdAt")
        if isinstance(created, str):
            created_values.append(created)
        tx = node.get("transactionByTxHash")
        if isinstance(tx, dict):
            height = tx.get("blockHeight")
            height = _decode_graphql_int(height)
            if isinstance(height, int) and not isinstance(height, bool):
                heights.append(height)
            contract = tx.get("contract")
            if isinstance(contract, str):
                contracts.add(contract)

    return {
        "recipient_key": recipient_key,
        "total_rewards": format(total, "f"),
        "reward_count": len(nodes),
        "tx_count": len(tx_hashes),
        "contract_count": len(contracts),
        "first_block_height": min(heights) if heights else None,
        "last_block_height": max(heights) if heights else None,
        "first_reward_at": created_values[0] if created_values else None,
        "last_reward_at": created_values[-1] if created_values else None,
    }


# === CORE TOOLS (kept compatible with existing tests) ===
async def create_wallet() -> dict[str, str] | str:
    """Create a new XIAN wallet with a random seed."""
    logger.debug("Creating new wallet")

    try:
        wallet = Wallet()
        return {
            "public_key": wallet.public_key,
            "private_key": wallet.private_key,
        }
    except Exception as ex:
        logger.error("Error creating wallet", extra=exception_context(ex))
        return f"❌ Error creating wallet: {str(ex)}"


async def create_wallet_from_private_key(private_key: str = "") -> dict[str, str] | str:
    """Create a wallet from an existing private key."""
    if not private_key.strip():
        return "❌ Error: Private key is required"

    logger.debug("Creating wallet from private key")

    try:
        wallet = Wallet(private_key.strip())
        return {
            "public_key": wallet.public_key,
            "private_key": wallet.private_key,
        }
    except Exception as ex:
        logger.error("Error creating wallet from private key", extra=exception_context(ex))
        return f"❌ Error creating wallet from private key: {str(ex)}"


def _hd_wallet_result(hd_wallet: HDWallet, account_index: int) -> dict[str, str | int]:
    xian_wallet = Wallet.from_mnemonic(hd_wallet.mnemonic_str, account_index=account_index)
    eth_wallet = hd_wallet.get_ethereum_wallet()
    return {
        "mnemonic": hd_wallet.mnemonic_str,
        "account_index": account_index,
        "ethereum_account_index": 0,
        "xian_public_key": xian_wallet.public_key,
        "xian_private_key": xian_wallet.private_key,
        "eth_public_key": eth_wallet.address,
        "eth_private_key": eth_wallet.private_key,
    }


async def create_hd_wallet(account_index: int = 0) -> dict[str, str | int] | str:
    """Create a mnemonic wallet at the selected account index."""
    logger.debug("Creating new HD wallet")

    try:
        return _hd_wallet_result(HDWallet(), account_index)
    except Exception as ex:
        logger.error("Error creating HD wallet", extra=exception_context(ex))
        return f"❌ Error creating HD wallet: {str(ex)}"


async def create_hd_wallet_from_mnemonic(
    mnemonic: str = "", account_index: int = 0
) -> dict[str, str | int] | str:
    """Restore a mnemonic wallet at the selected account index."""
    mnemonic = " ".join(mnemonic.strip().lower().split())
    if not mnemonic:
        return "❌ Error: Mnemonic is required"

    logger.debug("Creating HD wallet from mnemonic")

    try:
        return _hd_wallet_result(HDWallet(mnemonic), account_index)
    except Exception as ex:
        logger.error("Error creating HD wallet", extra=exception_context(ex))
        return f"❌ Error creating HD wallet: {str(ex)}"


async def get_balance(address: str = "", token_contract: str = "currency") -> dict[str, Any] | str:
    """Get balance for an address, optionally for a specific token contract."""
    if not address.strip():
        return "❌ Error: Address is required"

    logger.debug("Getting balance for %s", address)

    try:
        async with XianAsync(NODE_URL, chain_id=CHAIN_ID, config=CLIENT_CONFIG) as xian:
            balance = await xian.get_balance(address.strip(), contract=token_contract.strip())
            balance = 0 if balance is None else balance

            return normalize_for_transport(
                {
                    "address": address.strip(),
                    "token_contract": token_contract.strip(),
                    "balance": balance,
                }
            )
    except Exception as ex:
        logger.error("Error getting balance", extra=exception_context(ex))
        return f"❌ Error getting balance: {str(ex)}"


async def get_token_balances(
    address: str = "",
    limit: int = 100,
    offset: int = 0,
    include_zero: bool = False,
) -> dict[str, Any] | str:
    """List all token balances for an address."""
    if not address.strip():
        return "❌ Error: Address is required"

    try:
        limit = int(limit)
        offset = int(offset)
    except TypeError, ValueError:
        return "❌ Error: Limit and offset must be integers"

    if limit <= 0:
        return "❌ Error: Limit must be positive"
    if offset < 0:
        return "❌ Error: Offset must be zero or greater"

    logger.debug("Getting token balances for %s", address)

    try:
        async with XianAsync(NODE_URL, chain_id=CHAIN_ID, config=CLIENT_CONFIG) as xian:
            balances = await xian.get_token_balances(
                address.strip(),
                limit=limit,
                offset=offset,
                include_zero=include_zero,
            )
            return normalize_for_transport(balances)
    except Exception as ex:
        logger.debug(
            "BDS token balances unavailable, falling back to GraphQL", extra=exception_context(ex)
        )

    try:
        return await _graphql_get_token_balances(
            address.strip(),
            limit=limit,
            offset=offset,
            include_zero=include_zero,
        )
    except Exception as ex:
        logger.error("Error getting token balances", extra=exception_context(ex))
        return f"❌ Error getting token balances: {str(ex)}"


async def get_bds_status() -> dict[str, Any] | str:
    """Get indexed/BDS availability and synchronization status."""
    try:
        return await _call_indexed_read("get_bds_status")
    except Exception as ex:
        logger.debug(
            "BDS status unavailable, falling back to RPC status", extra=exception_context(ex)
        )

    try:
        status = await _fetch_rpc_json("status")
        sync_info = status.get("result", {}).get("sync_info", {})
        latest_height = sync_info.get("latest_block_height")
        try:
            latest_height = int(latest_height) if latest_height is not None else None
        except TypeError, ValueError:
            latest_height = None
        return {
            "available": False,
            "worker_running": False,
            "catchup_running": False,
            "catching_up": bool(sync_info.get("catching_up")),
            "queue_depth": 0,
            "current_block_height": latest_height,
            "height_lag": None,
            "indexed_height": None,
            "spool_pending_count": 0,
            "alerts": [
                {
                    "level": "warning",
                    "message": "BDS ABCI read surface is unavailable on this node",
                }
            ],
        }
    except Exception as ex:
        logger.error("Error getting BDS status", extra=exception_context(ex))
        return f"❌ Error getting BDS status: {str(ex)}"


async def get_developer_rewards(recipient_key: str = "") -> dict[str, Any] | str:
    """Get indexed developer reward totals for a recipient key."""
    if not recipient_key.strip():
        return "❌ Error: Recipient key is required"

    try:
        return await _call_indexed_read(
            "get_developer_rewards",
            recipient_key.strip(),
        )
    except Exception as ex:
        logger.debug(
            "BDS developer rewards unavailable, falling back to GraphQL",
            extra=exception_context(ex),
        )

    try:
        return await _graphql_get_developer_rewards(recipient_key.strip())
    except Exception as ex:
        logger.error("Error getting developer rewards", extra=exception_context(ex))
        return f"❌ Error getting developer rewards: {str(ex)}"


async def list_blocks(
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]] | str:
    """List indexed blocks with pagination."""
    try:
        limit = _parse_positive_int(limit, field="Limit")
        offset = _parse_non_negative_int(offset, field="Offset")
    except ValueError as ex:
        return f"❌ Error: {str(ex)}"

    try:
        return await _call_indexed_read(
            "list_blocks",
            limit=limit,
            offset=offset,
        )
    except Exception as ex:
        logger.debug(
            "BDS block listing unavailable, falling back to RPC", extra=exception_context(ex)
        )

    try:
        return await _list_rpc_blocks(limit=limit, offset=offset)
    except Exception as ex:
        logger.error("Error listing blocks", extra=exception_context(ex))
        return f"❌ Error listing blocks: {str(ex)}"


async def get_block(height: int) -> dict[str, Any] | None | str:
    """Get an indexed block by height."""
    try:
        height = _parse_positive_int(height, field="Height")
    except ValueError as ex:
        return f"❌ Error: {str(ex)}"

    try:
        result = await _call_indexed_read("get_block", height)
        if result is not None:
            return result
    except Exception as ex:
        logger.debug(
            "BDS block lookup unavailable, falling back to RPC", extra=exception_context(ex)
        )

    try:
        return await _get_rpc_block(height)
    except Exception as ex:
        logger.error("Error getting block", extra=exception_context(ex))
        return f"❌ Error getting block: {str(ex)}"


async def get_block_by_hash(block_hash: str = "") -> dict[str, Any] | None | str:
    """Get an indexed block by block hash."""
    if not block_hash.strip():
        return "❌ Error: Block hash is required"

    try:
        result = await _call_indexed_read("get_block_by_hash", block_hash.strip())
        if result is not None:
            return result
    except Exception as ex:
        logger.debug(
            "BDS block hash lookup unavailable, falling back to RPC", extra=exception_context(ex)
        )

    try:
        return await _get_rpc_block_by_hash(block_hash.strip())
    except Exception as ex:
        logger.error("Error getting block by hash", extra=exception_context(ex))
        return f"❌ Error getting block by hash: {str(ex)}"


async def get_indexed_tx(tx_hash: str = "") -> dict[str, Any] | None | str:
    """Get an indexed transaction by transaction hash."""
    if not tx_hash.strip():
        return "❌ Error: Transaction hash is required"

    try:
        result = await _call_indexed_read("get_indexed_tx", tx_hash.strip())
        if result is not None:
            return result
    except Exception as ex:
        logger.debug(
            "BDS transaction lookup unavailable, falling back to GraphQL",
            extra=exception_context(ex),
        )

    try:
        return await _graphql_get_indexed_tx(tx_hash.strip())
    except Exception as ex:
        logger.error("Error getting indexed transaction", extra=exception_context(ex))
        return f"❌ Error getting indexed transaction: {str(ex)}"


async def list_txs_for_block(
    block_ref: str | int | None = None,
) -> list[dict[str, Any]] | str:
    """List indexed transactions for a block reference."""
    try:
        block_ref = _normalize_block_ref(block_ref)
    except ValueError as ex:
        return f"❌ Error: {str(ex)}"

    try:
        return await _call_indexed_read("list_txs_for_block", block_ref)
    except Exception as ex:
        logger.debug(
            "BDS block transaction list unavailable, falling back to GraphQL",
            extra=exception_context(ex),
        )

    try:
        condition = (
            {"blockHeight": block_ref}
            if isinstance(block_ref, int)
            else {"blockHash": block_ref.removeprefix("0x").lower()}
        )
        return await _graphql_list_txs_by_condition(
            condition,
            limit=1000,
            offset=0,
        )
    except Exception as ex:
        logger.error("Error listing transactions for block", extra=exception_context(ex))
        return f"❌ Error listing transactions for block: {str(ex)}"


async def list_txs_by_sender(
    sender: str = "",
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]] | str:
    """List indexed transactions for a sender."""
    if not sender.strip():
        return "❌ Error: Sender is required"

    try:
        limit = _parse_positive_int(limit, field="Limit")
        offset = _parse_non_negative_int(offset, field="Offset")
    except ValueError as ex:
        return f"❌ Error: {str(ex)}"

    try:
        return await _call_indexed_read(
            "list_txs_by_sender",
            sender.strip(),
            limit=limit,
            offset=offset,
        )
    except Exception as ex:
        logger.debug(
            "BDS sender transaction list unavailable, falling back to GraphQL",
            extra=exception_context(ex),
        )

    try:
        return await _graphql_list_txs_by_condition(
            {"sender": sender.strip()},
            limit=limit,
            offset=offset,
        )
    except Exception as ex:
        logger.error("Error listing transactions by sender", extra=exception_context(ex))
        return f"❌ Error listing transactions by sender: {str(ex)}"


async def list_txs_by_contract(
    contract: str = "",
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]] | str:
    """List indexed transactions for a contract."""
    if not contract.strip():
        return "❌ Error: Contract is required"

    try:
        limit = _parse_positive_int(limit, field="Limit")
        offset = _parse_non_negative_int(offset, field="Offset")
    except ValueError as ex:
        return f"❌ Error: {str(ex)}"

    try:
        return await _call_indexed_read(
            "list_txs_by_contract",
            contract.strip(),
            limit=limit,
            offset=offset,
        )
    except Exception as ex:
        logger.debug(
            "BDS contract transaction list unavailable, falling back to GraphQL",
            extra=exception_context(ex),
        )

    try:
        return await _graphql_list_txs_by_condition(
            {"contract": contract.strip()},
            limit=limit,
            offset=offset,
        )
    except Exception as ex:
        logger.error("Error listing transactions by contract", extra=exception_context(ex))
        return f"❌ Error listing transactions by contract: {str(ex)}"


async def get_events_for_tx(tx_hash: str = "") -> list[dict[str, Any]] | str:
    """List indexed events emitted by a transaction."""
    if not tx_hash.strip():
        return "❌ Error: Transaction hash is required"

    try:
        return await _call_indexed_read("get_events_for_tx", tx_hash.strip())
    except Exception as ex:
        logger.error("Error getting events for transaction", extra=exception_context(ex))
        return f"❌ Error getting events for transaction: {str(ex)}"


async def list_events(
    contract: str = "",
    event: str = "",
    limit: int = 100,
    offset: int = 0,
    after_id: int | None = None,
) -> list[dict[str, Any]] | str:
    """List indexed events for a contract/event name."""
    if not contract.strip():
        return "❌ Error: Contract is required"
    if not event.strip():
        return "❌ Error: Event is required"

    try:
        limit = _parse_positive_int(limit, field="Limit")
        offset = _parse_non_negative_int(offset, field="Offset")
        if after_id is not None:
            after_id = _parse_non_negative_int(after_id, field="After ID")
    except ValueError as ex:
        return f"❌ Error: {str(ex)}"

    try:
        return await _call_indexed_read(
            "list_events",
            contract.strip(),
            event.strip(),
            limit=limit,
            offset=offset,
            after_id=after_id,
        )
    except Exception as ex:
        logger.error("Error listing events", extra=exception_context(ex))
        return f"❌ Error listing events: {str(ex)}"


async def get_state_history(
    key: str = "",
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]] | str:
    """List indexed state history for a key."""
    if not key.strip():
        return "❌ Error: State key is required"

    try:
        limit = _parse_positive_int(limit, field="Limit")
        offset = _parse_non_negative_int(offset, field="Offset")
    except ValueError as ex:
        return f"❌ Error: {str(ex)}"

    try:
        return await _call_indexed_read(
            "get_state_history",
            key.strip(),
            limit=limit,
            offset=offset,
        )
    except Exception as ex:
        logger.debug(
            "BDS state history unavailable, falling back to GraphQL", extra=exception_context(ex)
        )

    try:
        return await _graphql_get_state_changes(
            condition={"key": key.strip()},
            limit=limit,
            offset=offset,
        )
    except Exception as ex:
        logger.error("Error getting state history", extra=exception_context(ex))
        return f"❌ Error getting state history: {str(ex)}"


async def get_state_for_tx(tx_hash: str = "") -> list[dict[str, Any]] | str:
    """List indexed state writes for a transaction hash."""
    if not tx_hash.strip():
        return "❌ Error: Transaction hash is required"

    try:
        return await _call_indexed_read("get_state_for_tx", tx_hash.strip())
    except Exception as ex:
        logger.debug(
            "BDS state-for-tx unavailable, falling back to GraphQL", extra=exception_context(ex)
        )

    try:
        return await _graphql_get_state_changes(
            condition={"txHash": tx_hash.strip()},
            limit=1000,
            offset=0,
        )
    except Exception as ex:
        logger.error("Error getting state for transaction", extra=exception_context(ex))
        return f"❌ Error getting state for transaction: {str(ex)}"


async def get_state_for_block(
    block_ref: str | int | None = None,
) -> list[dict[str, Any]] | str:
    """List indexed state writes for a block reference."""
    try:
        block_ref = _normalize_block_ref(block_ref)
    except ValueError as ex:
        return f"❌ Error: {str(ex)}"

    try:
        return await _call_indexed_read("get_state_for_block", block_ref)
    except Exception as ex:
        logger.debug(
            "BDS state-for-block unavailable, falling back to GraphQL", extra=exception_context(ex)
        )

    try:
        return await _graphql_get_state_for_block(block_ref)
    except Exception as ex:
        logger.error("Error getting state for block", extra=exception_context(ex))
        return f"❌ Error getting state for block: {str(ex)}"


async def list_shielded_output_tags(
    tag_value: str = "",
    kind: str = "sync_hint",
    limit: int = 100,
    offset: int = 0,
    after_id: int | None = None,
) -> list[dict[str, Any]] | str:
    """List indexed shielded output tags for a wallet sync tag."""
    if not tag_value.strip():
        return "❌ Error: Tag value is required"
    if not kind.strip():
        return "❌ Error: Kind is required"

    try:
        limit = _parse_positive_int(limit, field="Limit")
        offset = _parse_non_negative_int(offset, field="Offset")
        if after_id is not None:
            after_id = _parse_non_negative_int(after_id, field="After ID")
    except ValueError as ex:
        return f"❌ Error: {str(ex)}"

    try:
        return await _call_indexed_read(
            "list_shielded_output_tags",
            tag_value.strip(),
            kind=kind.strip(),
            limit=limit,
            offset=offset,
            after_id=after_id,
        )
    except Exception as ex:
        status = await get_bds_status()
        if isinstance(status, dict) and status.get("available") is False:
            return "❌ Error: BDS shielded output tag index is unavailable on the configured node"
        logger.error("Error listing shielded output tags", extra=exception_context(ex))
        return f"❌ Error listing shielded output tags: {str(ex)}"


async def list_shielded_wallet_history(
    tag_value: str = "",
    kind: str = "sync_hint",
    limit: int = 100,
    after_note_index: int = 0,
) -> list[dict[str, Any]] | str:
    """List indexed shielded wallet history for a wallet sync tag."""
    if not tag_value.strip():
        return "❌ Error: Tag value is required"
    if not kind.strip():
        return "❌ Error: Kind is required"

    try:
        limit = _parse_positive_int(limit, field="Limit")
        after_note_index = _parse_non_negative_int(
            after_note_index,
            field="After note index",
        )
    except ValueError as ex:
        return f"❌ Error: {str(ex)}"

    try:
        return await _call_indexed_read(
            "list_shielded_wallet_history",
            tag_value.strip(),
            kind=kind.strip(),
            limit=limit,
            after_note_index=after_note_index,
        )
    except Exception as ex:
        status = await get_bds_status()
        if isinstance(status, dict) and status.get("available") is False:
            return (
                "❌ Error: BDS shielded wallet history index is unavailable on the configured node"
            )
        logger.error("Error listing shielded wallet history", extra=exception_context(ex))
        return f"❌ Error listing shielded wallet history: {str(ex)}"


def _accepted_submission(submission: Any) -> dict[str, Any] | str:
    """A CheckTx rejection is a tool error; accepted or pending submissions pass through."""
    result = normalize_for_transport(submission)
    if result["accepted"] is False:
        return f"❌ Error: transaction rejected at CheckTx: {result['message']}"
    return result


async def send_transaction(
    private_key: str = "",
    contract: str = "",
    function: str = "",
    kwargs: dict[str, Any] | None = None,
) -> dict[str, str] | str:
    """Send a transaction to the Xian Network."""
    if not private_key.strip():
        return "❌ Error: Private key is required"
    if not contract.strip():
        return "❌ Error: Contract name is required"
    if not function.strip():
        return "❌ Error: Function name is required"
    if kwargs is None:
        kwargs = {}

    logger.debug("Sending transaction %s.%s", contract, function)

    try:
        wallet = Wallet(private_key.strip())
        async with XianAsync(
            NODE_URL, wallet=wallet, chain_id=CHAIN_ID, config=CLIENT_CONFIG
        ) as xian:
            return _accepted_submission(await xian.send_tx(contract, function, kwargs))
    except Exception as ex:
        logger.error("Error sending transaction", extra=exception_context(ex))
        return f"❌ Error sending transaction: {str(ex)}"


async def send_tokens(
    private_key: str = "",
    to_address: str = "",
    token_contract: str = "currency",
    amount: str | int = 0,
) -> dict[str, str] | str:
    """Send tokens from a specific token contract to another address."""
    if not private_key.strip():
        return "❌ Error: Private key is required"
    if not token_contract.strip():
        return "❌ Error: Contract name is required"
    if not to_address.strip():
        return "❌ Error: Recipient address is required"
    try:
        amount = exact_amount(amount, field="Amount", positive=True)
    except ValueError as exc:
        return f"❌ Error: {exc}"

    logger.debug("Sending %s %s tokens to %s", amount, token_contract, to_address)

    try:
        wallet = Wallet(private_key.strip())
        async with XianAsync(
            NODE_URL, wallet=wallet, chain_id=CHAIN_ID, config=CLIENT_CONFIG
        ) as xian:
            return _accepted_submission(await xian.send(amount, to_address, token_contract))
    except Exception as ex:
        logger.error("Error sending tokens", extra=exception_context(ex))
        return f"❌ Error sending tokens: {str(ex)}"


async def get_transaction(tx_hash: str = "") -> dict[str, Any] | str:
    """Retrieve transaction from a transaction hash."""
    if not tx_hash.strip():
        return "❌ Error: Transaction hash is required"

    try:
        async with XianAsync(NODE_URL, chain_id=CHAIN_ID, config=CLIENT_CONFIG) as xian:
            tx = await xian.get_tx(tx_hash)
            return normalize_for_transport(tx)
    except Exception as ex:
        logger.error("Error retrieving transaction", extra=exception_context(ex))
        return f"❌ Error retrieving transaction: {str(ex)}"


async def get_state(state_key: str) -> dict[str, Any] | str:
    """Get state from a contract variable."""
    if not state_key.strip():
        return "❌ Error: State key is required"

    logger.debug("Getting state for key %s", state_key)

    contract, _, rest = state_key.strip().partition(".")
    parts = rest.split(":")
    variable = parts[0]
    keys = tuple(parts[1:]) if len(parts) > 1 else None

    try:
        async with XianAsync(NODE_URL, chain_id=CHAIN_ID, config=CLIENT_CONFIG) as xian:
            if keys:
                state = await xian.get_state(contract, variable, *keys)
            else:
                state = await xian.get_state(contract, variable)

            return {
                "state_key": state_key.strip(),
                "state_value": normalize_for_transport(state),
            }
    except Exception as ex:
        logger.error("Error getting state", extra=exception_context(ex))
        return f"❌ Error getting state: {str(ex)}"


async def get_contract_source(contract_name: str = "") -> dict[str, str] | str:
    """Get contract source code."""
    if not contract_name.strip():
        return "❌ Error: Contract name is required"

    logger.debug("Getting contract source %s", contract_name)

    try:
        async with XianAsync(NODE_URL, chain_id=CHAIN_ID, config=CLIENT_CONFIG) as xian:
            source = await xian.get_contract_source(contract_name.strip())
            if source is None:
                source = await _graphql_get_contract_source(contract_name.strip())
            return {
                "contract_name": contract_name.strip(),
                "source": source,
            }
    except Exception as ex:
        logger.error("Error getting contract source", extra=exception_context(ex))
        return f"❌ Error getting contract source: {str(ex)}"


async def simulate_transaction(
    address: str = "",
    contract: str = "",
    function: str = "",
    kwargs: dict[str, Any] | None = None,
) -> dict[str, Any] | str:
    """Simulate a transaction to estimate chi or execute read-only functions."""
    if not address.strip():
        return "❌ Error: Address is required"
    if not contract.strip():
        return "❌ Error: Contract name is required"
    if not function.strip():
        return "❌ Error: Function name is required"
    if kwargs is None:
        kwargs = {}

    logger.debug("Simulating %s.%s", contract, function)

    try:
        payload = {
            "contract": contract,
            "function": function,
            "kwargs": kwargs,
            "sender": address,
        }

        return normalize_for_transport(
            await simulate_tx_async(NODE_URL, payload, vm_profile=CLIENT_CONFIG.vm_profile)
        )
    except Exception as ex:
        logger.error("Error simulating transaction", extra=exception_context(ex))
        return f"❌ Error simulating transaction: {str(ex)}"


async def sign_message(private_key: str = "", message: str = "") -> dict[str, str] | str:
    """Sign a message with a wallet's private key."""
    if not private_key.strip():
        return "❌ Error: Private key is required"
    if not message.strip():
        return "❌ Error: Message is required"

    logger.debug("Signing message")

    try:
        wallet = Wallet(private_key.strip())
        signature = wallet.sign_msg(message)

        return {
            "signature": signature,
        }
    except Exception as ex:
        logger.error("Error signing message", extra=exception_context(ex))
        return f"❌ Error signing message: {str(ex)}"


async def verify_signature(address: str = "", message: str = "", signature: str = "") -> bool | str:
    """Verify a signature for a message."""
    if not address.strip():
        return "❌ Error: Address is required"
    if not message.strip():
        return "❌ Error: Message is required"
    if not signature.strip():
        return "❌ Error: Signature is required"

    logger.debug("Verifying signature")

    try:
        return verify_msg(address, message, signature)
    except Exception as ex:
        logger.error("Error verifying signature", extra=exception_context(ex))
        return f"❌ Error verifying signature: {str(ex)}"


async def encrypt_message(
    sender_private_key: str = "",
    receiver_public_key: str = "",
    message: str = "",
) -> dict[str, str] | str:
    """Encrypt a message between sender and receiver."""
    if not sender_private_key.strip():
        return "❌ Error: Sender private key is required"
    if not receiver_public_key.strip():
        return "❌ Error: Receiver public key is required"
    if not message.strip():
        return "❌ Error: Message is required"

    logger.debug("Encrypting message")

    try:
        sender_wallet = Wallet(sender_private_key.strip())

        encrypted = encrypt(
            sender_private_key.strip(),
            receiver_public_key.strip(),
            message.strip(),
        )

        return {
            "sender_public_key": sender_wallet.public_key.strip(),
            "receiver_public_key": receiver_public_key.strip(),
            "encrypted_message": encrypted,
        }
    except Exception as ex:
        logger.error("Error encrypting message", extra=exception_context(ex))
        return f"❌ Error encrypting message: {str(ex)}"


async def decrypt_message(
    receiver_private_key: str = "",
    sender_public_key: str = "",
    encrypted_message: str = "",
) -> dict[str, str] | str:
    """Decrypt a message as either sender or receiver."""
    if not receiver_private_key.strip():
        return "❌ Error: Private key is required"
    if not sender_public_key.strip():
        return "❌ Error: Other party's public key is required"
    if not encrypted_message.strip():
        return "❌ Error: Encrypted message is required"

    logger.debug("Decrypting message")

    try:
        receiver_wallet = Wallet(receiver_private_key.strip())

        decrypted = decrypt_as_receiver(
            sender_public_key.strip(),
            receiver_private_key.strip(),
            encrypted_message.strip(),
        )

        return {
            "receiver_public_key": receiver_wallet.public_key.strip(),
            "sender_public_key": sender_public_key.strip(),
            "decrypted_message": decrypted,
        }
    except Exception as ex:
        logger.error("Error decrypting message", extra=exception_context(ex))
        return f"❌ Error decrypting message: {str(ex)}"


async def get_token_contract_by_symbol(token_symbol: str = "") -> dict[str, Any] | str:
    """Get token contract by symbol."""
    if not token_symbol.strip():
        return "❌ Error: Token symbol is required"

    try:
        try:
            tokens = await _bds_get_tokens()
        except Exception as ex:
            logger.debug(
                "BDS token contract listing unavailable, falling back to GraphQL",
                extra=exception_context(ex),
            )
            tokens = await get_tokens()

        symbols = defaultdict(list)
        for tkn in tokens:
            symbols[tkn["token_symbol"]].append(tkn["token_contract"])

        contracts = symbols.get(token_symbol.strip().upper(), [])

        if not contracts:
            return {
                "token_contracts": [],
                "count": 0,
                "message": "No token found with this symbol",
            }
        if len(contracts) == 1:
            return {
                "token_contracts": contracts,
                "count": 1,
            }
        return {
            "token_contracts": contracts,
            "count": len(contracts),
            "message": "Multiple tokens found with this symbol",
        }
    except Exception as ex:
        logger.error("Error getting token contract", extra=exception_context(ex))
        return f"❌ Error getting token contract: {str(ex)}"


async def get_token_data_by_contract(token_contract: str = "") -> dict[str, Any] | str:
    """Get token data by contract."""
    if not token_contract.strip():
        return "❌ Error: Token contract is required"

    contract = token_contract.strip()

    query = """
    query GetTokenDetails(
      $operator: String!,
      $logoUrl: String!,
      $name: String!,
      $symbol: String!,
      $website: String!) {
      tokenStates: allStates(
        filter: {
          or: [
            { key: { equalTo: $operator } },
            { key: { equalTo: $logoUrl } },
            { key: { equalTo: $name } },
            { key: { equalTo: $symbol } },
            { key: { equalTo: $website } }
          ]
        }
      ) {
        nodes {
          key
          value
          updatedAt
        }
      }
    }
    """

    try:
        try:
            return await _bds_get_token_data(contract)
        except Exception as ex:
            logger.debug(
                "BDS token metadata unavailable, falling back to GraphQL",
                extra=exception_context(ex),
            )

        data = await fetch_graphql(
            query=query,
            variables={
                "operator": f"{contract}.metadata:operator",
                "logoUrl": f"{contract}.metadata:token_logo_url",
                "name": f"{contract}.metadata:token_name",
                "symbol": f"{contract}.metadata:token_symbol",
                "website": f"{contract}.metadata:token_website",
            },
        )
        return data

    except Exception as ex:
        logger.error("Error getting token data", extra=exception_context(ex))
        return f"❌ Error getting token data: {str(ex)}"


def _deadline_value(*, minutes_from_now: float) -> dict[str, list[int]]:
    future = datetime.now(UTC) + timedelta(minutes=minutes_from_now)
    return {
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


async def _helper_trade(
    *,
    side: str,
    private_key: str,
    sell_token: str,
    buy_token: str,
    amount: str | int,
    slippage: str | int,
    deadline_min: float,
) -> dict[str, Any] | str:
    """Quote, bound and submit a con_dex_helper buy or sell."""
    try:
        amount_value = exact_amount(amount, field="Amount", positive=True)
        slippage_value = exact_amount(slippage, field="Slippage")
        account = Wallet(private_key.strip()).public_key
        bounds = await dex_tools.helper_trade_bounds(
            side=side,
            sell_token=sell_token,
            buy_token=buy_token,
            amount=amount_value,
            slippage_percent=slippage_value,
            account=account,
        )
    except ValueError as exc:
        return f"❌ Error: {exc}"

    bound_name = bounds.pop("bound_name")
    bound = bounds.pop("bound")
    # Signed payloads use sorted keys, so kwargs order carries no meaning.
    result = await send_transaction(
        private_key=private_key,
        contract="con_dex_helper",
        function=side,
        kwargs={
            "sell_token": sell_token,
            "buy_token": buy_token,
            "amount": amount_value,
            bound_name: bound,
            "deadline": _deadline_value(minutes_from_now=deadline_min),
        },
    )
    if isinstance(result, dict):
        result["trade_bounds"] = {
            **{key: canonical_fixed(value) for key, value in bounds.items()},
            bound_name: canonical_fixed(bound),
        }
    return result


# side -> (debug log template, verb for unexpected failures)
_HELPER_TRADE_WORDING = {
    "buy": ("Buying %s %s with %s", "buying"),
    "sell": ("Selling %s %s for %s", "selling"),
}


async def _requested_helper_trade(
    *,
    side: str,
    private_key: str,
    sell_token: str,
    buy_token: str,
    amount: str | int,
    slippage: str | int,
    deadline_min: float,
) -> dict[str, Any] | str:
    """Check a buy/sell tool request, then run it; failures become error text.

    The traded token (bought or sold) is checked before the counter token.
    """
    traded, counter = ("Buy", buy_token), ("Sell", sell_token)
    if side == "sell":
        traded, counter = counter, traded
    if not private_key.strip():
        return "❌ Error: Private key is required"
    for label, token in (traded, counter):
        if not token.strip():
            return f"❌ Error: {label} token contract is required"

    log_template, verb = _HELPER_TRADE_WORDING[side]
    logger.debug(log_template, amount, traded[1], counter[1])
    try:
        return await _helper_trade(
            side=side,
            private_key=private_key,
            sell_token=sell_token.strip(),
            buy_token=buy_token.strip(),
            amount=amount,
            slippage=slippage,
            deadline_min=deadline_min,
        )
    except Exception as ex:
        logger.error(f"Error {verb} on DEX", extra=exception_context(ex))
        return f"❌ Error {verb} on DEX: {str(ex)}"


async def buy_on_dex(
    private_key: str = "",
    buy_token: str = "",
    sell_token: str = "currency",
    amount: str | int = 0,
    slippage: str | int = 1,
    deadline_min: float = 1.0,
) -> dict[str, Any] | str:
    """Buy an exact output amount; the signed max_input bounds the price."""
    return await _requested_helper_trade(
        side="buy",
        private_key=private_key,
        sell_token=sell_token,
        buy_token=buy_token,
        amount=amount,
        slippage=slippage,
        deadline_min=deadline_min,
    )


async def sell_on_dex(
    private_key: str = "",
    sell_token: str = "",
    buy_token: str = "currency",
    amount: str | int = 0,
    slippage: str | int = 1,
    deadline_min: float = 1.0,
) -> dict[str, Any] | str:
    """Sell an exact input amount; the signed min_out bounds the price."""
    return await _requested_helper_trade(
        side="sell",
        private_key=private_key,
        sell_token=sell_token,
        buy_token=buy_token,
        amount=amount,
        slippage=slippage,
        deadline_min=deadline_min,
    )


async def get_dex_price(
    token_contract: str = "", base_contract: str = "currency"
) -> dict[str, Any] | str:
    """Get DEX price for a token against a base currency."""
    if not token_contract.strip():
        return "❌ Error: Token contract is required"
    try:
        return await _dex_price(token_contract.strip(), base_contract.strip())
    except Exception as ex:
        logger.error("Error getting DEX price", extra=exception_context(ex))
        return f"❌ Error getting DEX price: {str(ex)}"


async def _dex_price(token: str, base: str) -> dict[str, Any] | str:
    token_a, token_b = sorted((token, base))
    pair_id = await get_state(f"con_pairs.toks_to_pair:{token_a}:{token_b}")
    if isinstance(pair_id, str):
        return pair_id
    pair = pair_id.get("state_value")
    if pair is None:
        return f"❌ Error: No DEX pair exists for {token}/{base}"

    reserves = [await get_state(f"con_pairs.pairs:{pair}:reserve{side}") for side in (0, 1)]
    failures = [reserve for reserve in reserves if isinstance(reserve, str)]
    if failures:
        return failures[0]
    reserve0, reserve1 = (_reserve_value(reserve) for reserve in reserves)
    reserve_token, reserve_base = (reserve0, reserve1) if token_a == token else (reserve1, reserve0)
    return normalize_for_transport(
        {
            "token": token,
            "base": base,
            "price": reserve_base / reserve_token if reserve_token > 0 else 0,
            "pair_id": pair,
            "reserve_token": reserve_token,
            "reserve_base": reserve_base,
        }
    )


def _reserve_value(result: dict[str, Any]) -> ContractingDecimal:
    value = result["state_value"]
    if isinstance(value, dict) and set(value) == {"__big_int__"}:
        value = value["__big_int__"]
    return ContractingDecimal(value)


# === SUPPORTING HELPERS ===
def _metadata_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
        if decoded is None:
            return None
        if isinstance(decoded, (str, int, float, bool)):
            return str(decoded)
        return json.dumps(decoded, separators=(",", ":"))
    return str(value)


async def _bds_get_token_contract_page(
    xian: XianAsync,
    *,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    get_token_contracts = getattr(xian, "get_token_contracts", None)
    if callable(get_token_contracts):
        page = await get_token_contracts(limit=limit, offset=offset)
        return {
            "items": [
                {
                    "contract": item.contract,
                    "symbol": item.symbol,
                }
                for item in page.items
            ],
            "total": page.total,
        }

    # Older xian-tech-py versions expose the generic ABCI helper but not this
    # specific BDS convenience method.
    payload = await xian._abci_query_value(f"/token_contracts/limit={limit}/offset={offset}")
    if not isinstance(payload, dict):
        raise ValueError("Unexpected token contracts payload")
    return payload


async def _bds_get_tokens(*, page_size: int = 1000) -> list[dict[str, str]]:
    tokens: list[dict[str, str]] = []
    offset = 0

    async with XianAsync(NODE_URL, chain_id=CHAIN_ID, config=CLIENT_CONFIG) as xian:
        while True:
            page = await _bds_get_token_contract_page(
                xian,
                limit=page_size,
                offset=offset,
            )
            items = [item for item in page.get("items", []) if isinstance(item, dict)]
            for item in items:
                contract = _metadata_text(item.get("contract"))
                symbol = _metadata_text(item.get("symbol"))
                if contract and symbol:
                    tokens.append(
                        {
                            "token_contract": contract.lower(),
                            "token_symbol": symbol.upper(),
                        }
                    )

            offset += len(items)
            total = int(page.get("total", offset))
            if not items or offset >= total:
                break

    return tokens


async def _bds_get_token_data(contract: str) -> dict[str, Any]:
    state_keys = [
        f"{contract}.metadata:operator",
        f"{contract}.metadata:token_logo_url",
        f"{contract}.metadata:token_name",
        f"{contract}.metadata:token_symbol",
        f"{contract}.metadata:token_website",
    ]

    nodes: list[dict[str, Any]] = []
    async with XianAsync(NODE_URL, chain_id=CHAIN_ID, config=CLIENT_CONFIG) as xian:
        for state_key in state_keys:
            state_contract, _, rest = state_key.partition(".")
            variable, _, state_subkey = rest.partition(":")
            value = (
                await xian.get_state(state_contract, variable, state_subkey)
                if state_subkey
                else await xian.get_state(state_contract, variable)
            )
            if value is not None:
                nodes.append(
                    {
                        "key": state_key,
                        "value": normalize_for_transport(value),
                        "updatedAt": None,
                    }
                )

    if not nodes:
        raise ValueError(f"No token metadata found for {contract}")

    return {"tokenStates": {"nodes": nodes}}


async def get_tokens() -> list[dict]:
    """Get all tokens with their contract name and symbol."""
    query = """
    query GetTokenContractsWithSymbols {
      tokenSymbols: allStates(
        filter: {
          key: { endsWith: ".metadata:token_symbol" }
        }
        orderBy: KEY_ASC
      ) {
        nodes {
          key
          value
        }
      }
    }
    """

    try:
        data = await fetch_graphql(query=query)

        tokens: list[dict[str, str]] = []
        for node in data["tokenSymbols"]["nodes"]:
            contract = node["key"].split(".metadata:token_symbol")[0]
            tokens.append(
                {
                    "token_contract": contract.lower(),
                    "token_symbol": str(node["value"]).upper(),
                }
            )

        return tokens

    except Exception as ex:
        logger.error("Error getting tokens", extra=exception_context(ex))
        raise


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
    endpoint = endpoint or GRAPHQL

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


# === MCP TOOL REGISTRATION ===

_ACCOUNT_INDEX_SCHEMA = {
    "type": "integer",
    "minimum": 0,
    "maximum": 2**32 - 1,
    "default": 0,
    "description": "Xian account index; the Ethereum helper always uses index 0",
}
ToolHandler = Callable[..., Awaitable[Any]]

TOOL_SPECS: List[dict[str, Any]] = [
    {
        "name": "create_wallet",
        "description": "Create a new random XIAN wallet with public/private key pair",
        "schema": {"type": "object", "properties": {}, "required": []},
        "unsafe": True,
        "handler": create_wallet,
    },
    {
        "name": "create_wallet_from_private_key",
        "description": "Import a wallet from an existing private key",
        "schema": {
            "type": "object",
            "properties": {
                "private_key": {
                    "type": "string",
                    "description": "The private key (64 hex characters)",
                },
            },
            "required": ["private_key"],
        },
        "unsafe": True,
        "handler": create_wallet_from_private_key,
    },
    {
        "name": "create_hd_wallet",
        "description": "Create a mnemonic wallet using browser/mobile-compatible indexed Xian derivation",
        "schema": {
            "type": "object",
            "properties": {"account_index": _ACCOUNT_INDEX_SCHEMA},
            "required": [],
        },
        "unsafe": True,
        "handler": create_hd_wallet,
    },
    {
        "name": "create_hd_wallet_from_mnemonic",
        "description": "Restore a BIP39 mnemonic wallet at the selected account index",
        "schema": {
            "type": "object",
            "properties": {
                "mnemonic": {
                    "type": "string",
                    "description": "Mnemonic phrase (space separated)",
                },
                "account_index": _ACCOUNT_INDEX_SCHEMA,
            },
            "required": ["mnemonic"],
        },
        "unsafe": True,
        "handler": create_hd_wallet_from_mnemonic,
    },
    {
        "name": "get_balance",
        "description": "Check the balance of a XIAN address for a specific token",
        "schema": {
            "type": "object",
            "properties": {
                "address": {
                    "type": "string",
                    "description": "The XIAN address to check (64 hex characters)",
                },
                "token_contract": {
                    "type": "string",
                    "description": "The token contract name (default: 'currency')",
                    "default": "currency",
                },
            },
            "required": ["address"],
        },
        "handler": get_balance,
    },
    {
        "name": "get_token_balances",
        "description": "List all token balances for a XIAN address with pagination support",
        "schema": {
            "type": "object",
            "properties": {
                "address": {
                    "type": "string",
                    "description": "The XIAN address to inspect (64 hex characters)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of token balances to return",
                    "default": 100,
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset",
                    "default": 0,
                },
                "include_zero": {
                    "type": "boolean",
                    "description": "Include tokens whose current balance is zero",
                    "default": False,
                },
            },
            "required": ["address"],
        },
        "handler": get_token_balances,
    },
    {
        "name": "get_bds_status",
        "description": "Get indexed/BDS availability and synchronization status for the connected node",
        "schema": {"type": "object", "properties": {}, "required": []},
        "handler": get_bds_status,
    },
    {
        "name": "get_developer_rewards",
        "description": "Get indexed developer reward totals for a recipient key",
        "schema": {
            "type": "object",
            "properties": {
                "recipient_key": {
                    "type": "string",
                    "description": "Developer reward recipient key or address",
                }
            },
            "required": ["recipient_key"],
        },
        "handler": get_developer_rewards,
    },
    {
        "name": "list_blocks",
        "description": "List indexed blocks with pagination",
        "schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of blocks to return",
                    "default": 100,
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset",
                    "default": 0,
                },
            },
            "required": [],
        },
        "handler": list_blocks,
    },
    {
        "name": "get_block",
        "description": "Get an indexed block by height",
        "schema": {
            "type": "object",
            "properties": {
                "height": {
                    "type": "integer",
                    "description": "Block height",
                }
            },
            "required": ["height"],
        },
        "handler": get_block,
    },
    {
        "name": "get_block_by_hash",
        "description": "Get an indexed block by block hash",
        "schema": {
            "type": "object",
            "properties": {
                "block_hash": {
                    "type": "string",
                    "description": "Block hash",
                }
            },
            "required": ["block_hash"],
        },
        "handler": get_block_by_hash,
    },
    {
        "name": "get_indexed_tx",
        "description": "Get an indexed transaction by transaction hash",
        "schema": {
            "type": "object",
            "properties": {
                "tx_hash": {
                    "type": "string",
                    "description": "Transaction hash",
                }
            },
            "required": ["tx_hash"],
        },
        "handler": get_indexed_tx,
    },
    {
        "name": "list_txs_for_block",
        "description": "List indexed transactions for a block height or block hash",
        "schema": {
            "type": "object",
            "properties": {
                "block_ref": {
                    "anyOf": [
                        {"type": "integer"},
                        {"type": "string"},
                    ],
                    "description": "Block height or block hash",
                }
            },
            "required": ["block_ref"],
        },
        "handler": list_txs_for_block,
    },
    {
        "name": "list_txs_by_sender",
        "description": "List indexed transactions submitted by a sender with pagination",
        "schema": {
            "type": "object",
            "properties": {
                "sender": {
                    "type": "string",
                    "description": "Sender address or public key",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of transactions to return",
                    "default": 100,
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset",
                    "default": 0,
                },
            },
            "required": ["sender"],
        },
        "handler": list_txs_by_sender,
    },
    {
        "name": "list_txs_by_contract",
        "description": "List indexed transactions that called a contract with pagination",
        "schema": {
            "type": "object",
            "properties": {
                "contract": {
                    "type": "string",
                    "description": "Contract name",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of transactions to return",
                    "default": 100,
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset",
                    "default": 0,
                },
            },
            "required": ["contract"],
        },
        "handler": list_txs_by_contract,
    },
    {
        "name": "get_events_for_tx",
        "description": "List indexed events emitted by a transaction",
        "schema": {
            "type": "object",
            "properties": {
                "tx_hash": {
                    "type": "string",
                    "description": "Transaction hash",
                }
            },
            "required": ["tx_hash"],
        },
        "handler": get_events_for_tx,
    },
    {
        "name": "list_events",
        "description": "List indexed events for a contract and event name",
        "schema": {
            "type": "object",
            "properties": {
                "contract": {
                    "type": "string",
                    "description": "Contract name",
                },
                "event": {
                    "type": "string",
                    "description": "Event name",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of events to return",
                    "default": 100,
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset when after_id is not set",
                    "default": 0,
                },
                "after_id": {
                    "type": "integer",
                    "description": "Cursor-based pagination anchor; overrides offset when set",
                },
            },
            "required": ["contract", "event"],
        },
        "handler": list_events,
    },
    {
        "name": "get_state_history",
        "description": "List indexed history entries for a state key",
        "schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "State key in 'contract.variable:key' form",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of history entries to return",
                    "default": 100,
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset",
                    "default": 0,
                },
            },
            "required": ["key"],
        },
        "handler": get_state_history,
    },
    {
        "name": "get_state_for_tx",
        "description": "List indexed state writes produced by a transaction",
        "schema": {
            "type": "object",
            "properties": {
                "tx_hash": {
                    "type": "string",
                    "description": "Transaction hash",
                }
            },
            "required": ["tx_hash"],
        },
        "handler": get_state_for_tx,
    },
    {
        "name": "get_state_for_block",
        "description": "List indexed state writes produced by a block height or block hash",
        "schema": {
            "type": "object",
            "properties": {
                "block_ref": {
                    "anyOf": [
                        {"type": "integer"},
                        {"type": "string"},
                    ],
                    "description": "Block height or block hash",
                }
            },
            "required": ["block_ref"],
        },
        "handler": get_state_for_block,
    },
    {
        "name": "list_shielded_output_tags",
        "description": "List indexed shielded output tags for a wallet sync tag",
        "schema": {
            "type": "object",
            "properties": {
                "tag_value": {
                    "type": "string",
                    "description": "Wallet sync tag value",
                },
                "kind": {
                    "type": "string",
                    "description": "Tag kind",
                    "default": "sync_hint",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of tag entries to return",
                    "default": 100,
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset when after_id is not set",
                    "default": 0,
                },
                "after_id": {
                    "type": "integer",
                    "description": "Cursor-based pagination anchor; overrides offset when set",
                },
            },
            "required": ["tag_value"],
        },
        "handler": list_shielded_output_tags,
    },
    {
        "name": "list_shielded_wallet_history",
        "description": "List indexed shielded wallet history for a wallet sync tag",
        "schema": {
            "type": "object",
            "properties": {
                "tag_value": {
                    "type": "string",
                    "description": "Wallet sync tag value",
                },
                "kind": {
                    "type": "string",
                    "description": "History kind",
                    "default": "sync_hint",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of history entries to return",
                    "default": 100,
                },
                "after_note_index": {
                    "type": "integer",
                    "description": "Cursor for note history pagination",
                    "default": 0,
                },
            },
            "required": ["tag_value"],
        },
        "handler": list_shielded_wallet_history,
    },
    {
        "name": "send_transaction",
        "description": "Send a transaction to execute a smart contract function",
        "schema": {
            "type": "object",
            "properties": {
                "private_key": {
                    "type": "string",
                    "description": "Private key of the sender",
                },
                "contract": {"type": "string", "description": "Contract name to call"},
                "function": {
                    "type": "string",
                    "description": "Function name to execute",
                },
                "kwargs": {
                    "type": "object",
                    "description": 'Function arguments. Encode decimals as {"__fixed__":"1.25"} and large integers as {"__big_int__":"9007199254740993"}.',
                    "default": {},
                },
            },
            "required": ["private_key", "contract", "function"],
        },
        "unsafe": True,
        "handler": send_transaction,
    },
    {
        "name": "send_tokens",
        "description": "Send tokens from one address to another",
        "schema": {
            "type": "object",
            "properties": {
                "private_key": {
                    "type": "string",
                    "description": "Private key of the sender",
                },
                "to_address": {"type": "string", "description": "Recipient address"},
                "token_contract": {
                    "type": "string",
                    "description": "Token contract name (default: 'currency')",
                    "default": "currency",
                },
                "amount": {
                    **AMOUNT_SCHEMA,
                    "description": "Exact token amount as a decimal string or integer",
                },
            },
            "required": ["private_key", "to_address", "amount"],
        },
        "unsafe": True,
        "handler": send_tokens,
    },
    {
        "name": "get_transaction",
        "description": "Get details of a transaction by its hash",
        "schema": {
            "type": "object",
            "properties": {"tx_hash": {"type": "string", "description": "Transaction hash"}},
            "required": ["tx_hash"],
        },
        "handler": get_transaction,
    },
    {
        "name": "simulate_transaction",
        "description": "Simulate a transaction to estimate chi without executing it",
        "schema": {
            "type": "object",
            "properties": {
                "address": {
                    "type": "string",
                    "description": "Sender address for simulation",
                },
                "contract": {"type": "string", "description": "Contract name"},
                "function": {"type": "string", "description": "Function name"},
                "kwargs": {
                    "type": "object",
                    "description": 'Function arguments. Encode decimals as {"__fixed__":"1.25"} and large integers as {"__big_int__":"9007199254740993"}.',
                    "default": {},
                },
            },
            "required": ["address", "contract", "function"],
        },
        "handler": simulate_transaction,
    },
    {
        "name": "get_state",
        "description": "Read a state variable from a smart contract",
        "schema": {
            "type": "object",
            "properties": {
                "state_key": {
                    "type": "string",
                    "description": "State key 'contract.variable:key'",
                }
            },
            "required": ["state_key"],
        },
        "handler": get_state,
    },
    {
        "name": "get_contract_source",
        "description": "Get the source code of a smart contract",
        "schema": {
            "type": "object",
            "properties": {
                "contract_name": {
                    "type": "string",
                    "description": "Name of the contract",
                }
            },
            "required": ["contract_name"],
        },
        "handler": get_contract_source,
    },
    {
        "name": "get_token_contract_by_symbol",
        "description": "Find the contract address for a token by its symbol",
        "schema": {
            "type": "object",
            "properties": {
                "token_symbol": {
                    "type": "string",
                    "description": "Token symbol (e.g., 'XIAN')",
                }
            },
            "required": ["token_symbol"],
        },
        "handler": get_token_contract_by_symbol,
    },
    {
        "name": "get_token_data_by_contract",
        "description": "Get metadata for a token by its contract address",
        "schema": {
            "type": "object",
            "properties": {
                "token_contract": {
                    "type": "string",
                    "description": "Token contract name",
                }
            },
            "required": ["token_contract"],
        },
        "handler": get_token_data_by_contract,
    },
    {
        "name": "buy_on_dex",
        "description": (
            "Buy an exact output amount of tokens on the XIAN DEX through con_dex_helper. "
            "The server quotes the direct pair and signs max_input = quoted input plus "
            "slippage; the trade reverts if the input would exceed it. Requires a "
            "sell_token allowance to con_dex_helper of at least max_input."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "private_key": {
                    "type": "string",
                    "description": "Private key for signing the transaction",
                },
                "buy_token": {"type": "string", "description": "Token contract to buy"},
                "sell_token": {
                    "type": "string",
                    "description": "Token contract to sell",
                    "default": "currency",
                },
                "amount": {
                    **AMOUNT_SCHEMA,
                    "description": "Desired amount of buy_token to receive (output amount)",
                },
                "slippage": {
                    **AMOUNT_SCHEMA,
                    "description": (
                        "Slippage tolerance in percent (0 to below 100) applied to a fresh "
                        "quote to derive the signed price bound"
                    ),
                    "default": 1,
                },
                "deadline_min": {
                    "type": "number",
                    "description": "Deadline in minutes",
                    "default": 1.0,
                },
            },
            "required": ["private_key", "buy_token", "amount"],
        },
        "unsafe": True,
        "handler": buy_on_dex,
    },
    {
        "name": "sell_on_dex",
        "description": (
            "Sell an exact input amount of tokens on the XIAN DEX through con_dex_helper. "
            "The server quotes the direct pair and signs min_out = quoted output minus "
            "slippage; the trade reverts if the received output would be lower. Requires "
            "a sell_token allowance to con_dex_helper of at least amount."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "private_key": {
                    "type": "string",
                    "description": "Private key for signing the transaction",
                },
                "sell_token": {
                    "type": "string",
                    "description": "Token contract to sell",
                },
                "buy_token": {
                    "type": "string",
                    "description": "Token contract to receive",
                    "default": "currency",
                },
                "amount": {
                    **AMOUNT_SCHEMA,
                    "description": "Amount of sell_token to sell",
                },
                "slippage": {
                    **AMOUNT_SCHEMA,
                    "description": (
                        "Slippage tolerance in percent (0 to below 100) applied to a fresh "
                        "quote to derive the signed price bound"
                    ),
                    "default": 1,
                },
                "deadline_min": {
                    "type": "number",
                    "description": "Deadline in minutes",
                    "default": 1.0,
                },
            },
            "required": ["private_key", "sell_token", "amount"],
        },
        "unsafe": True,
        "handler": sell_on_dex,
    },
    {
        "name": "get_dex_price",
        "description": "Get the current price of a token on the DEX",
        "schema": {
            "type": "object",
            "properties": {
                "token_contract": {
                    "type": "string",
                    "description": "Token contract to get price for",
                },
                "base_contract": {
                    "type": "string",
                    "description": "Base token to price against (default: 'currency')",
                    "default": "currency",
                },
            },
            "required": ["token_contract"],
        },
        "handler": get_dex_price,
    },
    {
        "name": "sign_message",
        "description": "Sign a message with a private key",
        "schema": {
            "type": "object",
            "properties": {
                "private_key": {
                    "type": "string",
                    "description": "Private key to sign with",
                },
                "message": {"type": "string", "description": "Message to sign"},
            },
            "required": ["private_key", "message"],
        },
        "unsafe": True,
        "handler": sign_message,
    },
    {
        "name": "verify_signature",
        "description": "Verify a message signature",
        "schema": {
            "type": "object",
            "properties": {
                "address": {
                    "type": "string",
                    "description": "Address that allegedly signed the message",
                },
                "message": {"type": "string", "description": "Original message"},
                "signature": {"type": "string", "description": "Signature to verify"},
            },
            "required": ["address", "message", "signature"],
        },
        "handler": verify_signature,
    },
    {
        "name": "encrypt_message",
        "description": "Encrypt a message from one party to another",
        "schema": {
            "type": "object",
            "properties": {
                "sender_private_key": {
                    "type": "string",
                    "description": "Sender's private key",
                },
                "receiver_public_key": {
                    "type": "string",
                    "description": "Receiver's public key",
                },
                "message": {"type": "string", "description": "Message to encrypt"},
            },
            "required": ["sender_private_key", "receiver_public_key", "message"],
        },
        "unsafe": True,
        "handler": encrypt_message,
    },
    {
        "name": "decrypt_message",
        "description": "Decrypt a received encrypted message",
        "schema": {
            "type": "object",
            "properties": {
                "receiver_private_key": {
                    "type": "string",
                    "description": "Receiver's private key",
                },
                "sender_public_key": {
                    "type": "string",
                    "description": "Sender's public key",
                },
                "encrypted_message": {
                    "type": "string",
                    "description": "Encrypted message to decrypt",
                },
            },
            "required": [
                "receiver_private_key",
                "sender_public_key",
                "encrypted_message",
            ],
        },
        "unsafe": True,
        "handler": decrypt_message,
    },
] + DEX_TOOL_SPECS

TOOL_REGISTRY: Dict[str, ToolHandler] = {spec["name"]: spec["handler"] for spec in TOOL_SPECS}
TOOL_SPEC_BY_NAME: Dict[str, dict[str, Any]] = {spec["name"]: spec for spec in TOOL_SPECS}


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List all available tools for MCP clients."""
    return [
        Tool(
            name=spec["name"],
            description=spec["description"],
            inputSchema=spec["schema"],
        )
        for spec in TOOL_SPECS
        if not spec.get("unsafe") or _unsafe_wallet_tools_enabled()
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent] | CallToolResult:
    """Handle tool execution via MCP."""
    spec = TOOL_SPEC_BY_NAME.get(name)
    if spec is None:
        return format_error_response(f"Unknown tool: {name}")
    if spec.get("unsafe") and not _unsafe_wallet_tools_enabled():
        return format_error_response(
            f"{name} is disabled by default. Set {UNSAFE_WALLET_TOOLS_ENV}=1 "
            "to enable unsafe wallet/signing tools on a trusted local host."
        )
    with request_context(name):
        try:
            result = await TOOL_REGISTRY[name](**arguments)
        except TypeError as ex:
            logger.warning(
                "Invalid tool arguments",
                extra={**exception_context(ex), "tool": name, "reason": "invalid_arguments"},
            )
            return format_error_response(f"Invalid arguments for {name}: {ex}")
        except Exception as ex:
            logger.error(
                "Tool execution failed",
                extra={**exception_context(ex), "tool": name, "reason": "tool_execution_failed"},
            )
            return format_error_response(str(ex))

        try:
            return _tool_result(name, result)
        except TypeError as ex:
            logger.error(
                "Tool result has no transport encoding",
                extra={**exception_context(ex), "tool": name, "reason": "unencodable_result"},
            )
            return format_error_response(f"{name}: {ex}")


def _tool_result(name: str, result: Any) -> list[TextContent] | CallToolResult:
    if isinstance(result, str) and result.startswith("❌"):
        return format_error_response(f"{name}: {result[2:].strip()}")
    if isinstance(result, dict) and result.get("ok") is False:
        # DEX tools refuse with a structured {"ok": false, "error": ...} body.
        return CallToolResult(content=format_success_response(result), isError=True)
    return format_success_response(result)


# === SERVER STARTUP ===
async def main() -> None:
    logger.info("Starting XIAN Network MCP server...")
    logger.info("Chain ID: %s", CHAIN_ID)
    logger.info(
        "Unsafe wallet tools enabled: %s",
        _unsafe_wallet_tools_enabled(),
    )

    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


def run_cli() -> None:
    configure_logging()
    asyncio.run(main())


if __name__ == "__main__":
    run_cli()
