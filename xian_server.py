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
import sys
from collections import defaultdict
from typing import Any, Awaitable, Callable, Dict, List

import aiohttp
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from xian_py import Wallet, XianAsync
from xian_py.crypto import decrypt_as_receiver, encrypt
from xian_py.transaction import simulate_tx_async
from xian_py.wallet import HDWallet, verify_msg

from serialization import normalize_for_transport

# Configure logging to stderr
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("xian-server")

# Configuration
CHAIN_ID = os.environ.get("XIAN_CHAIN_ID", "xian-1")
NODE_URL = os.environ.get("XIAN_NODE_URL", "https://node.xian.org")
GRAPHQL = os.environ.get("XIAN_GRAPHQL", "https://node.xian.org/graphql")
UNSAFE_WALLET_TOOLS_ENV = "XIAN_MCP_ENABLE_UNSAFE_WALLET_TOOLS"

# === MCP APP INITIALIZATION ===
app = Server("xian")


# === HELPER: RESPONSE FORMATTING ===
def format_success_response(data: Any) -> List[TextContent]:
    """
    Wrap a successful result in MCP TextContent.
    - Dict/list values are JSON-formatted for readability.
    - Strings are passed through as-is.
    """
    data = normalize_for_transport(data)
    if isinstance(data, str):
        text = data
    else:
        try:
            text = json.dumps(data, indent=2)
        except Exception:
            text = str(data)

    return [TextContent(type="text", text=text)]


def format_error_response(error_msg: str) -> List[TextContent]:
    """Wrap an error in MCP TextContent."""
    return [TextContent(type="text", text=f"Error: {error_msg}")]


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


async def _call_indexed_read(method_name: str, /, *args: Any, **kwargs: Any) -> Any:
    async with XianAsync(NODE_URL, chain_id=CHAIN_ID) as xian:
        method = getattr(xian, method_name)
        result = await method(*args, **kwargs)
        return normalize_for_transport(result)


# === CORE TOOLS (kept compatible with existing tests) ===
async def create_wallet() -> dict[str, str] | str:
    """Create a new XIAN wallet with a random seed."""
    logger.info("Creating new wallet")

    try:
        wallet = Wallet()
        return {
            "public_key": wallet.public_key,
            "private_key": wallet.private_key,
        }
    except Exception as ex:
        logger.error("Error creating wallet: %s", ex)
        return f"❌ Error creating wallet: {str(ex)}"


async def create_wallet_from_private_key(private_key: str = "") -> dict[str, str] | str:
    """Create a wallet from an existing private key."""
    if not private_key.strip():
        return "❌ Error: Private key is required"

    logger.info("Creating wallet from private key")

    try:
        wallet = Wallet(private_key.strip())
        return {
            "public_key": wallet.public_key,
            "private_key": wallet.private_key,
        }
    except Exception as ex:
        logger.error("Error creating wallet from private key: %s", ex)
        return f"❌ Error creating wallet from private key: {str(ex)}"


async def create_hd_wallet() -> dict[str, str] | str:
    """Create a new HD wallet."""
    logger.info("Creating new HD wallet")

    try:
        hd_wallet = HDWallet()
        logger.info("Created new HD wallet")

        path = [44, 0, 0, 0, 0]  # m/44'/0'/0'/0'/0'
        xian_wallet = hd_wallet.get_wallet(path)
        eth_wallet = hd_wallet.get_ethereum_wallet(0)

        return {
            "mnemonic": hd_wallet.mnemonic_str,
            "path": str(path),
            "xian_public_key": xian_wallet.public_key,
            "xian_private_key": xian_wallet.private_key,
            "eth_public_key": eth_wallet.public_key,
            "eth_private_key": eth_wallet.private_key,
        }
    except Exception as ex:
        logger.error("Error creating HD wallet: %s", ex)
        return f"❌ Error creating HD wallet: {str(ex)}"


