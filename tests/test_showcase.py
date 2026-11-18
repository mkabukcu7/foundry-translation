import io
import json
import threading
import time
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pptx import Presentation
from requests.exceptions import ConnectionError

from scripts import showcase
from services.config import ReconstructionError, ReviewTimeoutError
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
    assert client.get("/job-status.js").status_code == 200
    assert client.get("/style.css").status_code == 200
    assert len(client.get("/api/samples").json()) == 3


def test_translation_connection_failure_is_distinguished_from_browser_disconnect(client, monkeypatch, caplog):
    def fail(source, results, *, progress, **_):
        progress("translate")
        raise ConnectionError("PRIVATE-ENDPOINT")

    monkeypatch.setattr(showcase, "run_file", fail)
    response = client.post("/api/jobs?filename=claim.pdf", content=pdf(), headers=HEADERS)
    job = wait_for_job(client, response.json()["jobId"])
    assert job["state"] == "failed" and job["stage"] == "translate"
    assert "connection to the translation service" in job["error"]
    assert "PRIVATE-ENDPOINT" not in job["error"] + caplog.text
    assert "partialOutput" not in job


def test_reconstruction_failure_has_safe_error_and_downloadable_partial_text(client, tmp_path, monkeypatch):
    def fail(source, results, *, job_id, progress, **_):
        destination = results / job_id
        destination.mkdir()
        (destination / "translated.txt").write_text("Machine translation - not reviewed\n\nTranslation")
        progress("reconstruct")
        raise ReconstructionError("Translation completed, but PDF reconstruction cannot render this character set.")

    monkeypatch.setattr(showcase, "run_file", fail)
    response = client.post("/api/jobs?filename=claim.pdf", content=pdf(), headers=HEADERS)
    job_id = response.json()["jobId"]
    job = wait_for_job(client, job_id)
    assert job["state"] == "failed" and job["stage"] == "reconstruct"
    assert job["partialOutput"] == "translated.txt"
    assert "character set" in job["error"] and "Azure login" not in job["error"]
    assert "not reviewed" in client.get(f"/api/jobs/{job_id}/download/translated.txt").text


@pytest.mark.parametrize("filename", ["translated.pdf", "translated.pptx", "translated.png",
                                     "translated.jpeg", "translated.tiff"])
def test_same_format_output_is_selected_and_downloadable(client, tmp_path, filename):
    job_id = "f" * 32
    destination = tmp_path / job_id
    destination.mkdir()
    (destination / "report.json").write_text(json.dumps({
        "jobId": job_id, "status": "not_reviewed", "score": None, "outputFile": filename,
    }))
    (destination / filename).write_bytes(b"synthetic-output")
    (destination / "translated.txt").write_text("Text sidecar")
    assert client.get(f"/api/jobs/{job_id}").json()["output"] == filename
    assert client.get(f"/api/jobs/{job_id}/download/{filename}").content == b"synthetic-output"


def test_cross_origin_and_rebinding_blocked(client):
    assert client.get("/", headers={"host": "malicious.example:8765"}).status_code == 403
    assert client.post("/api/jobs?sample=scan").status_code == 403
    assert client.post("/api/jobs?sample=scan", headers={
        **HEADERS, "Origin": "https://external.example",
    }).status_code == 403


@pytest.mark.parametrize("filename", ["../private.pdf", "C:private.pdf", "report.doc", ""])
def test_upload_names_are_not_paths(client, filename):
    response = client.post("/api/jobs", params={"filename": filename}, content=b"data", headers=HEADERS)
    assert response.status_code == 400


def test_rejects_bad_file_and_unknown_samples(client):
    assert client.post("/api/jobs?filename=bad.pdf", content=b"bad", headers=HEADERS).status_code == 400
    assert client.post("/api/jobs?sample=unknown", headers=HEADERS).status_code == 400
    assert client.post("/api/jobs?filename=bad.pdf&strategy=unknown", content=b"bad",
                       headers=HEADERS).status_code == 400
    assert client.post("/api/jobs?filename=big.pptx", content=b"x" * (10 * 1024 * 1024 + 1),
                       headers=HEADERS).status_code == 413


