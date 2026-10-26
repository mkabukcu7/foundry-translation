import base64
import json

import pytest

from services import auth, intake


def request(data=b"hello", **fields):
    return json.dumps({"fileName": "claim.pdf", "contentBase64": base64.b64encode(data).decode(), **fields}).encode()


def test_base64_default_and_reverse():
    meta, data = intake.decode(request(), "application/json")
    assert data == b"hello" and meta["targetLanguage"] == "en" and not meta["sourceLanguage"]
    meta, _ = intake.decode(request(sourceLanguage="en", targetLanguage="it"), "application/json")
    assert meta["sourceLanguage"] == "en" and meta["targetLanguage"] == "it"


@pytest.mark.parametrize("name", ["claim.ppt", "../claim.pdf", "c:\\claim.pdf"])
def test_reject_bad_filename(name):
    with pytest.raises(intake.IntakeError):
        intake.filename(name)


def test_invalid_base64_and_size(monkeypatch):
    with pytest.raises(intake.IntakeError):
        intake.decode(b'{"fileName":"x.pdf","contentBase64":"%%%"}', "application/json")
    monkeypatch.setenv("MAX_BASE64_MB", "1")
    with pytest.raises(intake.IntakeError, match="limit") as error:
        intake.decode(request(b"x" * (1024 * 1024 + 1)), "application/json")
    assert error.value.status == 413
    assert intake.decode(request(b"x" * (1024 * 1024)), "application/json")[1] == b"x" * (1024 * 1024)


def test_multipart():
    body = (b'--bound\r\nContent-Disposition: form-data; name="file"; filename="claim.pptx"\r\n'
            b'Content-Type: application/octet-stream\r\n\r\nfile-bytes\r\n'
            b'--bound\r\nContent-Disposition: form-data; name="targetLanguage"\r\n\r\nes\r\n--bound--\r\n')
    meta, content = intake.decode(body, "multipart/form-data; boundary=bound")
    assert meta["targetLanguage"] == "es" and content == b"file-bytes"


def principal(scopes="Translation.Invoke", object_id="user-a"):
    return {"x-ms-client-principal": base64.b64encode(json.dumps({
        "claims": [{"typ": "oid", "val": object_id}, {"typ": "scp", "val": scopes}],
    }).encode()).decode()}


def test_auth_scopes_and_production_bypass(monkeypatch):
    assert auth.owner(principal()) == "user-a"
    with pytest.raises(intake.IntakeError) as error:
        auth.owner(principal("User.Read"))
    assert error.value.status == 403
    monkeypatch.setenv("ALLOW_LOCAL_AUTH_BYPASS", "1")
    monkeypatch.setenv("AZURE_FUNCTIONS_ENVIRONMENT", "Development")
    monkeypatch.setenv("WEBSITE_INSTANCE_ID", "cloud-instance")
    with pytest.raises(intake.IntakeError):
        auth.owner({})


def test_sharepoint_rejects_ssrf_before_network(monkeypatch):
    monkeypatch.setenv("SHAREPOINT_HOSTS", "contoso.sharepoint.com")
    for url in ("http://contoso.sharepoint.com/x", "https://localhost/x",
                "https://contoso.sharepoint.com@localhost/x"):
        with pytest.raises(intake.IntakeError):
            intake.from_url({"url": url})
