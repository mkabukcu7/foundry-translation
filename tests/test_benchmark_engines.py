import sys

import pytest
from azure.core.exceptions import ServiceRequestError

from scripts import benchmark_engines
from scripts.benchmark_engines import exact_values, structure_matches, terminology_score


def test_fidelity_checks_compare_exact_numbers_dates_ids_and_currency():
    checks = exact_values(
        "POL-123 dated 12/01/2025 for EUR 1.250,00",
        "POL-123 dated 12/01/2025 for EUR 1.250,00",
    )
    assert checks["numbers"]["exact"]
    assert checks["dates"]["exact"]
    assert checks["ids"]["exact"]
    assert checks["currency"]["exact"]
    changed = exact_values(
        "POL-123 dated 12/01/2025 for EUR 1.250,00",
        "POL-123 dated 12/01/2025 for EUR 1.200,00",
    )
    assert not changed["currency"]["exact"]


def test_terminology_score_and_structure_comparison():
    terms = [
        {"term": "franchigia", "action": "map", "target": "deductible"},
        {"term": "POL-123", "action": "keep", "target": None},
    ]
    assert terminology_score(terms, "deductible POL-123") == 100
    assert terminology_score(terms, "excess POL-123") == 50
    assert terminology_score([], "") is None
    assert structure_matches({"pages": 2, "images": 1}, {"pages": 2, "images": 1})
    assert not structure_matches({"pages": 2}, {"pages": 1})


def test_azure_transport_failure_is_recorded_without_aborting_matrix(monkeypatch, tmp_path, capsys):
    def fail(*_args, **_kwargs):
        raise ServiceRequestError("PRIVATE-ENDPOINT")

    monkeypatch.setattr(benchmark_engines, "run_file", fail)
    row = benchmark_engines.run_case(
        tmp_path / "synthetic.pdf", tmp_path, "it", "en", "translator", "static", "low", False, False,
    )
    assert row["status"] == "failed"
    assert row["errorType"] == "ServiceRequestError"
    assert "PRIVATE-ENDPOINT" not in capsys.readouterr().err


@pytest.mark.parametrize("status,expected", [("complete", 0), ("needs_review", 0), ("failed", 1)])
def test_benchmark_exit_code_reports_execution_failures(monkeypatch, tmp_path, status, expected):
    samples = tmp_path / "inputs"
    samples.mkdir()
    (samples / "synthetic.pdf").write_bytes(b"synthetic")
    monkeypatch.setattr(sys, "argv", [
        "benchmark_engines.py", "--samples", str(samples), "--output-dir", str(tmp_path / "results"),
        "--engines", "translator", "--glossary-modes", "static", "--reasoning-efforts", "low",
    ])
    monkeypatch.setattr(benchmark_engines, "run_case", lambda *_: {"engine": "translator", "status": status})
    assert benchmark_engines.main() == expected
    assert (tmp_path / "results" / "benchmark.csv").exists()
    assert (tmp_path / "results" / "benchmark.md").exists()
