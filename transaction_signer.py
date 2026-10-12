"""Explicit local service custody; never enroll or generate recovery keys."""

import os

from xian_py.signers import load_signing_wallet


def signer_available(private_key: str) -> bool:
    return bool(private_key.strip() or os.environ.get("XIAN_ACTIVE_BACKUP_FILE"))


def transaction_wallet(private_key: str, chain_id: str, legacy_factory):
    backup = os.environ.get("XIAN_ACTIVE_BACKUP_FILE")
    if not backup:
        return legacy_factory(private_key.strip())
    return load_signing_wallet(
        private_key=private_key.strip() or None,
        active_backup_file=backup,
        backup_password=os.environ.get("XIAN_BACKUP_PASSWORD"),
        expected_address=os.environ.get("XIAN_SIGNER_ADDRESS"),
        chain_id=chain_id,
    )
