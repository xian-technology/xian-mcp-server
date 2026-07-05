import pytest
from fastapi.testclient import TestClient
from xian_py.models import TransactionSubmission

from http_server import HTTP_CORS_ORIGINS_ENV, HTTP_TOKEN_ENV, create_app
from tool_policy import UNSAFE_WALLET_TOOLS_ENV


def _clear_http_security_env(monkeypatch):
    monkeypatch.delenv(UNSAFE_WALLET_TOOLS_ENV, raising=False)
    monkeypatch.delenv(HTTP_TOKEN_ENV, raising=False)
    monkeypatch.delenv(HTTP_CORS_ORIGINS_ENV, raising=False)


def _demo_tool_specs():
    async def safe_handler():
        return {"safe": True}

    async def unsafe_handler():
        return {"unsafe": True}

    return [
        {
            "name": "safe_tool",
            "description": "safe tool",
            "schema": {"type": "object", "properties": {}, "required": []},
            "handler": safe_handler,
        },
        {
            "name": "unsafe_tool",
            "description": "unsafe tool",
            "schema": {"type": "object", "properties": {}, "required": []},
            "unsafe": True,
            "handler": unsafe_handler,
        },
    ]


def test_http_bridge_normalizes_sdk_models(monkeypatch):
    _clear_http_security_env(monkeypatch)

    async def handler():
        return TransactionSubmission.from_dict(
            {
                "submitted": True,
                "accepted": True,
                "finalized": False,
                "tx_hash": "abc",
                "mode": "checktx",
                "nonce": 1,
                "chi_supplied": 10,
                "chi_estimated": 8,
                "message": None,
                "response": {"result": {"hash": "abc"}},
            }
        )

    app = create_app(
        [
            {
                "name": "demo",
                "description": "demo tool",
                "schema": {"type": "object", "properties": {}, "required": []},
                "handler": handler,
            }
        ]
    )

    client = TestClient(app)
    response = client.post("/tools/demo", json={})

    assert response.status_code == 200
    assert response.json() == {
        "result": {
            "submitted": True,
            "accepted": True,
            "finalized": False,
            "tx_hash": "abc",
            "mode": "checktx",
            "nonce": 1,
            "chi_supplied": 10,
            "chi_estimated": 8,
            "message": None,
            "response": {"result": {"hash": "abc"}},
            "receipt": None,
        }
    }
    assert "raw" not in response.text


def test_http_bridge_hides_unsafe_tools_by_default(monkeypatch):
    _clear_http_security_env(monkeypatch)

    client = TestClient(create_app(_demo_tool_specs()))
    response = client.get("/tools")

    assert response.status_code == 200
    tool_names = {tool["name"] for tool in response.json()}
    assert tool_names == {"safe_tool"}


def test_http_bridge_rejects_unsafe_tool_calls_by_default(monkeypatch):
    _clear_http_security_env(monkeypatch)

    client = TestClient(create_app(_demo_tool_specs()))
    response = client.post("/tools/unsafe_tool", json={})

    assert response.status_code == 403
    assert "disabled by default" in response.json()["detail"]


def test_default_xian_http_catalog_hides_and_rejects_unsafe_tools(monkeypatch):
    _clear_http_security_env(monkeypatch)

    client = TestClient(create_app())
    list_response = client.get("/tools")
    call_response = client.post("/tools/create_wallet", json={})

    assert list_response.status_code == 200
    tool_names = {tool["name"] for tool in list_response.json()}
    assert "get_balance" in tool_names
    assert "create_wallet" not in tool_names
    assert "send_transaction" not in tool_names
    assert "sign_message" not in tool_names
    assert call_response.status_code == 403
    assert "disabled by default" in call_response.json()["detail"]


