import json
import asyncio
import inspect
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_manifest_and_mcp_contract():
    manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "codex-usage-guard"
    assert manifest["version"] == "0.1.1"
    assert manifest["interface"]["displayName"] == "Codex Usage Guard v0.1.1"
    assert 'version = "0.1.1"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'APP_VERSION = "0.1.1"' in (ROOT / "app_server.py").read_text(encoding="utf-8")
    assert isinstance(manifest["interface"]["defaultPrompt"], list)
    assert "hooks" not in manifest
    config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    assert config["mcpServers"]["codex_usage_guard"]["command"].lower() == "powershell.exe"


def test_exact_tool_names_and_defaults():
    import usage_guard_mcp
    assert set(inspect.signature(usage_guard_mcp.get_usage_status).parameters) == {"force_refresh", "include_activity", "timeout_sec"}
    assert inspect.signature(usage_guard_mcp.get_usage_status).parameters["force_refresh"].default is True
    assert inspect.signature(usage_guard_mcp.get_usage_status).parameters["include_activity"].default is False
    assert inspect.signature(usage_guard_mcp.get_usage_status).parameters["timeout_sec"].default == 15
    tools = asyncio.run(usage_guard_mcp.mcp.list_tools())
    assert {tool.name for tool in tools} == {"get_usage_status", "evaluate_usage_guard", "doctor"}
    evaluate_schema = next(tool.inputSchema for tool in tools if tool.name == "evaluate_usage_guard")
    assert evaluate_schema["properties"]["purpose"]["enum"] == ["task_start", "before_delegate", "before_build", "before_external_cli", "checkpoint", "manual"]
    assert evaluate_schema["properties"]["purpose"]["default"] == "checkpoint"


def test_hooks_are_advisory_and_rate_limited():
    hooks = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert "UserPromptSubmit" in hooks and "PostToolUse" in hooks
    post = hooks["PostToolUse"][0]
    assert post["matcher"] == "" and "(?" not in post["matcher"]
    command = post["hooks"][0]
    assert "commandWindows" in command and "PLUGIN_ROOT" in command["commandWindows"]
    script = (ROOT / "scripts/hook_usage_context.ps1").read_text(encoding="utf-8")
    assert "hookSpecificOutput" in script and "600" in script and "continue = $false" not in script


def test_shared_cache_root_is_explicit_and_common():
    provider = (ROOT / "app_server.py").read_text(encoding="utf-8")
    hook = (ROOT / "scripts/hook_usage_context.ps1").read_text(encoding="utf-8")
    assert "CODEX_USAGE_GUARD_DATA_DIR" in provider and "CODEX_USAGE_GUARD_DATA_DIR" in hook
    assert "OpenAI" in provider and "codex-usage-guard" in provider and "OpenAI\\codex-usage-guard" in hook


def test_runtime_is_external_to_cached_plugin_copy():
    installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
    launcher = (ROOT / "run-mcp.ps1").read_text(encoding="utf-8")
    assert "plugin-config.json" in installer and "OpenAI\\codex-usage-guard" in installer
    assert "plugin-config.json" in launcher
