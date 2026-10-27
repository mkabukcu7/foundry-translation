import json

import pytest

from scripts.verify_local_demo import verify


def create_output(folder, *, secondary=False):
    folder.mkdir()
    report = {
        "jobId": "a" * 32,
        "status": "complete",
        "passB": [{"score": 100, "issues": []}],
        "versions": [{"agent": "translation-checker"}],
        "checkerAgreement": [],
        "costSignals": {"tokens": 1},
    }
    if secondary:
        report["versions"].append({"agent": "translation-checker-secondary"})
    (folder / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (folder / "translated.txt").write_text(
        "POL-2024-558812 SIN-2025-004471 Giulia Rossetti water damage EUR 500,00",
        encoding="utf-8",
    )
    return report


def test_verify_accepts_single_checker_output_without_agreement(tmp_path, capsys):
    folder = tmp_path / "single"
    create_output(folder)
    report_path = folder / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.pop("checkerAgreement")
    report_path.write_text(json.dumps(report), encoding="utf-8")

    verify(folder)

    assert "syntheticOutputVerified" in capsys.readouterr().out


def test_verify_requires_agreement_for_secondary_checker(tmp_path):
    folder = tmp_path / "dual"
    create_output(folder, secondary=True)
    with pytest.raises(AssertionError, match="Independent checker results missing"):
        verify(folder)