async def create_hd_wallet_from_mnemonic(mnemonic: str = "") -> dict[str, str] | str:
    """Restore an HD wallet from mnemonic phrase."""
    if not mnemonic.strip():
        return "❌ Error: Mnemonic is required"

    logger.info("Creating HD wallet from mnemonic")

    try:
        hd_wallet = HDWallet(mnemonic.strip())
        logger.info("Created HD wallet from mnemonic")

        path = [44, 0, 0, 0, 0]  # m/44'/0'/0'/0'/0'
        xian_wallet = hd_wallet.get_wallet(path)
        eth_wallet = hd_wallet.get_ethereum_wallet(0)

        return {
            "mnemonic": hd_wallet.mnemonic_str,
            "path": str(path),
            "xian_public_key": xian_wallet.public_key,
            "xian_private_key": xian_wallet.private_key,
            "eth_public_key": eth_wallet.public_key,
            "eth_private_key": eth_wallet.private_key,
        }
    except Exception as ex:
        logger.error("Error creating HD wallet: %s", ex)
        return f"❌ Error creating HD wallet: {str(ex)}"


async def get_balance(
    address: str = "", token_contract: str = "currency"
) -> dict[str, Any] | str:
    """Get balance for an address, optionally for a specific token contract."""
    if not address.strip():
        return "❌ Error: Address is required"

    logger.info("Getting balance for %s", address)

    try:
        async with XianAsync(NODE_URL, chain_id=CHAIN_ID) as xian:
            balance = await xian.get_balance(
                address.strip(), contract=token_contract.strip()
            )
            balance = 0 if balance is None else balance

            return normalize_for_transport(
                {
                    "address": address.strip(),
                    "token_contract": token_contract.strip(),
                    "balance": balance,
                }
            )
    except Exception as ex:
        logger.error("Error getting balance: %s", ex)
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
    except (TypeError, ValueError):
        return "❌ Error: Limit and offset must be integers"

    if limit <= 0:
        return "❌ Error: Limit must be positive"
    if offset < 0:
        return "❌ Error: Offset must be zero or greater"

    logger.info("Getting token balances for %s", address)

    try:
        async with XianAsync(NODE_URL, chain_id=CHAIN_ID) as xian:
            balances = await xian.get_token_balances(
                address.strip(),
                limit=limit,
                offset=offset,
                include_zero=include_zero,
            )
            return normalize_for_transport(balances)
    except Exception as ex:
        logger.error("Error getting token balances: %s", ex)
        return f"❌ Error getting token balances: {str(ex)}"


async def get_bds_status() -> dict[str, Any] | str:
    """Get indexed/BDS availability and synchronization status."""
    try:
        return await _call_indexed_read("get_bds_status")
    except Exception as ex:
        logger.error("Error getting BDS status: %s", ex)
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
        logger.error("Error getting developer rewards: %s", ex)
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
        logger.error("Error listing blocks: %s", ex)
        return f"❌ Error listing blocks: {str(ex)}"


async def get_block(height: int) -> dict[str, Any] | None | str:
    """Get an indexed block by height."""
    try:
        height = _parse_positive_int(height, field="Height")
    except ValueError as ex:
        return f"❌ Error: {str(ex)}"

    try:
        return await _call_indexed_read("get_block", height)
    except Exception as ex:
        logger.error("Error getting block: %s", ex)
        return f"❌ Error getting block: {str(ex)}"


async def get_block_by_hash(block_hash: str = "") -> dict[str, Any] | None | str:
    """Get an indexed block by block hash."""
    if not block_hash.strip():
        return "❌ Error: Block hash is required"

    try:
        return await _call_indexed_read("get_block_by_hash", block_hash.strip())
    except Exception as ex:
        logger.error("Error getting block by hash: %s", ex)
        return f"❌ Error getting block by hash: {str(ex)}"


