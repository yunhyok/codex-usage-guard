import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]


def _ps(script: Path, data_dir: Path, payload: str, event: str = "PostToolUse"):
    env = os.environ.copy(); env["CODEX_USAGE_GUARD_DATA_DIR"] = str(data_dir)
    return subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-EventName", event], input=payload, text=True, encoding="utf-8", capture_output=True, env=env, timeout=15)


def test_hook_real_powershell_heartbeat_recursion_and_corrupt_cache():
    with tempfile.TemporaryDirectory() as temp:
        data = Path(temp); (data / "state.json").write_text("not-json", encoding="utf-8")
        for name in ("mcp__codex_usage_guard__doctor", "mcp__codex_usage_guard_codex_usage_guard__evaluate_usage_guard", "mcp__plugin_codex-usage-guard_codex_usage_guard__get_usage_status"):
            own = _ps(ROOT / "scripts/hook_usage_context.ps1", data, json.dumps({"tool_name": name, "tool_input": {"secret": "must-not-echo"}}))
            assert own.returncode == 0 and json.loads(own.stdout)["hookSpecificOutput"]["additionalContext"] == ""
        assert not (data / "post-hook.txt").exists()
        with ThreadPoolExecutor(max_workers=8) as pool:
            outputs = list(pool.map(lambda _: _ps(ROOT / "scripts/hook_usage_context.ps1", data, '{"tool_name":"other_tool","tool_response":{"token":"secret"}}'), range(8)))
        assert all(item.returncode == 0 for item in outputs)
        assert (data / "heartbeat.txt").exists()
        assert all("secret" not in item.stdout for item in outputs)
        contexts = [json.loads(item.stdout)["hookSpecificOutput"]["additionalContext"] for item in outputs]
        assert sum(bool(value) for value in contexts) == 1


def test_session_hook_uses_installed_unicode_path_and_fails_if_skill_missing(tmp_path):
    package = tmp_path / "한글 plugin with spaces"
    shutil.copytree(ROOT / "scripts", package / "scripts")
    shutil.copytree(ROOT / "skills", package / "skills")
    hook = package / "scripts/hook_usage_context.ps1"
    result = _ps(hook, tmp_path / "data", '{}', "SessionStart")
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert result.returncode == 0 and output["hookEventName"] == "SessionStart"
    skill = package / "skills/usage-aware-workflow/SKILL.md"
    assert str(skill) in output["additionalContext"] and str(ROOT) not in output["additionalContext"]
    skill.unlink()
    missing = _ps(hook, tmp_path / "data", '{}', "SessionStart")
    assert missing.returncode != 0 and not missing.stdout.strip()


def test_launcher_runs_installed_server_with_existing_private_runtime(tmp_path):
    data = tmp_path / "data"
    runtime = data / "runtime"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(runtime)], check=True, timeout=30)
    package = tmp_path / "한글 cached plugin"
    package.mkdir()
    shutil.copy2(ROOT / "run-mcp.ps1", package / "run-mcp.ps1")
    server = package / "usage_guard_mcp.py"
    server.write_text("import json, os\nprint(json.dumps({'server': __file__, 'data': os.environ['CODEX_USAGE_GUARD_DATA_DIR']}))\n", encoding="utf-8")
    config = {"schemaVersion": 1, "python": str(runtime / "Scripts/python.exe"), "dataDir": str(data), "server": str(tmp_path / "removed/server.py"), "pluginRoot": str(tmp_path / "removed")}
    config_path = data / "plugin-config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    env = {**os.environ, "CODEX_USAGE_GUARD_DATA_DIR": str(data)}
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(package / "run-mcp.ps1")]
    result = subprocess.run(command, text=True, encoding="utf-8", errors="replace", capture_output=True, env=env, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"server": str(server), "data": str(data)}
    config["python"] = sys.executable
    config_path.write_text(json.dumps(config), encoding="utf-8")
    rejected = subprocess.run(command, text=True, encoding="utf-8", errors="replace", capture_output=True, env=env, timeout=15)
    assert rejected.returncode != 0 and "private runtime" in rejected.stderr


def test_launcher_rejects_invalid_schema_without_global_python_fallback():
    with tempfile.TemporaryDirectory() as temp:
        data = Path(temp); (data / "plugin-config.json").write_text(json.dumps({"schemaVersion": 99}), encoding="utf-8")
        env = os.environ.copy(); env["CODEX_USAGE_GUARD_DATA_DIR"] = str(data)
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "run-mcp.ps1")], capture_output=True, text=True, env=env, timeout=15)
        assert result.returncode != 0 and "schema" in (result.stdout + result.stderr).lower()


def test_release_archive_contains_allowlisted_files_only(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", "dist"))
    archive = source / "dist" / "test-runtime.zip"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(source / "scripts/package-release.ps1"), "-Development", "-Output", str(archive)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(archive) as zf:
        assert not any("__pycache__" in name or ".venv" in name for name in zf.namelist())


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
