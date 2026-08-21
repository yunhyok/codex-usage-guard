import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]


def _ps(script: Path, data_dir: Path, payload: str, event: str = "PostToolUse"):
    env = os.environ.copy(); env["CODEX_USAGE_GUARD_DATA_DIR"] = str(data_dir)
    return subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-EventName", event], input=payload, text=True, capture_output=True, env=env, timeout=15)


def test_hook_real_powershell_heartbeat_recursion_and_corrupt_cache():
    with tempfile.TemporaryDirectory() as temp:
        data = Path(temp); (data / "state.json").write_text("not-json", encoding="utf-8")
        own = _ps(ROOT / "scripts/hook_usage_context.ps1", data, '{"tool_name":"mcp__codex_usage_guard__doctor","tool_input":{"secret":"must-not-echo"}}')
        assert own.returncode == 0 and json.loads(own.stdout)["hookSpecificOutput"]["additionalContext"] == ""
        with ThreadPoolExecutor(max_workers=8) as pool:
            outputs = list(pool.map(lambda _: _ps(ROOT / "scripts/hook_usage_context.ps1", data, '{"tool_name":"other_tool","tool_response":{"token":"secret"}}'), range(8)))
        assert all(item.returncode == 0 for item in outputs)
        assert (data / "heartbeat.txt").exists()
        assert all("secret" not in item.stdout for item in outputs)
        contexts = [json.loads(item.stdout)["hookSpecificOutput"]["additionalContext"] for item in outputs]
        assert sum(bool(value) for value in contexts) == 1


def test_launcher_rejects_invalid_schema_without_global_python_fallback():
    with tempfile.TemporaryDirectory() as temp:
        data = Path(temp); (data / "plugin-config.json").write_text(json.dumps({"schemaVersion": 99}), encoding="utf-8")
        env = os.environ.copy(); env["CODEX_USAGE_GUARD_DATA_DIR"] = str(data)
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "run-mcp.ps1")], capture_output=True, text=True, env=env, timeout=15)
        assert result.returncode != 0 and "schema" in (result.stdout + result.stderr).lower()


def test_release_archive_contains_allowlisted_files_only():
    with tempfile.TemporaryDirectory() as temp:
        output = Path(temp) / "artifact.zip"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts/package-release.ps1"), "-Development", "-Output", str(ROOT / "dist" / "test-runtime.zip")], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0
        archive = ROOT / "dist" / "test-runtime.zip"
        try:
            with zipfile.ZipFile(archive) as zf:
                names = zf.namelist()
                assert not any("__pycache__" in name or ".venv" in name for name in names)
        finally:
            archive.unlink(missing_ok=True); (archive.with_suffix(archive.suffix + ".sha256")).unlink(missing_ok=True)


def test_personal_marketplace_plugins_upsert_preserves_existing_entry():
    with tempfile.TemporaryDirectory() as temp:
        home = Path(temp); local = home / "local"; local.mkdir(); market = home / ".agents" / "plugins" / "marketplace.json"; market.parent.mkdir(parents=True)
        market.write_text(json.dumps({"name": "personal", "interface": {"displayName": "Personal"}, "plugins": [{"name": "integrated-agent-workflow", "source": {"source": "local", "path": "./plugins/integrated-agent-workflow"}, "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": "Productivity"}]}), encoding="utf-8")
        env = os.environ.copy(); env["LOCALAPPDATA"] = str(local)
        plugin_root = home / "plugins"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "install.ps1"), "-SkipRuntime", "-MarketplacePath", str(market), "-PluginInstallRoot", str(plugin_root)], capture_output=True, text=True, env=env, timeout=30)
        assert result.returncode == 0, result.stderr
        saved = json.loads(market.read_text(encoding="utf-8-sig")); names = [item["name"] for item in saved["plugins"]]
        assert names == ["integrated-agent-workflow", "codex-usage-guard"]
        assert saved["plugins"][1]["source"]["source"] == "local"
        assert (plugin_root / "codex-usage-guard" / ".codex-plugin" / "plugin.json").exists()
        sentinel = plugin_root / "codex-usage-guard" / "stale-sentinel.txt"; sentinel.write_text("stale", encoding="utf-8")
        second = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "install.ps1"), "-SkipRuntime", "-MarketplacePath", str(market), "-PluginInstallRoot", str(plugin_root)], capture_output=True, text=True, env=env, timeout=30)
        assert second.returncode == 0 and not sentinel.exists() and not (plugin_root / "codex-usage-guard" / "Plugin" / ".codex-plugin").exists()
        removed = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "uninstall.ps1"), "-KeepVenv", "-MarketplacePath", str(market), "-PluginInstallRoot", str(plugin_root)], capture_output=True, text=True, env=env, timeout=30)
        assert removed.returncode == 0
        assert [item["name"] for item in json.loads(market.read_text(encoding="utf-8-sig"))["plugins"]] == ["integrated-agent-workflow"]
