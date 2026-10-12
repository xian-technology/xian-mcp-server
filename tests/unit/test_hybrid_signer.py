import pytest
from xian_py.hybrid import AccountTransitionDraft
from xian_py.wallet import Wallet


@pytest.fixture
def custody(tmp_path):
    legacy = Wallet()
    draft = AccountTransitionDraft("local-test", legacy.public_key, 0)
    path = tmp_path / "active.json"
    path.write_text(draft.export_backup("active", "backup test password"))
    return legacy, draft, path


def test_local_configured_signer_is_explicit_and_fail_closed(custody, monkeypatch):
    from transaction_signer import signer_available, transaction_wallet

    legacy, draft, path = custody
    monkeypatch.setenv("XIAN_ACTIVE_BACKUP_FILE", str(path))
    monkeypatch.setenv("XIAN_BACKUP_PASSWORD", "backup test password")
    assert signer_available("")
    assert transaction_wallet("", "local-test", Wallet).public_key == legacy.public_key
    with pytest.raises(ValueError, match="exactly one"):
        transaction_wallet(legacy.private_key, "local-test", Wallet)
    monkeypatch.setenv("XIAN_BACKUP_PASSWORD", "incorrect")
    with pytest.raises(Exception):
        transaction_wallet("", "local-test", Wallet)
