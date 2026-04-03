from fastapi.testclient import TestClient
from xian_py.models import TransactionSubmission

from http_server import create_app


def test_http_bridge_normalizes_sdk_models():
    async def handler():
        return TransactionSubmission.from_dict(
            {
                "submitted": True,
                "accepted": True,
                "finalized": False,
                "tx_hash": "abc",
                "mode": "checktx",
                "nonce": 1,
                "stamps_supplied": 10,
                "stamps_estimated": 8,
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
            "stamps_supplied": 10,
            "stamps_estimated": 8,
            "message": None,
            "response": {"result": {"hash": "abc"}},
            "receipt": None,
        }
    }
    assert "raw" not in response.text
