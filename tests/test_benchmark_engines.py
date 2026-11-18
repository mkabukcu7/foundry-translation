import sys
from zipfile import ZipFile

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
    added_values = exact_values("EUR 100 POL-123", "EUR 100 and EUR 900 POL-123 POL-456")
    assert not added_values["numbers"]["exact"]
    assert not added_values["currency"]["exact"]
    assert not added_values["ids"]["exact"]
    assert not exact_values("", "EUR 900")["currency"]["exact"]
    assert not exact_values("100 100", "100 100 100")["numbers"]["exact"]


def test_terminology_score_and_structure_comparison():
    terms = [
        {"term": "franchigia", "action": "map", "target": "deductible"},
        {"term": "POL-123", "action": "keep", "target": None},
    ]
    assert terminology_score(terms, "deductible POL-123") == 100
    assert terminology_score(terms, "excess POL-123") == 50
    assert terminology_score(
        [{"term": "insured", "action": "keep", "target": None}],
        "The uninsured property remains insured.",
    ) == 100
    assert terminology_score(
        [{"term": "insured", "action": "keep", "target": None}], "uninsured property",
    ) == 0
    assert terminology_score(
        [{"term": "POL-123", "action": "keep", "target": None}], "POL-1234",
    ) == 0
    assert terminology_score([], "") is None
    assert structure_matches({"pages": 2, "images": 1}, {"pages": 2, "images": 1})
    assert not structure_matches({"pages": 2}, {"pages": 1})


def test_xlsx_structure_uses_existing_xml_support(tmp_path):
    path = tmp_path / "synthetic.xlsx"
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheets><sheet name="Policy" sheetId="1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Policy</t></is></c></row>'
            '</sheetData></worksheet>',
        )
        archive.writestr("xl/tables/table1.xml", "<table/>")
        archive.writestr("xl/media/image1.png", b"image")
    assert benchmark_engines.document_structure(path) == {
        "pages": None, "slides": None, "sheets": 1, "paragraphs": 1,
        "tables": 1, "images": 1,
    }


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


def test_benchmark_postprocessing_failure_is_recorded_per_case(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(benchmark_engines, "run_file", lambda *_args, **_kwargs: {
        "outputFile": "translated.pdf", "translation": {}, "status": "complete",
    })
    row = benchmark_engines.run_case(
        tmp_path / "synthetic.pdf", tmp_path, "it", "en", "translator", "static", "low", False, False,
    )
    assert row["status"] == "failed"
    assert row["errorType"] == "FileNotFoundError"
    assert "FileNotFoundError" in capsys.readouterr().err


def test_summary_includes_only_engines_in_recorded_cases(tmp_path):
    path = tmp_path / "summary.md"
    benchmark_engines.write_summary([{
        "engine": "translator", "status": "not_reviewed", "wallSeconds": 1.0,
        "terminologyScore": 90.0, "estimatedCostUSD": None, "maturity": "GA",
    }], path)
    summary = path.read_text()
    assert "| translator | GA | 1.00 | 90.00 | unknown |" in summary
    assert "gpt6_luna" not in summary


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