@pytest.mark.parametrize("translator_only,strategy", [
    (False, "translator"), (False, "direct_gpt"), (False, "translator_refine"), (True, "translator"),
])
@pytest.mark.parametrize("skip_review", [False, True])
def test_spanish_upload_passes_language_to_runner(monkeypatch, tmp_path, translator_only, strategy, skip_review):
    monkeypatch.setenv("SHOWCASE_SKIP_REVIEW", str(skip_review).lower())
    seen = []

    def run_file(source, results, *, job_id, progress, source_language, target_language, **options):
        assert target_language == "en"
        seen.append((source_language, options))
        destination = results / job_id
        destination.mkdir()
        report = {"jobId": job_id, "status": "complete", "score": 100, "sourceLanguage": source_language}
        (destination / "report.json").write_text(json.dumps(report), encoding="utf-8")
        (destination / "translated.txt").write_text("Policy", encoding="utf-8")
        return report

    monkeypatch.setattr(showcase, "run_file", run_file)
    port = 8766 if translator_only else 8765
    headers = {**HEADERS, "Origin": f"http://localhost:{port}"}
    with TestClient(showcase.create_app(tmp_path, translator_only=translator_only),
                    base_url=f"http://localhost:{port}") as client:
        assert client.get("/api/config").json()["reviewEnabled"] is not (translator_only or skip_review)
        response = client.post(
            "/api/jobs", params={"filename": "spanish.pdf", "sourceLanguage": "es", "strategy": strategy},
            content=pdf(), headers=headers,
        )
        assert response.status_code == 202
        assert wait_for_job(client, response.json()["jobId"])["report"]["sourceLanguage"] == "es"
    options = {"translator_only": True} if translator_only else {"strategy": strategy}
    if skip_review and not translator_only:
        options["skip_review"] = True
        options["hybrid"] = False
    assert seen == [("es", options)]


@pytest.mark.parametrize("query", ["sourceLanguage=fr", "sourceLanguage=", "sourceLanguage=es&sample=scan",
                                 "targetLanguage=fr", "targetLanguage=", "sourceLanguage=en",
                                 "sourceLanguage=es&targetLanguage=es"])
def test_invalid_or_mismatched_source_language_is_rejected(client, query):
    response = client.post(f"/api/jobs?filename=claim.pdf&{query}", content=pdf(), headers=HEADERS)
    assert response.status_code == 400


def test_selected_model_engine_reaches_the_local_runner(monkeypatch, tmp_path):
    monkeypatch.setenv("GPT6_1_TERRA_MODEL_NAME", "gpt-6.1-terra")
    monkeypatch.setenv("GPT6_1_TERRA_DEPLOYMENT_NAME", "terra-poc")
    seen = []

    def run_file(source, results, *, job_id, progress, source_language, target_language, strategy, engine):
        seen.append((strategy, engine, source_language, target_language))
        destination = results / job_id
        destination.mkdir()
        report = {"jobId": job_id, "status": "not_reviewed", "score": None, "outputFile": "translated.pdf"}
        (destination / "report.json").write_text(json.dumps(report), encoding="utf-8")
        (destination / "translated.pdf").write_bytes(b"synthetic-output")
        return report

    monkeypatch.setattr(showcase, "run_file", run_file)
    with TestClient(showcase.create_app(tmp_path), base_url="http://localhost:8765") as client:
        response = client.post(
            "/api/jobs", params={"filename": "sample.pdf", "engine": "gpt6_1_terra"},
            content=pdf(), headers=HEADERS,
        )
        assert response.status_code == 202
        job = wait_for_job(client, response.json()["jobId"])
    assert job["state"] == "finished"
    assert seen == [("direct_gpt", "gpt6_1_terra", "it", "en")]


