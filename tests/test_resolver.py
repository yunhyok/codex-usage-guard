from pathlib import Path
from types import SimpleNamespace

import app_server


def test_versioned_candidate_beats_old_path(monkeypatch, tmp_path):
    old = tmp_path / "old.exe"; new = tmp_path / "new.exe"; old.write_text(""); new.write_text("")
    monkeypatch.delenv("CODEX_USAGE_GUARD_CODEX", raising=False)
    monkeypatch.setattr(app_server, "find_candidates", lambda: [old, new])
    def run(command, **kwargs):
        version = "0.130.0-alpha.5" if command[0] == str(old) else "0.148.0-alpha.15"
        return SimpleNamespace(returncode=0, stdout=f"codex-cli {version}", stderr="")
    monkeypatch.setattr(app_server.subprocess, "run", run)
    selected, _ = app_server.find_codex_executable()
    assert selected == new


def test_explicit_override_has_priority(monkeypatch, tmp_path):
    override = tmp_path / "override.exe"; override.write_text("")
    monkeypatch.setenv("CODEX_USAGE_GUARD_CODEX", str(override))
    monkeypatch.setattr(app_server, "find_candidates", lambda: [])
    selected, _ = app_server.find_codex_executable()
    assert selected == override.resolve()
