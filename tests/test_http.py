import asyncio
import base64
import json
from unittest.mock import AsyncMock

import azure.functions as func

from services import jobs_table, storage
from tests.helpers import indexed_functions
from tests.test_intake import principal


def function(name):
    return indexed_functions()[name].get_user_function()


def test_submit_returns_only_job_metadata_and_starts_workflow(monkeypatch):
    rows, blobs = [], []
    monkeypatch.setattr(jobs_table, "create", rows.append)
    monkeypatch.setattr(storage, "put", lambda *args: blobs.append(args))
    client = AsyncMock()
    content = b"document content must not enter durable history"
    request = func.HttpRequest("POST", "https://unit/api/jobs", headers={
        **principal(), "content-type": "application/json",
    }, body=json.dumps({
        "fileName": "claim.pdf", "contentBase64": base64.b64encode(content).decode(),
    }).encode())
    # Unwrap the host's Durable client-binding middleware, supplying an AsyncMock directly.
    response = asyncio.run(function("post_jobs").__wrapped__(request, client))
    assert response.status_code == 202
    payload = json.loads(response.get_body())
    assert set(payload) == {"jobId", "statusUrl"}
    assert rows[0]["ownerId"] == "user-a"
    assert blobs[0][2] == content
    client.start_new.assert_awaited_once()
    assert "content" not in json.dumps(client.start_new.call_args.args)


def test_job_owner_is_enforced_before_reading_report(monkeypatch):
    job_id = "a" * 32
    monkeypatch.setattr(jobs_table, "get", lambda _: {"ownerId": "another-user", "jobId": job_id})
    request = func.HttpRequest("GET", f"https://unit/api/jobs/{job_id}/report", headers=principal(),
                               route_params={"id": job_id}, body=b"")
    response = asyncio.run(function("get_report")(request))
    assert response.status_code == 404


def test_report_not_ready(monkeypatch):
    job_id = "a" * 32
    monkeypatch.setattr(jobs_table, "get", lambda _: {"ownerId": "user-a", "jobId": job_id})
    request = func.HttpRequest("GET", f"https://unit/api/jobs/{job_id}/report", headers=principal(),
                               route_params={"id": job_id}, body=b"")
    assert asyncio.run(function("get_report")(request)).status_code == 409


def test_missing_connections_are_explicit_and_arbitrary_errors_are_redacted(monkeypatch):
    from services.config import ConfigurationError
    job_id = "a" * 32
    request = func.HttpRequest("GET", f"https://unit/api/jobs/{job_id}", headers=principal(),
                               route_params={"id": job_id}, body=b"")

    def missing(_):
        raise ConfigurationError("Missing configuration: STORAGE_TABLE_ENDPOINT")

    monkeypatch.setattr(jobs_table, "get", missing)
    result = asyncio.run(function("get_job")(request))
    assert result.status_code == 503 and b"STORAGE_TABLE_ENDPOINT" in result.get_body()

    def upstream(_):
        raise RuntimeError("secret document excerpt or raw upstream token")

    monkeypatch.setattr(jobs_table, "get", upstream)
    result = asyncio.run(function("get_job")(request))
    assert result.status_code == 503 and b"secret" not in result.get_body()