async def get_indexed_tx(tx_hash: str = "") -> dict[str, Any] | None | str:
    """Get an indexed transaction by transaction hash."""
    if not tx_hash.strip():
        return "❌ Error: Transaction hash is required"

    try:
        return await _call_indexed_read("get_indexed_tx", tx_hash.strip())
    except Exception as ex:
        logger.error("Error getting indexed transaction: %s", ex)
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
        logger.error("Error listing transactions for block: %s", ex)
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
        logger.error("Error listing transactions by sender: %s", ex)
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
        logger.error("Error listing transactions by contract: %s", ex)
        return f"❌ Error listing transactions by contract: {str(ex)}"


async def get_events_for_tx(tx_hash: str = "") -> list[dict[str, Any]] | str:
    """List indexed events emitted by a transaction."""
    if not tx_hash.strip():
        return "❌ Error: Transaction hash is required"

    try:
        return await _call_indexed_read("get_events_for_tx", tx_hash.strip())
    except Exception as ex:
        logger.error("Error getting events for transaction: %s", ex)
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
        logger.error("Error listing events: %s", ex)
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
        logger.error("Error getting state history: %s", ex)
        return f"❌ Error getting state history: {str(ex)}"


async def get_state_for_tx(tx_hash: str = "") -> list[dict[str, Any]] | str:
    """List indexed state writes for a transaction hash."""
    if not tx_hash.strip():
        return "❌ Error: Transaction hash is required"

    try:
        return await _call_indexed_read("get_state_for_tx", tx_hash.strip())
    except Exception as ex:
        logger.error("Error getting state for transaction: %s", ex)
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
        logger.error("Error getting state for block: %s", ex)
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
        logger.error("Error listing shielded output tags: %s", ex)
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
        logger.error("Error listing shielded wallet history: %s", ex)
        return f"❌ Error listing shielded wallet history: {str(ex)}"


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

    logger.info("Sending transaction %s.%s", contract, function)

    try:
        wallet = Wallet(private_key.strip())
        async with XianAsync(NODE_URL, wallet=wallet, chain_id=CHAIN_ID) as xian:
            result = await xian.send_tx(contract, function, kwargs)
            return normalize_for_transport(result)
    except Exception as ex:
        logger.error("Error sending transaction: %s", ex)
        return f"❌ Error sending transaction: {str(ex)}"


async def send_tokens(
    private_key: str = "",
    to_address: str = "",
    token_contract: str = "currency",
    amount: float = 0,
) -> dict[str, str] | str:
    """Send tokens from a specific token contract to another address."""
    if not private_key.strip():
        return "❌ Error: Private key is required"
    if not token_contract.strip():
        return "❌ Error: Contract name is required"
    if not to_address.strip():
        return "❌ Error: Recipient address is required"
    if amount <= 0:
        return "❌ Error: Amount is required"

    logger.info("Sending %s %s tokens to %s", amount, token_contract, to_address)

    try:
        wallet = Wallet(private_key.strip())
        async with XianAsync(NODE_URL, wallet=wallet, chain_id=CHAIN_ID) as xian:
            result = await xian.send(amount, to_address, token_contract)
            return normalize_for_transport(result)
    except Exception as ex:
        logger.error("Error sending tokens: %s", ex)
        return f"❌ Error sending tokens: {str(ex)}"


async def get_transaction(tx_hash: str = "") -> dict[str, Any] | str:
    """Retrieve transaction from a transaction hash."""
    if not tx_hash.strip():
        return "❌ Error: Transaction hash is required"

    try:
        async with XianAsync(NODE_URL, chain_id=CHAIN_ID) as xian:
            tx = await xian.get_tx(tx_hash)
            return normalize_for_transport(tx)
    except Exception as ex:
        logger.error("Error retrieving transaction: %s", ex)
        return f"❌ Error retrieving transaction: {str(ex)}"


