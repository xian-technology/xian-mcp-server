"""Connection settings shared by MCP and HTTP tools for the fresh chain."""

import os

from xian_py import XianClientConfig

CHAIN_ID = os.environ.get("XIAN_CHAIN_ID", "xian-local-1")
NODE_URL = os.environ.get("XIAN_NODE_URL", "http://127.0.0.1:26657")
CLIENT_CONFIG = XianClientConfig(vm_profile="xian_vm_v2")