def test_http_bridge_exposes_unsafe_tools_when_gate_and_token_are_enabled(monkeypatch):
    _clear_http_security_env(monkeypatch)
    monkeypatch.setenv(UNSAFE_WALLET_TOOLS_ENV, "1")
    monkeypatch.setenv(HTTP_TOKEN_ENV, "dev-secret")

    client = TestClient(create_app(_demo_tool_specs()))
    headers = {"Authorization": "Bearer dev-secret"}
    list_response = client.get("/tools", headers=headers)
    call_response = client.post("/tools/unsafe_tool", json={}, headers=headers)

    assert list_response.status_code == 200
    assert {tool["name"] for tool in list_response.json()} == {
        "safe_tool",
        "unsafe_tool",
    }
    assert call_response.status_code == 200
    assert call_response.json() == {"result": {"unsafe": True}}


def test_http_bridge_requires_token_when_unsafe_gate_is_enabled(monkeypatch):
    _clear_http_security_env(monkeypatch)
    monkeypatch.setenv(UNSAFE_WALLET_TOOLS_ENV, "1")

    client = TestClient(create_app(_demo_tool_specs()))
    response = client.get("/tools")

    assert response.status_code == 503
    assert HTTP_TOKEN_ENV in response.json()["detail"]


def test_http_bridge_requires_configured_token(monkeypatch):
    _clear_http_security_env(monkeypatch)
    monkeypatch.setenv(HTTP_TOKEN_ENV, "dev-secret")

    client = TestClient(create_app(_demo_tool_specs()))
    missing_response = client.get("/tools")
    invalid_response = client.get("/tools", headers={"Authorization": "Bearer wrong"})
    valid_response = client.get("/tools", headers={"Authorization": "Bearer dev-secret"})

    assert missing_response.status_code == 401
    assert invalid_response.status_code == 401
    assert valid_response.status_code == 200


def test_http_bridge_requires_token_for_non_loopback_bind(monkeypatch):
    _clear_http_security_env(monkeypatch)

    client = TestClient(create_app(_demo_tool_specs(), bind_host="0.0.0.0"))
    response = client.get("/tools")

    assert response.status_code == 503
    assert HTTP_TOKEN_ENV in response.json()["detail"]


@pytest.mark.parametrize("bind_host", ["::1", "[::1]"])
def test_http_bridge_allows_ipv6_loopback_bind_without_token(monkeypatch, bind_host):
    _clear_http_security_env(monkeypatch)

    client = TestClient(create_app(_demo_tool_specs(), bind_host=bind_host))
    response = client.get("/tools")

    assert response.status_code == 200
    assert {tool["name"] for tool in response.json()} == {"safe_tool"}


@pytest.mark.parametrize("bind_host", ["::", "2001:db8::1", "[2001:db8::1]"])
def test_http_bridge_requires_token_for_non_loopback_ipv6_bind(monkeypatch, bind_host):
    _clear_http_security_env(monkeypatch)

    client = TestClient(create_app(_demo_tool_specs(), bind_host=bind_host))
    response = client.get("/tools")

    assert response.status_code == 503
    assert HTTP_TOKEN_ENV in response.json()["detail"]


def test_http_bridge_has_no_cors_by_default(monkeypatch):
    _clear_http_security_env(monkeypatch)

    client = TestClient(create_app(_demo_tool_specs()))
    response = client.get("/health", headers={"Origin": "http://localhost:3000"})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_http_bridge_allows_explicit_cors_origin(monkeypatch):
    _clear_http_security_env(monkeypatch)
    monkeypatch.setenv(HTTP_CORS_ORIGINS_ENV, "http://localhost:3000")

    client = TestClient(create_app(_demo_tool_specs()))
    response = client.options(
        "/tools",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_http_bridge_rejects_wildcard_cors(monkeypatch):
    _clear_http_security_env(monkeypatch)
    monkeypatch.setenv(HTTP_CORS_ORIGINS_ENV, "*")

    with pytest.raises(RuntimeError, match="wildcard CORS"):
        create_app(_demo_tool_specs())
