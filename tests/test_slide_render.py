from pathlib import Path

from services import slide_render


def test_finds_standard_windows_installation_without_path(monkeypatch, tmp_path):
    executable = tmp_path / "LibreOffice" / "program" / "soffice.com"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(slide_render.shutil, "which", lambda _: None)
    monkeypatch.setattr(slide_render.sys, "platform", "win32")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))

    assert slide_render._local_renderer() == str(executable)


def test_headless_render_passes_absolute_profile_uri(monkeypatch):
    monkeypatch.setattr(slide_render, "_local_renderer", lambda: "soffice")

    def run(command, **options):
        source = Path(command[-1])
        assert command[1] == f"-env:UserInstallation={(source.parent / 'profile').as_uri()}"
        assert "--headless" in command
        assert source.read_bytes() == b"synthetic-slide"
        assert options["check"]
        source.with_suffix(".pdf").write_bytes(b"%PDF-synthetic")

    monkeypatch.setattr(slide_render.subprocess, "run", run)
    assert slide_render.render(b"synthetic-slide") == b"%PDF-synthetic"