async def get_state(state_key: str) -> dict[str, Any] | str:
    """Get state from a contract variable."""
    if not state_key.strip():
        return "❌ Error: State key is required"

    logger.info("Getting state for key %s", state_key)

    contract, _, rest = state_key.strip().partition(".")
    parts = rest.split(":")
    variable = parts[0]
    keys = tuple(parts[1:]) if len(parts) > 1 else None

    try:
        async with XianAsync(NODE_URL, chain_id=CHAIN_ID) as xian:
            if keys:
                state = await xian.get_state(contract, variable, *keys)
            else:
                state = await xian.get_state(contract, variable)

            return {
                "state_key": state_key.strip(),
                "state_value": normalize_for_transport(state),
            }
    except Exception as ex:
        logger.error("Error getting state: %s", ex)
        return f"❌ Error getting state: {str(ex)}"


async def get_contract(contract_name: str = "") -> dict[str, str] | str:
    """Get and decompile contract source code."""
    if not contract_name.strip():
        return "❌ Error: Contract name is required"

    logger.info("Getting contract %s", contract_name)

    try:
        async with XianAsync(NODE_URL, chain_id=CHAIN_ID) as xian:
            source = await xian.get_contract(contract_name.strip())
            return {
                "contract_name": contract_name.strip(),
                "source": source,
            }
    except Exception as ex:
        logger.error("Error getting contract: %s", ex)
        return f"❌ Error getting contract: {str(ex)}"


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

    logger.info("Simulating %s.%s", contract, function)

    try:
        payload = {
            "contract": contract,
            "function": function,
            "kwargs": kwargs,
            "sender": address,
        }

        return normalize_for_transport(await simulate_tx_async(NODE_URL, payload))
    except Exception as ex:
        logger.error("Error simulating transaction: %s", ex)
        return f"❌ Error simulating transaction: {str(ex)}"


async def sign_message(
    private_key: str = "", message: str = ""
) -> dict[str, str] | str:
    """Sign a message with a wallet's private key."""
    if not private_key.strip():
        return "❌ Error: Private key is required"
    if not message.strip():
        return "❌ Error: Message is required"

    logger.info("Signing message")

    try:
        wallet = Wallet(private_key.strip())
        signature = wallet.sign_msg(message.strip())

        return {
            "signature": signature,
        }
    except Exception as ex:
        logger.error("Error signing message: %s", ex)
        return f"❌ Error signing message: {str(ex)}"


async def verify_signature(
    address: str = "", message: str = "", signature: str = ""
) -> bool | str:
    """Verify a signature for a message."""
    if not address.strip():
        return "❌ Error: Address is required"
    if not message.strip():
        return "❌ Error: Message is required"
    if not signature.strip():
        return "❌ Error: Signature is required"

    logger.info("Verifying signature")

    try:
        return verify_msg(address, message, signature)
    except Exception as ex:
        logger.error("Error verifying signature: %s", ex)
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

    logger.info("Encrypting message")

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
        logger.error("Error encrypting message: %s", ex)
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

    logger.info("Decrypting message")

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
        logger.error("Error decrypting message: %s", ex)
        return f"❌ Error decrypting message: {str(ex)}"


