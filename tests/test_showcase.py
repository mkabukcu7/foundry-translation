import io
import json
import threading
import time
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from scripts import showcase
from tests.test_pipeline import pdf

HEADERS = {"Origin": "http://localhost:8765", "X-Showcase-Request": "1"}


@pytest.fixture
def client(tmp_path):
    with TestClient(showcase.create_app(tmp_path), base_url="http://localhost:8765") as client:
        yield client


def wait_for_job(client, job_id):
    for _ in range(100):
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["state"] != "running":
            return job
        time.sleep(.01)
    pytest.fail("Background job did not finish")


def test_local_page_and_assets(client):
    page = client.get("/")
    assert page.status_code == 200 and "Translation Lab" in page.text
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    assert page.headers["Cache-Control"] == "no-store"
    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200
    assert len(client.get("/api/samples").json()) == 3


def test_cross_origin_and_rebinding_blocked(client):
    assert client.get("/", headers={"host": "malicious.example:8765"}).status_code == 403
    assert client.post("/api/jobs?sample=scan").status_code == 403
    assert client.post("/api/jobs?sample=scan", headers={
        **HEADERS, "Origin": "https://external.example",
    }).status_code == 403


@pytest.mark.parametrize("filename", ["../private.pdf", "C:private.pdf", "report.docx", ""])
def test_upload_names_are_not_paths(client, filename):
    response = client.post("/api/jobs", params={"filename": filename}, content=b"data", headers=HEADERS)
    assert response.status_code == 400


def test_rejects_bad_file_and_unknown_samples(client):
    assert client.post("/api/jobs?filename=bad.pdf", content=b"bad", headers=HEADERS).status_code == 400
    assert client.post("/api/jobs?sample=unknown", headers=HEADERS).status_code == 400
    assert client.post("/api/jobs?filename=big.pptx", content=b"x" * (10 * 1024 * 1024 + 1),
                       headers=HEADERS).status_code == 413


def test_malformed_pptx_package_returns_sanitized_validation_error(client):
    data = io.BytesIO()
    with ZipFile(data, "w") as archive:
        archive.writestr(
            "ppt/presentation.xml",
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>',
        )
    response = client.post("/api/jobs?filename=broken.pptx", content=data.getvalue(), headers=HEADERS)
    assert response.status_code == 400
    assert "KeyError" not in response.text


def test_upload_runs_in_background_saves_and_downloads(client, monkeypatch, tmp_path):
    def run_file(source, results, *, job_id, progress):
        assert source.read_bytes().startswith(b"%PDF")
        progress("check")
        destination = results / job_id
        destination.mkdir()
        report = {"jobId": job_id, "status": "needs_review", "score": 60}
        (destination / "report.json").write_text(json.dumps(report), encoding="utf-8")
        (destination / "showcase.json").write_text(json.dumps({"terms": [], "sourceText": "Polizza"}),
                                                 encoding="utf-8")
        (destination / "translated.txt").write_text("Policy", encoding="utf-8")
        return report

    monkeypatch.setattr(showcase, "run_file", run_file)
    response = client.post("/api/jobs?filename=claim.pdf", content=pdf(), headers=HEADERS)
    assert response.status_code == 202
    job_id = response.json()["jobId"]
    job = wait_for_job(client, job_id)
    assert job["state"] == "finished" and job["report"]["score"] == 60
    assert job["preview"]["sourceText"] == "Polizza"
    assert client.get(f"/api/jobs/{job_id}/download/translated.txt").text == "Policy"
    assert client.get(f"/api/jobs/{job_id}/download/showcase.json").status_code == 404
    assert client.get("/api/jobs/not-a-job").status_code == 404
    assert client.get("/api/jobs").json()[0]["jobId"] == job_id


def test_busy_and_sanitized_failure(client, monkeypatch, caplog):
    started, release = threading.Event(), threading.Event()

    def fail(source, results, *, job_id, progress):
        progress("prepare")
        started.set()
        assert release.wait(5)
        raise ValueError("PRIVATE DOCUMENT CONTENT")

    monkeypatch.setattr(showcase, "run_file", fail)
    first = client.post("/api/jobs?filename=claim.pdf", content=pdf(), headers=HEADERS).json()
    try:
        assert started.wait(5)
        running = client.get(f"/api/jobs/{first['jobId']}").json()
        assert running["stage"] == "prepare"
        assert client.post("/api/jobs?filename=claim.pdf", content=pdf(), headers=HEADERS).status_code == 409
    finally:
        release.set()
    failed = wait_for_job(client, first["jobId"])
    assert failed["state"] == "failed" and "prepare" in failed["error"]
    assert "PRIVATE DOCUMENT CONTENT" not in json.dumps(failed)
    assert "PRIVATE DOCUMENT CONTENT" not in caplog.text
    assert first["jobId"] in caplog.text


def test_old_reports_remain_viewable(client, tmp_path):
    job_id = "b" * 32
    folder = tmp_path / job_id
    folder.mkdir()
    (folder / "report.json").write_text(json.dumps({"status": "needs_review", "score": 60}))
    (folder / "translated.txt").write_text("Policy")
    response = client.get(f"/api/jobs/{job_id}").json()
    assert response["state"] == "finished" and response["preview"] is None


def test_sample_run_uses_only_allowlisted_path(client, monkeypatch, tmp_path):
    sample_folder = tmp_path / "samples" / "out"
    sample_folder.mkdir(parents=True)
    sample_data = pdf()
    (sample_folder / "local_scanned_it.pdf").write_bytes(sample_data)
    monkeypatch.setattr(showcase, "ROOT", tmp_path)
    seen = []

    def run_file(source, results, *, job_id, progress):
        seen.append((source.name, source.read_bytes()))
        destination = results / job_id
        destination.mkdir()
        report = {"jobId": job_id, "status": "complete", "score": 100}
        (destination / "report.json").write_text(json.dumps(report), encoding="utf-8")
        (destination / "translated.txt").write_text("Policy")
        return report

    monkeypatch.setattr(showcase, "run_file", run_file)
    response = client.post("/api/jobs?sample=scan", headers=HEADERS)
    assert response.status_code == 202
    assert wait_for_job(client, response.json()["jobId"])["state"] == "finished"
    assert seen == [("local_scanned_it.pdf", sample_data)]
    assert client.post("/api/jobs?sample=slides", headers=HEADERS).status_code == 400


def test_baseline_separate_port_origin_and_runner(monkeypatch, tmp_path):
    def run_file(source, results, *, job_id, progress, translator_only):
        assert translator_only is True
        folder = results / job_id
        folder.mkdir()
        report = {"jobId": job_id, "status": "not_reviewed", "score": None}
        (folder / "report.json").write_text(json.dumps(report))
        (folder / "translated.txt").write_text("Policy")
        return report
    monkeypatch.setattr(showcase, "run_file", run_file)
    with TestClient(showcase.create_app(tmp_path, translator_only=True),
                    base_url="http://localhost:8766") as client:
        assert "Translator only" in client.get("/").text
        assert client.get("/app.js").status_code == 200
        assert client.get("/", headers={"Host": "localhost:8765"}).status_code == 403
        assert client.post("/api/jobs?filename=x.pdf", content=pdf(), headers=HEADERS).status_code == 403
        response = client.post("/api/jobs?filename=x.pdf", content=pdf(),
                               headers={**HEADERS, "Origin": "http://localhost:8766"})
        assert response.status_code == 202
        report = wait_for_job(client, response.json()["jobId"])["report"]
        assert report["status"] == "not_reviewed" and report["score"] is None
