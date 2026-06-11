# Tests

## Purpose

Pytest coverage for the MCP server's tool catalog, serialization, and
transports.

## Contents

- `unit/` — deterministic tests that run offline; CI runs these on every
  push.
- `integration/` — live-network tests against a configured Xian node; CI
  runs them on a daily schedule and via manual dispatch.
- `shared.py` — shared fixtures and the network configuration used by the
  integration suite.

## Notes

- Keep unit tests deterministic: no live RPC calls outside
  `tests/integration/`.
- Integration tests expect the environment described in `shared.py`
  (node URL, chain id, funded test key where needed).

## Next

- `uv run pytest -q tests/unit` for the fast loop;
  `uv run pytest -q tests/integration` against a live node.
