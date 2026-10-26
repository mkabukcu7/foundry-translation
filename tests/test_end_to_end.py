import copy
import io
import json
from datetime import datetime, timezone

import pytest
from pypdf import PdfReader

from orchestrator import run
from services import extraction, foundry_agents, jobs_table, storage
from tests.helpers import indexed_functions
from tests.test_pipeline import pdf


class Context:
    current_utc_datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __init__(self, job):
        self.job = job

    def get_input(self):
        return self.job

    def call_activity_with_retry(self, name, retry, data):
        return name, data

    def call_activity(self, name, data):
        return name, data


@pytest.mark.parametrize("critical,expected", [(False, "complete"), (True, "needs_review")])
def test_real_activity_chain_with_mocked_cloud_boundaries(monkeypatch, critical, expected):
    job_id = "a" * 32
    job = {"jobId": job_id, "fileName": "x.pdf", "sourcePath": f"{job_id}/x.pdf",
           "sourceLanguage": "it", "targetLanguage": "en"}
    row = {**job}
    blobs = {("source", job["sourcePath"]): pdf(), (f"target-{job_id}", "x.pdf"): pdf()}
    monkeypatch.setattr(storage, "ensure_container", lambda _: None)
    monkeypatch.setattr(storage, "url", lambda c, p="": f"https://unit.blob.core.windows.net/{c}/{p}")
    monkeypatch.setattr(storage, "source", lambda j: blobs[("source", j["sourcePath"])])
    monkeypatch.setattr(storage, "target", lambda j: ("x.pdf", blobs[(f"target-{job_id}", "x.pdf")]))
    monkeypatch.setattr(storage, "put", lambda c, p, d, *args: blobs.__setitem__((c, p), d))
    monkeypatch.setattr(storage, "put_json", lambda j, n, d:
                        blobs.__setitem__(("reports", f"{j}/{n}.json"), json.dumps(d).encode()))
    monkeypatch.setattr(storage, "get_json", lambda j, n: json.loads(blobs[("reports", f"{j}/{n}.json")]))
    monkeypatch.setattr(jobs_table, "get", lambda _: copy.deepcopy(row))
    monkeypatch.setattr(jobs_table, "update", lambda _, **fields: row.update(fields))
    monkeypatch.setattr(extraction, "extract", lambda *_: {
        "segments": [{"text": "POL-123", "role": "paragraph", "location": "paragraph:1"}], "language": "it",
    })
    usage = {"inputTokens": 20, "outputTokens": 10, "version": "1", "model": "unit"}

    def judge(name, data, _):
        if name == "term-guard":
            return {"terms": [{"term": "POL-123", "type": "policy_number", "action": "keep", "target": None}]}, usage
        issues = [{"severity": "critical", "type": "meaning_shift", "location": "paragraph:1",
                   "source_excerpt": "POL-123", "target_excerpt": "POL-123",
                   "explanation": "Synthetic critical issue"}] if critical else []
        return {"score": 100, "issues": issues}, usage

    monkeypatch.setattr(foundry_agents, "invoke", judge)
    from services.translator_client import Translator
    monkeypatch.setattr(Translator, "__init__", lambda self: None)
    monkeypatch.setattr(Translator, "submit", lambda *args: "https://unit/operation")
    monkeypatch.setattr(Translator, "poll", lambda *args: {"status": "Succeeded"})
    monkeypatch.setattr(Translator, "documents", lambda *args: [
        {"status": "Succeeded", "characterCharged": 42, "imageCharged": 0}
    ])
    activities = indexed_functions()
    generator = run(Context(job))
    request = next(generator)
    while True:
        name, data = request
        assert name != "record_failure"
        result = activities[name].get_user_function()(data)
        try:
            request = generator.send(result)
        except StopIteration as done:
            assert done.value["status"] == expected
            break
    assert row["status"] == expected and row["chars"] == 42 and row["tokens"] == 60
    report = storage.get_json(job_id, "report")
    assert report["decision"] == ("flagged" if critical else "passed")
    assert report["costSignals"]["characters"] == 42
    if critical:
        result = blobs[("reports", f"{job_id}/reviewed.pdf")]
        assert "requires review" in PdfReader(io.BytesIO(result)).pages[0].extract_text()
        # A retry reads original Translator output, not a previously watermarked copy.
        before = PdfReader(io.BytesIO(result)).pages[0].mediabox.height
        from activities.route import run as route
        route(job)
        after = PdfReader(io.BytesIO(blobs[("reports", f"{job_id}/reviewed.pdf")])).pages[0].mediabox.height
        assert before == after