async def get_token_contract_by_symbol(token_symbol: str = "") -> dict[str, Any] | str:
    """Get token contract by symbol."""
    if not token_symbol.strip():
        return "❌ Error: Token symbol is required"

    try:
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
        logger.error("Error getting token contract: %s", ex)
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
          updated
        }
      }
    }
    """

    try:
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
        logger.error("Error getting token data: %s", ex)
        return f"❌ Error getting token data: {str(ex)}"


async def buy_on_dex(
    private_key: str = "",
    buy_token: str = "",
    sell_token: str = "currency",
    amount: float = 0,
    slippage: float = 1.0,
    deadline_min: float = 1.0,
) -> dict[str, Any] | str:
    """Buy tokens on the DEX."""
    if not private_key.strip():
        return "❌ Error: Private key is required"
    if not buy_token.strip():
        return "❌ Error: Buy token contract is required"
    if not sell_token.strip():
        return "❌ Error: Sell token contract is required"
    if amount <= 0:
        return "❌ Error: Amount must be positive"

    logger.info("Buying %s %s with %s", amount, buy_token, sell_token)

    try:
        return await send_transaction(
            private_key=private_key,
            contract="con_dex_noob_wrapper",
            function="buy",
            kwargs={
                "buy_token": buy_token.strip(),
                "sell_token": sell_token.strip(),
                "amount": amount,
                "slippage": slippage,
                "deadline_min": deadline_min,
            },
        )
    except Exception as ex:
        logger.error("Error buying on DEX: %s", ex)
        return f"❌ Error buying on DEX: {str(ex)}"


async def sell_on_dex(
    private_key: str = "",
    sell_token: str = "",
    buy_token: str = "currency",
    amount: float = 0,
    slippage: float = 1.0,
    deadline_min: float = 1.0,
) -> dict[str, Any] | str:
    """Sell tokens on the DEX."""
    if amount != round(amount, 8):
        amount *= 0.9999

    if not private_key.strip():
        return "❌ Error: Private key is required"
    if not sell_token.strip():
        return "❌ Error: Sell token contract is required"
    if not buy_token.strip():
        return "❌ Error: Buy token contract is required"
    if amount <= 0:
        return "❌ Error: Amount must be positive"

    logger.info("Selling %s %s for %s", amount, sell_token, buy_token)

    try:
        return await send_transaction(
            private_key=private_key,
            contract="con_dex_noob_wrapper",
            function="sell",
            kwargs={
                "sell_token": sell_token.strip(),
                "buy_token": buy_token.strip(),
                "amount": amount,
                "slippage": slippage,
                "deadline_min": deadline_min,
            },
        )
    except Exception as ex:
        logger.error("Error selling on DEX: %s", ex)
        return f"❌ Error selling on DEX: {str(ex)}"


async def get_dex_price(
    token_contract: str = "", base_contract: str = "currency"
) -> dict[str, Any] | str:
    """Get DEX price for a token against a base currency."""
    if not token_contract.strip():
        return "❌ Error: Token contract is required"

    token = token_contract.strip()
    base = base_contract.strip()

    token_a, token_b = (token, base) if token < base else (base, token)

    try:
        pair_id = await get_state(f"con_pairs.toks_to_pair:{token_a}:{token_b}")

        if isinstance(pair_id, str):
            return pair_id

        if pair_id.get("state_value") is None:
            return {
                "error": "Pair does not exist",
                "token": token,
                "base": base,
            }

        pair = pair_id["state_value"]

        reserve0 = await get_state(f"con_pairs.pairs:{pair}:reserve0")
        reserve1 = await get_state(f"con_pairs.pairs:{pair}:reserve1")

        if isinstance(reserve0, str):
            return reserve0
        if isinstance(reserve1, str):
            return reserve1

        r0 = reserve0["state_value"]
        r1 = reserve1["state_value"]

        if token_a == token:
            price = r1 / r0 if r0 > 0 else 0
        else:
            price = r0 / r1 if r1 > 0 else 0

        return normalize_for_transport(
            {
                "token": token,
                "base": base,
                "price": price,
                "pair_id": pair,
                "reserve_token": r0 if token_a == token else r1,
                "reserve_base": r1 if token_a == token else r0,
            }
        )

    except Exception as ex:
        logger.error("Error getting DEX price: %s", ex)
        return f"❌ Error getting DEX price: {str(ex)}"


# === SUPPORTING HELPERS ===
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
      tokenContracts: allContracts(
        condition: { xsc0001: true }
      ) {
        nodes {
          name
        }
      }
    }
    """

    try:
        data = await fetch_graphql(query=query)

        valid_contracts = {node["name"] for node in data["tokenContracts"]["nodes"]}

        tokens: list[dict[str, str]] = []
        for node in data["tokenSymbols"]["nodes"]:
            contract = node["key"].split(".metadata:token_symbol")[0]
            if contract in valid_contracts:
                tokens.append(
                    {
                        "token_contract": contract.lower(),
                        "token_symbol": node["value"].upper(),
                    }
                )

        return tokens

    except Exception as ex:
        logger.error("Error getting tokens: %s", ex)
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
                    error_msg = ", ".join(
                        err.get("message", str(err)) for err in result["errors"]
                    )
                    raise Exception(f"GraphQL errors: {error_msg}")

                return result.get("data", {})

        except aiohttp.ClientError as ex:
            logger.error("HTTP error fetching GraphQL: %s", ex)
            raise Exception(f"Failed to fetch GraphQL: {str(ex)}")
        except Exception as ex:
            logger.error("Error fetching GraphQL: %s", ex)
            raise


