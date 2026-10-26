import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from renderer.app import app
from services import retention, storage


def test_renderer_validates_issuer_audience_and_application_role(monkeypatch):
    import renderer.app as module
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setenv("AZURE_TENANT_ID", "tenant")
    monkeypatch.setenv("RENDERER_CLIENT_ID", "renderer")
    monkeypatch.setattr(module, "PyJWKClient", lambda _: SimpleNamespace(
        get_signing_key_from_jwt=lambda _: SimpleNamespace(key=key.public_key())
    ))
    client = TestClient(app)
    assert client.post("/render", content=b"x").status_code == 401
    claims = {"iss": "https://login.microsoftonline.com/tenant/v2.0", "aud": "renderer",
              "exp": int(time.time()) + 60, "iat": int(time.time()), "roles": []}
    token = jwt.encode(claims, key, algorithm="RS256")
    assert client.post("/render", content=b"x", headers={"Authorization": f"Bearer {token}"}).status_code == 403
    claims["roles"] = ["Renderer.Invoke"]
    token = jwt.encode(claims, key, algorithm="RS256")
    assert client.post("/render", content=b"x", headers={"Authorization": f"Bearer {token}"}).status_code == 415
    assert client.get("/healthz").status_code == 200


def test_retention_deletes_only_expired_application_targets(monkeypatch):
    now = datetime(2026, 3, 20, tzinfo=timezone.utc)
    client = MagicMock()
    old = now - timedelta(days=15)
    client.list_containers.return_value = [
        SimpleNamespace(name="target-" + "a" * 32, last_modified=old),
        SimpleNamespace(name="target-" + "b" * 32, last_modified=now),
        SimpleNamespace(name="target-not-an-application-job", last_modified=old),
    ]
    client.get_container_client.return_value.list_blobs.return_value = []
    monkeypatch.setattr(storage, "client", lambda: client)
    assert retention.cleanup(now) == 1
    client.delete_container.assert_called_once_with("target-" + "a" * 32)