@pytest.mark.parametrize("source_language,target_language", [
    ("en", "es"), ("es", "en"), ("en", "it"), ("it", "en"), ("es", "it"), ("it", "es"),
])
@pytest.mark.parametrize("strategy", ["translator", "direct_gpt", "translator_refine"])
def test_bidirectional_language_pair_reaches_runner(monkeypatch, tmp_path, source_language, target_language, strategy):
    seen = []

    def run_file(source, results, *, job_id, progress, source_language, target_language, **options):
        seen.append((source_language, target_language, options["strategy"]))
        destination = results / job_id
        destination.mkdir()
        report = {"jobId": job_id, "status": "not_reviewed", "score": None,
                  "sourceLanguage": source_language, "targetLanguage": target_language}
        (destination / "report.json").write_text(json.dumps(report))
        return report

    monkeypatch.setattr(showcase, "run_file", run_file)
    with TestClient(showcase.create_app(tmp_path), base_url="http://localhost:8765") as client:
        response = client.post("/api/jobs", params={
            "filename": "test.pdf", "sourceLanguage": source_language, "targetLanguage": target_language,
            "strategy": strategy,
        }, content=pdf(), headers=HEADERS)
        assert response.status_code == 202
        job = wait_for_job(client, response.json()["jobId"])
        assert job["report"]["targetLanguage"] == target_language
    assert seen == [(source_language, target_language, strategy)]


@pytest.mark.parametrize("target_language", ["es", "it"])
def test_baseline_accepts_english_source_and_non_english_target(monkeypatch, tmp_path, target_language):
    def run_file(source, results, *, job_id, source_language, target_language, translator_only, **_):
        assert source_language == "en" and translator_only
        destination = results / job_id
        destination.mkdir()
        report = {"jobId": job_id, "status": "not_reviewed", "score": None, "targetLanguage": target_language}
        (destination / "report.json").write_text(json.dumps(report))
        return report

    monkeypatch.setattr(showcase, "run_file", run_file)
    with TestClient(showcase.create_app(tmp_path, translator_only=True), base_url="http://localhost:8766") as client:
        response = client.post("/api/jobs", params={
            "filename": "test.pdf", "sourceLanguage": "en", "targetLanguage": target_language,
        }, content=pdf(), headers={**HEADERS, "Origin": "http://localhost:8766"})
        assert response.status_code == 202
        assert wait_for_job(client, response.json()["jobId"])["report"]["targetLanguage"] == target_language


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


def test_image_pptx_upload_is_accepted_for_ocr_translation(client, monkeypatch, tmp_path):
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    image = io.BytesIO()
    Image.new("RGB", (10, 10), "white").save(image, format="PNG")
    image.seek(0)
    slide.shapes.add_picture(image, 0, 0)
    data = io.BytesIO()
    presentation.save(data)

    def run_file(source, results, *, job_id, progress, strategy, source_language, target_language):
        assert strategy == "translator"
        destination = results / job_id
        destination.mkdir()
        report = {"jobId": job_id, "status": "needs_review", "score": 70}
        (destination / "report.json").write_text(json.dumps(report), encoding="utf-8")
        (destination / "translated.txt").write_text("Marketing text", encoding="utf-8")
        return report

    monkeypatch.setattr(showcase, "run_file", run_file)
    response = client.post(
        "/api/jobs?filename=marketing.pptx&strategy=translator",
        content=data.getvalue(),
        headers=HEADERS,
    )
    assert response.status_code == 202
    assert wait_for_job(client, response.json()["jobId"])["state"] == "finished"