# === MCP TOOL REGISTRATION ===
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
        "description": "Create a new HD (Hierarchical Deterministic) wallet with the current xian-tech-py mnemonic defaults",
        "schema": {"type": "object", "properties": {}, "required": []},
        "unsafe": True,
        "handler": create_hd_wallet,
    },
    {
        "name": "create_hd_wallet_from_mnemonic",
        "description": "Restore an HD wallet from a mnemonic phrase (12 or 24 words)",
        "schema": {
            "type": "object",
            "properties": {
                "mnemonic": {
                    "type": "string",
                    "description": "Mnemonic phrase (space separated)",
                }
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
                    "description": "Function arguments as key-value pairs",
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
                "amount": {"type": "number", "description": "Amount of tokens to send"},
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
            "properties": {
                "tx_hash": {"type": "string", "description": "Transaction hash"}
            },
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
                    "description": "Function arguments",
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
        "name": "get_contract",
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
        "handler": get_contract,
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
        "description": "Buy tokens on the XIAN DEX",
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
                    "type": "number",
                    "description": "Amount of sell_token to spend",
                },
                "slippage": {
                    "type": "number",
                    "description": "Max slippage percentage",
                    "default": 1.0,
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
        "description": "Sell tokens on the XIAN DEX",
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
                    "type": "number",
                    "description": "Amount of sell_token to sell",
                },
                "slippage": {
                    "type": "number",
                    "description": "Max slippage percentage",
                    "default": 1.0,
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
]

TOOL_REGISTRY: Dict[str, ToolHandler] = {
    spec["name"]: spec["handler"] for spec in TOOL_SPECS
}
TOOL_SPEC_BY_NAME: Dict[str, dict[str, Any]] = {
    spec["name"]: spec for spec in TOOL_SPECS
}


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
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle tool execution via MCP."""
    spec = TOOL_SPEC_BY_NAME.get(name)
    if spec is None:
        return format_error_response(f"Unknown tool: {name}")
    if spec.get("unsafe") and not _unsafe_wallet_tools_enabled():
        return format_error_response(
            f"{name} is disabled by default. Set {UNSAFE_WALLET_TOOLS_ENV}=1 "
            "to enable unsafe wallet/signing tools on a trusted local host."
        )
    handler = TOOL_REGISTRY.get(name)
    if handler is None:
        return format_error_response(f"Unknown tool: {name}")

    try:
        result = await handler(**arguments)
    except TypeError as ex:
        logger.error("Invalid arguments for %s: %s", name, ex)
        return format_error_response(f"Invalid arguments for {name}: {ex}")
    except Exception as ex:
        logger.error("Error executing tool %s: %s", name, ex, exc_info=True)
        return format_error_response(str(ex))

    if isinstance(result, str) and result.startswith("❌"):
        return format_error_response(f"{name}: {result[2:].strip()}")

    return format_success_response(result)


# === SERVER STARTUP ===
async def main() -> None:
    logger.info("Starting XIAN Network MCP server...")
    logger.info("Chain ID: %s", CHAIN_ID)
    logger.info("Node URL: %s", NODE_URL)
    logger.info("GraphQL : %s", GRAPHQL)
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
    asyncio.run(main())


if __name__ == "__main__":
    run_cli()
