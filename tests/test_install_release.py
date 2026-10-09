"""Isolated Windows install/release-path checks; never touch the user's marketplace."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PS = shutil.which("powershell.exe") or shutil.which("pwsh")


def run_ps(script: Path, *args: str, env: dict[str, str] | None = None, check: bool = True):
    if not PS:
        pytest.skip("PowerShell is not available")
    e = os.environ.copy()
    if env:
        e.update(env)
    p = subprocess.run(
        [PS, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=e,
    )
    if check and p.returncode:
        raise AssertionError(f"PowerShell failed ({p.returncode}): {p.stdout}\n{p.stderr}")
    return p


@pytest.fixture()
def isolated(tmp_path: Path):
    source = tmp_path / "source"
    shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", "dist"))
    local = tmp_path / "localappdata"
    data = local / "OpenAI" / "codex-usage-guard" / "runtime" / "Scripts"
    data.mkdir(parents=True)
    (data / "python.exe").write_text("stub", encoding="ascii")
    market = tmp_path / "marketplace.json"
    market.write_text(json.dumps({"name": "personal", "plugins": [{"name": "other", "source": {"source": "local", "path": "keep"}}]}, indent=2) + "\n", encoding="utf-8")
    plugin_root = tmp_path / "plugins"
    env = {"LOCALAPPDATA": str(local), "USERPROFILE": str(tmp_path / "profile")}
    return source, local, market, plugin_root, env


def test_marketplace_install_is_targeted_and_idempotent(isolated):
    source, _local, market, plugin_root, env = isolated
    args = ("-SkipRuntime", "-MarketplacePath", str(market), "-PluginInstallRoot", str(plugin_root))
    run_ps(source / "install.ps1", *args, env=env)
    config = json.loads((_local / "OpenAI" / "codex-usage-guard" / "plugin-config.json").read_text(encoding="utf-8"))
    target = plugin_root / "codex-usage-guard"
    assert Path(config["pluginRoot"]).resolve() == target.resolve()
    assert Path(config["server"]).resolve() == (target / "usage_guard_mcp.py").resolve()
    obj = json.loads(market.read_text(encoding="utf-8"))
    plugin = next(p for p in obj["plugins"] if p["name"] == "codex-usage-guard")
    assert plugin["source"]["path"] == "./plugins/codex-usage-guard"
    assert not (target / ".env").exists()
    (source / ".env").write_text("DO_NOT_COPY=1", encoding="ascii")
    run_ps(source / "install.ps1", *args, env=env)
    assert len([p for p in json.loads(market.read_text(encoding="utf-8"))["plugins"] if p["name"] == "codex-usage-guard"]) == 1
    moved = source.with_name("source-renamed")
    source.rename(moved)
    assert Path(config["server"]).is_file()


@pytest.mark.parametrize("layout", ["personal", "repo"])
def test_marketplace_root_is_derived_and_mismatch_rejected(isolated, layout):
    source, _local, _market, _plugin_root, env = isolated
    base = source.parent / layout
    market = base / ".agents" / "plugins" / "marketplace.json" if layout == "personal" else base / "marketplace.json"
    expected = base / "plugins"
    market.parent.mkdir(parents=True, exist_ok=True); market.write_text(json.dumps({"name": "personal", "plugins": []}), encoding="utf-8")
    wrong = source.parent / "wrong-plugins"
    failed = run_ps(source / "install.ps1", "-SkipRuntime", "-MarketplacePath", str(market), "-PluginInstallRoot", str(wrong), env=env, check=False)
    assert failed.returncode != 0 and not (wrong / "codex-usage-guard").exists()
    run_ps(source / "install.ps1", "-SkipRuntime", "-MarketplacePath", str(market), "-PluginInstallRoot", str(expected), env=env)
    assert (expected / "codex-usage-guard" / ".codex-plugin" / "plugin.json").exists()


def test_install_rollback_preserves_old_target_and_marketplace_bytes(isolated):
    source, _local, market, plugin_root, env = isolated
    args = ("-SkipRuntime", "-MarketplacePath", str(market), "-PluginInstallRoot", str(plugin_root))
    run_ps(source / "install.ps1", *args, env=env)
    target = plugin_root / "codex-usage-guard"
    old_readme = (target / "README.md").read_bytes()
    old_market = market.read_bytes()
    (source / "README.md").write_text("changed source", encoding="utf-8")
    failed = run_ps(source / "install.ps1", *args, env={**env, "CODEX_USAGE_GUARD_TEST_FAIL_STAGE": "after-target"}, check=False)
    assert failed.returncode != 0
    assert (target / "README.md").read_bytes() == old_readme
    assert market.read_bytes() == old_market


def test_first_install_failure_removes_new_target_config_and_marketplace(isolated):
    source, local, market, plugin_root, env = isolated
    market.unlink()
    args = ("-SkipRuntime", "-MarketplacePath", str(market), "-PluginInstallRoot", str(plugin_root))
    failed = run_ps(source / "install.ps1", *args, env={**env, "CODEX_USAGE_GUARD_TEST_FAIL_CONFIG": "1"}, check=False)
    assert failed.returncode != 0
    assert not (plugin_root / "codex-usage-guard").exists()
    assert not (local / "OpenAI" / "codex-usage-guard" / "plugin-config.json").exists()
    assert not market.exists()


def test_force_runtime_failure_preserves_existing_runtime_and_config(isolated):
    source, local, _market, _plugin_root, env = isolated
    runtime = local / "OpenAI" / "codex-usage-guard" / "runtime"; marker = runtime / "keep.txt"; marker.write_text("old", encoding="ascii")
    config = local / "OpenAI" / "codex-usage-guard" / "plugin-config.json"; config.write_text('{"schemaVersion":1,"pluginRoot":"old"}', encoding="ascii"); old_config = config.read_bytes()
    failed = run_ps(source / "install.ps1", "-Force", env={**env, "CODEX_USAGE_GUARD_TEST_FAIL_RUNTIME": "before-venv"}, check=False)
    assert failed.returncode != 0 and marker.read_text(encoding="ascii") == "old" and config.read_bytes() == old_config


def test_late_config_failure_removes_new_runtime_without_backup(isolated):
    source, local, _market, _plugin_root, env = isolated
    runtime = local / "OpenAI" / "codex-usage-guard" / "runtime"
    shutil.rmtree(runtime)
    failed = run_ps(source / "install.ps1", "-Force", env={**env, "CODEX_USAGE_GUARD_TEST_SKIP_PIP": "1", "CODEX_USAGE_GUARD_TEST_FAIL_CONFIG": "1"}, check=False)
    assert failed.returncode != 0
    assert not runtime.exists() and not (local / "OpenAI" / "codex-usage-guard" / "plugin-config.json").exists()


@pytest.mark.parametrize("failure", ["CODEX_USAGE_GUARD_TEST_FAIL_CONFIG", "CODEX_USAGE_GUARD_TEST_FAIL_MARKETPLACE"])
def test_install_injected_late_failures_restore_target_config_and_marketplace(isolated, failure):
    source, local, market, plugin_root, env = isolated
    args = ("-SkipRuntime", "-MarketplacePath", str(market), "-PluginInstallRoot", str(plugin_root))
    run_ps(source / "install.ps1", *args, env=env)
    target = plugin_root / "codex-usage-guard"
    old_target = (target / "README.md").read_bytes(); old_market = market.read_bytes(); config_path = local / "OpenAI" / "codex-usage-guard" / "plugin-config.json"; old_config = config_path.read_bytes()
    failed = run_ps(source / "install.ps1", *args, env={**env, failure: "1"}, check=False)
    assert failed.returncode != 0 and (target / "README.md").read_bytes() == old_target and market.read_bytes() == old_market and config_path.read_bytes() == old_config


def test_reinstall_and_uninstall_are_scoped(isolated):
    source, local, market, plugin_root, env = isolated
    args = ("-SkipRuntime", "-MarketplacePath", str(market), "-PluginInstallRoot", str(plugin_root))
    run_ps(source / "reinstall.ps1", *args, env=env)
    assert (plugin_root / "codex-usage-guard").is_dir()
    run_ps(source / "uninstall.ps1", "-MarketplacePath", str(market), "-PluginInstallRoot", str(plugin_root), env=env)
    names = [p["name"] for p in json.loads(market.read_text(encoding="utf-8"))["plugins"]]
    assert names == ["other"]
    assert not (plugin_root / "codex-usage-guard").exists()
    assert not (local / "OpenAI" / "codex-usage-guard" / "plugin-config.json").exists()
    # A second uninstall is a no-op for the missing target and remains scoped.
    run_ps(source / "uninstall.ps1", "-MarketplacePath", str(market), "-PluginInstallRoot", str(plugin_root), env=env)
    assert [p["name"] for p in json.loads(market.read_text(encoding="utf-8"))["plugins"]] == ["other"]


def test_source_checkout_uninstall_never_deletes_checkout(isolated):
    source, local, _market, _plugin_root, env = isolated
    run_ps(source / "install.ps1", "-SkipRuntime", env=env)
    run_ps(source / "uninstall.ps1", "-KeepVenv", env=env)
    assert source.exists() and (source / "install.ps1").exists()
    assert not (local / "OpenAI" / "codex-usage-guard" / "plugin-config.json").exists()


def test_uninstall_failure_restores_target_and_exact_files(isolated):
    source, local, market, plugin_root, env = isolated
    args = ("-SkipRuntime", "-MarketplacePath", str(market), "-PluginInstallRoot", str(plugin_root))
    run_ps(source / "install.ps1", *args, env=env)
    target = plugin_root / "codex-usage-guard"; config_path = local / "OpenAI" / "codex-usage-guard" / "plugin-config.json"
    old_target = (target / "README.md").read_bytes(); old_market = market.read_bytes(); old_config = config_path.read_bytes()
    failed = run_ps(source / "uninstall.ps1", *args[1:], env={**env, "CODEX_USAGE_GUARD_TEST_FAIL_UNINSTALL_MARKETPLACE": "1"}, check=False)
    assert failed.returncode != 0 and target.exists() and (target / "README.md").read_bytes() == old_target and market.read_bytes() == old_market and config_path.read_bytes() == old_config


def test_launcher_rejects_relative_config_paths(tmp_path: Path):
    data = tmp_path / "data"; data.mkdir(); (data / "plugin-config.json").write_text(json.dumps({"schemaVersion": 1, "python": "python.exe", "server": "server.py", "pluginRoot": "plugin", "dataDir": str(data)}), encoding="utf-8")
    result = run_ps(ROOT / "run-mcp.ps1", env={"CODEX_USAGE_GUARD_DATA_DIR": str(data)}, check=False)
    assert result.returncode != 0 and "absolute" in (result.stdout + result.stderr).lower()


def test_launcher_rejects_tampered_data_dir(tmp_path: Path):
    data = tmp_path / "data"; other = tmp_path / "other"; data.mkdir(); other.mkdir(); plugin = tmp_path / "plugin"; plugin.mkdir(); (plugin / "usage_guard_mcp.py").write_text("", encoding="ascii"); python = data / "runtime" / "Scripts"; python.mkdir(parents=True); (python / "python.exe").write_text("", encoding="ascii")
    (data / "plugin-config.json").write_text(json.dumps({"schemaVersion": 1, "python": str(python / "python.exe"), "server": str(plugin / "usage_guard_mcp.py"), "pluginRoot": str(plugin), "dataDir": str(other)}), encoding="utf-8")
    result = run_ps(ROOT / "run-mcp.ps1", env={"CODEX_USAGE_GUARD_DATA_DIR": str(data)}, check=False)
    assert result.returncode != 0 and "datadir" in (result.stdout + result.stderr).lower()


def test_hook_reclaims_old_lock_and_rejects_bad_cache_schema(tmp_path: Path):
    data = tmp_path / "hook-data"; data.mkdir()
    now = int(time.time())
    (data / "state.json").write_text(json.dumps({"schemaVersion": 99, "fetchedAt": now, "minRemainingPercent": 20, "decision": "proceed"}), encoding="utf-8")
    (data / "post-hook.txt").write_text(str(now - 700), encoding="ascii")
    lock = data / "post-hook.txt.lock"; lock.write_text("stale", encoding="ascii"); old = time.time() - 60; os.utime(lock, (old, old))
    result = subprocess.run([PS, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts/hook_usage_context.ps1"), "-EventName", "PostToolUse"], input='{"tool_name":"other_tool"}', text=True, encoding="utf-8", errors="replace", capture_output=True, env={**os.environ, "CODEX_USAGE_GUARD_DATA_DIR": str(data)})
    assert result.returncode == 0 and not lock.exists()
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "unknown or stale" in context


def test_package_rejects_dirty_head_and_clean_archive_is_allowlisted(tmp_path: Path):
    if not PS:
        pytest.skip("PowerShell is not available")
    repo = tmp_path / "repo"
    shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", "dist", "tests"))
    (repo / ".git").mkdir()
    def git(*args: str):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True, capture_output=True)
    git("init", "-q"); git("config", "user.email", "test@example.invalid"); git("config", "user.name", "Test"); git("add", "."); git("commit", "-qm", "initial"); git("tag", "-a", "v0.2.1", "-m", "release")
    original = (repo / "README.md").read_bytes()
    (repo / "README.md").write_bytes(original + b"dirty\n")
    script = repo / "scripts" / "package-release.ps1"
    failed = run_ps(script, "-Ref", "HEAD", "-Output", "dist/dirty.zip", check=False)
    assert failed.returncode != 0
    (repo / "README.md").write_bytes(original)
    run_ps(script, "-Development", "-Ref", "HEAD", "-Output", "dist/release.zip")
    run_ps(script, "-Ref", "v0.2.1", "-Output", "dist/tagged.zip")
    (repo / "README.md").write_bytes(original + b"next commit\n")
    git("add", "README.md")
    parent = git("rev-parse", "HEAD").stdout.strip()
    tree = git("write-tree").stdout.strip()
    # Match the first hex digit so truncating a SHA cannot pass this check.
    for attempt in range(256):
        next_commit = git("commit-tree", tree, "-p", parent, "-m", f"next {attempt}").stdout.strip()
        if next_commit[0] == parent[0]:
            break
    else:
        raise AssertionError("could not construct same-prefix commit")
    git("update-ref", "HEAD", next_commit)
    stale = run_ps(script, "-Ref", "v0.2.1", "-Output", "dist/stale.zip", check=False)
    assert stale.returncode != 0
    archive = repo / "dist" / "release.zip"
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        assert ".codex-plugin/plugin.json" in names
        assert not any("/tests/" in f"/{n}" or n.endswith("/.env") or "__pycache__" in n for n in names)
        manifest = json.loads(zf.read(".codex-plugin/plugin.json"))
        assert manifest["version"] == "0.2.1"
    expected = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert expected.lower() in (archive.with_suffix(".zip.sha256")).read_text(encoding="ascii").lower()


def test_package_non_git_checkout_creates_real_zip(tmp_path: Path):
    repo = tmp_path / "nogit"
    shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", "dist", "tests"))
    output = repo / "dist" / "nogit.zip"
    run_ps(repo / "scripts" / "package-release.ps1", "-Development", "-Output", str(output))
    assert zipfile.is_zipfile(output)
    with zipfile.ZipFile(output) as zf:
        assert ".codex-plugin/plugin.json" in zf.namelist()


def test_package_rejects_sk_proj_secret_content(tmp_path: Path):
    repo = tmp_path / "nogit-secret"
    shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", "dist", "tests"))
    readme = repo / "README.md"; original = readme.read_bytes()
    fake_secret = b"sk-" + b"proj-" + b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    readme.write_bytes(original + b"\n" + fake_secret + b"\n")
    result = run_ps(repo / "scripts" / "package-release.ps1", "-Development", "-Output", str(repo / "dist" / "secret.zip"), check=False)
    assert result.returncode != 0