def test_standalone_image_upload_is_accepted_for_ocr_translation(client, monkeypatch, tmp_path):
    image = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(image, format="PNG")

    def run_file(source, results, *, job_id, progress, strategy, source_language, target_language):
        assert source.name == "marketing.png"
        assert source.read_bytes().startswith(b"\x89PNG")
        destination = results / job_id
        destination.mkdir()
        report = {"jobId": job_id, "status": "needs_review", "score": 70}
        (destination / "report.json").write_text(json.dumps(report), encoding="utf-8")
        (destination / "translated.txt").write_text("Marketing text", encoding="utf-8")
        return report

    monkeypatch.setattr(showcase, "run_file", run_file)
    response = client.post(
        "/api/jobs?filename=marketing.png&strategy=direct_gpt",
        content=image.getvalue(),
        headers=HEADERS,
    )
    assert response.status_code == 202
    assert wait_for_job(client, response.json()["jobId"])["state"] == "finished"


def test_upload_runs_in_background_saves_and_downloads(client, monkeypatch, tmp_path):
    def run_file(source, results, *, job_id, progress, strategy, source_language, target_language):
        assert strategy == "direct_gpt"
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
    response = client.post("/api/jobs?filename=claim.pdf&strategy=direct_gpt", content=pdf(), headers=HEADERS)
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

    def fail(source, results, *, job_id, progress, strategy, source_language, target_language):
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


def test_reviewer_timeout_shows_safe_actionable_message(client, monkeypatch):
    def fail(*_args, **_kwargs):
        raise ReviewTimeoutError("translation-checker-secondary", 600)

    monkeypatch.setattr(showcase, "run_file", fail)
    response = client.post("/api/jobs?filename=claim.pdf", content=pdf(), headers=HEADERS)
    job = wait_for_job(client, response.json()["jobId"])
    assert job["state"] == "failed"
    assert "translation-checker-secondary" in job["error"]
    assert "600s" in job["error"] and "quality review is incomplete" in job["error"]
    assert "Check Azure login" not in job["error"]


@pytest.mark.parametrize("rate_limited", [False, True])
def test_failed_review_exposes_only_the_unreviewed_pptx(client, monkeypatch, rate_limited):
    from services.config import ReviewOutputError, ReviewRateLimitError

    presentation = Presentation()
    presentation.slides.add_slide(presentation.slide_layouts[6])
    data = io.BytesIO()
    presentation.save(data)

    def fail(_source, results, *, job_id, progress, **_kwargs):
        destination = results / job_id
        destination.mkdir()
        (destination / "translated-unreviewed.pptx").write_bytes(data.getvalue())
        progress("check")
        error = ReviewRateLimitError if rate_limited else ReviewOutputError
        raise error("translation-checker-secondary")

    monkeypatch.setattr(showcase, "run_file", fail)
    response = client.post("/api/jobs?filename=claim.pptx", content=data.getvalue(), headers=HEADERS)
    assert response.status_code == 202
    job = wait_for_job(client, response.json()["jobId"])
    assert job["state"] == "failed"
    assert ("HTTP 429" if rate_limited else "invalid review response") in job["error"]
    assert "Check Azure login" not in job["error"]
    assert job["partialDocument"] == "translated-unreviewed.pptx"
    base = f"/api/jobs/{job['jobId']}/download"
    draft = client.get(f"{base}/{job['partialDocument']}")
    assert draft.status_code == 200 and draft.content == data.getvalue()
    assert client.get(f"{base}/translated.pptx").status_code == 404


def test_sample_run_uses_only_allowlisted_path(client, monkeypatch, tmp_path):
    sample_folder = tmp_path / "samples" / "out"
    sample_folder.mkdir(parents=True)
    sample_data = pdf()
    (sample_folder / "local_scanned_it.pdf").write_bytes(sample_data)
    monkeypatch.setattr(showcase, "ROOT", tmp_path)
    seen = []

    def run_file(source, results, *, job_id, progress, strategy, source_language, target_language):
        assert strategy == "translator"
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
    def run_file(source, results, *, job_id, progress, translator_only, source_language, target_language):
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
