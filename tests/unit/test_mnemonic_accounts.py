import pytest
from xian_py import Wallet
from xian_py.wallet import HDWallet

from xian_server import TOOL_SPECS, create_hd_wallet, create_hd_wallet_from_mnemonic

MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
)


@pytest.mark.parametrize(
    ("account_index", "expected_address"),
    [
        (0, "7e15e2e068cb41b5c4b51dafef23d944aa7d120d362097d1f7c02100da5c2be5"),
        (1, "6cb37dd4f3acf8152c4d083bba33ce7dc2cf410fa3bab94694ef17aa5616f1ad"),
    ],
)
async def test_mnemonic_import_matches_browser_mobile_accounts(account_index, expected_address):
    result = await create_hd_wallet_from_mnemonic(MNEMONIC, account_index=account_index)

    assert isinstance(result, dict)
    assert result["xian_public_key"] == expected_address
    assert result["account_index"] == account_index
    assert "path" not in result


async def test_default_import_restores_first_indexed_account():
    result = await create_hd_wallet_from_mnemonic(MNEMONIC)

    assert isinstance(result, dict)
    assert (
        result["xian_public_key"]
        == "7e15e2e068cb41b5c4b51dafef23d944aa7d120d362097d1f7c02100da5c2be5"
    )
    assert result["account_index"] == 0


async def test_created_mnemonic_restores_same_indexed_account():
    created = await create_hd_wallet(account_index=1)

    assert isinstance(created, dict)
    restored = await create_hd_wallet_from_mnemonic(created["mnemonic"], account_index=1)
    expected = Wallet.from_mnemonic(created["mnemonic"], account_index=1)
    assert created == restored
    assert created["xian_public_key"] == expected.public_key
    assert created["xian_private_key"] == expected.private_key


async def test_created_mnemonic_defaults_to_first_indexed_account():
    created = await create_hd_wallet()

    assert isinstance(created, dict)
    assert created["account_index"] == 0
    assert created == await create_hd_wallet_from_mnemonic(created["mnemonic"])


@pytest.mark.parametrize("account_index", [1, 2**32 - 1])
async def test_xian_index_preserves_default_ethereum_account(account_index):
    result = await create_hd_wallet_from_mnemonic(MNEMONIC, account_index=account_index)
    expected_xian = Wallet.from_mnemonic(MNEMONIC, account_index)
    expected_ethereum = HDWallet(MNEMONIC).get_ethereum_wallet()

    assert isinstance(result, dict)
    assert result["account_index"] == account_index
    assert result["xian_public_key"] == expected_xian.public_key
    assert result["xian_private_key"] == expected_xian.private_key
    assert result["ethereum_account_index"] == 0
    assert result["eth_public_key"] == expected_ethereum.address
    assert result["eth_private_key"] == expected_ethereum.private_key


async def test_mnemonic_import_normalizes_case_and_whitespace():
    result = await create_hd_wallet_from_mnemonic(
        "  " + " \n ".join(MNEMONIC.upper().split()) + "  "
    )

    assert isinstance(result, dict)
    assert result["mnemonic"] == MNEMONIC
    assert (
        result["xian_public_key"]
        == "7e15e2e068cb41b5c4b51dafef23d944aa7d120d362097d1f7c02100da5c2be5"
    )


@pytest.mark.parametrize("account_index", [-1, True, 1.5, 2**32, [44, 0, 0, 0, 0]])
@pytest.mark.parametrize("handler", [create_hd_wallet, create_hd_wallet_from_mnemonic])
async def test_mnemonic_tools_reject_invalid_account_indices(handler, account_index):
    arguments = {"account_index": account_index}
    if handler is create_hd_wallet_from_mnemonic:
        arguments["mnemonic"] = MNEMONIC
    result = await handler(**arguments)

    assert isinstance(result, str)
    assert result.startswith("❌")
    assert "account_index" in result


@pytest.mark.parametrize("mnemonic", ["", " \n ", "not a mnemonic"])
async def test_mnemonic_import_rejects_invalid_phrase(mnemonic):
    result = await create_hd_wallet_from_mnemonic(mnemonic)

    assert isinstance(result, str)
    assert result.startswith("❌")


@pytest.mark.parametrize("name", ["create_hd_wallet", "create_hd_wallet_from_mnemonic"])
def test_mnemonic_tool_schemas_offer_bounded_optional_account_index(name):
    spec = next(spec for spec in TOOL_SPECS if spec["name"] == name)
    schema = spec["schema"]

    assert schema["properties"]["account_index"]["type"] == "integer"
    assert schema["properties"]["account_index"]["minimum"] == 0
    assert schema["properties"]["account_index"]["maximum"] == 2**32 - 1
    assert schema["properties"]["account_index"]["default"] == 0
    assert "account_index" not in schema["required"]
    assert "path" not in schema["properties"]
