# Codex Usage Guard v0.2.0

Codex Usage Guard is a Windows-first Codex plugin that checks the official Codex App Server rate limits before expensive implementation, delegation, builds, and reviews. It is advisory: it cannot cancel the currently running request.

## Tools

- `get_usage_status(force_refresh=True, include_activity=False, timeout_sec=15)` returns sanitized rate-limit windows and optional `account/usage/read` activity summaries.
- `evaluate_usage_guard(purpose="checkpoint", force_refresh=True, timeout_sec=15)` applies the fixed policy: >40 proceed, >25-40 watch, >10-25 checkpoint, <=10 or reached critical. Freshness is strict: age >=120 seconds is unknown.
- `doctor(timeout_sec=15)` reports best-effort CLI resolver/runtime health without reading `config.toml`, credentials, or tokens.

The provider starts one persistent `codex app-server -c service_tier="fast"` child, merges `account/rateLimits/updated`, polls every 60 seconds, coalesces concurrent refreshes, and stores only a minimal atomic sanitized cache. `CODEX_USAGE_GUARD_CODEX` can select a specific executable. Versioned `%LOCALAPPDATA%\OpenAI\Codex\bin\<version>\codex.exe` children are discovered automatically.

## Workbench marketplace

Prerequisites: Windows PowerShell 5.1+, Python 3.11+, Git, and access to the private `yunhyok/workbench` repository. Install the plugin with a current Codex CLI:

```powershell
codex plugin marketplace add https://github.com/yunhyok/workbench.git
codex plugin add codex-usage-guard@workbench
```

On each PC, prepare the private Python runtime once from the installed plugin. This does not edit the marketplace or trust hooks:

```powershell
$usageGuardCodexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
& (Join-Path $usageGuardCodexRoot 'plugins/cache/workbench/codex-usage-guard/0.2.0/install.ps1')
```

The runtime lives under `%LOCALAPPDATA%\OpenAI\codex-usage-guard\runtime`. The launcher validates the configured private Python and data directory, then runs the server from its own installed directory. Old machine-local configs remain compatible; obsolete `server`/`pluginRoot` paths no longer pin the executable source. Plugin updates therefore use the new server and skill without reinstalling unchanged Python dependencies. A custom `CODEX_USAGE_GUARD_DATA_DIR` must already contain a valid runtime/config; the installer prepares the default location only.

Restart Codex, review and trust **Codex Usage Guard** hooks in the hook manager (interactive CLI: `/hooks`), then start a new chat. Installation does not grant hook trust; changed definitions require review again. Explicit skill invocation is `codex-usage-guard:usage-aware-workflow`.

- `SessionStart` asks Codex to read the installed skill at startup, resume, clear, and compact.
- `UserPromptSubmit` adds cached usage context or an unknown/stale advisory.
- `PostToolUse` adds an advisory at most once per 10 minutes and suppresses this plugin's own MCP calls.

Hooks never block or cancel tool calls. Missing runtime is reported as unavailable, not as a successful usage check. Disable the hooks to stop automatic application; explicit skill invocation remains available.

To update, refresh the marketplace and installed plugin, then restart Codex:

```powershell
codex plugin marketplace upgrade workbench
codex plugin add codex-usage-guard@workbench
```

Restart alone does not guarantee that the newest GitHub release was downloaded. If `codex-usage-guard@personal` is also installed, remove that plugin after verifying Workbench; keep the shared runtime. Do not run the legacy runtime uninstaller just to remove a duplicate catalog entry.

## Local development / Personal marketplace

From a source checkout, `./install.ps1` prepares the runtime without editing a marketplace. The optional Personal setup copies an explicit release allowlist to `./plugins/codex-usage-guard` and preserves other catalog entries. For this local-copy setup, re-run the installer with the same arguments after source changes.

Prerequisite: use a compatible bundled/current Codex CLI that exposes `codex plugin add`, `codex plugin remove`, and `codex plugin list`; if the PATH CLI reports a configuration-compatibility error, use the compatible versioned bundled CLI rather than editing user config.

Personal marketplace setup (run from this checkout, in this order):

```powershell
.\install.ps1 -MarketplacePath "$env:USERPROFILE\.agents\plugins\marketplace.json" -PluginInstallRoot "$env:USERPROFILE\plugins"
codex plugin add codex-usage-guard@personal
```

The installer writes the final absolute runtime/config paths but uses the official marketplace local-source shape `./plugins/codex-usage-guard`; it does not register the plugin by itself. Run `install.ps1`, then `codex plugin add ...`, restart Codex so the marketplace cache reloads, review/trust the newly discovered hooks, and start a new task. For same-version local iteration, use the official cachebuster helper at `$env:USERPROFILE\.codex\skills\.system\plugin-creator\scripts\update_plugin_cachebuster.py`: `python3 "$env:USERPROFILE/.codex/skills/.system/plugin-creator/scripts/update_plugin_cachebuster.py" <plugin-path>`; this is for local cache refresh only, while release source/tag remains `0.2.0`. Re-run `reinstall.ps1 -MarketplacePath <path> -PluginInstallRoot <directory>` after updates (the parameters are forwarded to `install.ps1`); use `codex plugin remove codex-usage-guard@personal` before `uninstall.ps1 -MarketplacePath <path> -PluginInstallRoot <directory>`. Uninstall removes only this plugin's validated copied target and marketplace object; add `-PurgeData` only when the dedicated cache/heartbeat directory should also be removed. A cachebuster/reinstall cycle is: reinstall, restart Codex, remove/re-add the plugin, and begin a new task. Set `CODEX_USAGE_GUARD_CODEX` to an approved absolute executable to override discovery. Hooks cannot cancel a running request, and account activity never changes quota decisions. Use `scripts/package-release.ps1 -Ref v0.2.0` only for the clean annotated release tag at HEAD; untagged push/PR checks use `scripts/package-release.ps1 -Development -Ref HEAD`. Both create a ZIP and SHA256 sidecar from an explicit allowlist, excluding caches, state, venv, tests, and credentials. A dirty checkout is rejected when git HEAD exists. See the [App Server rate-limit contract](https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt), [Plugins guide](https://learn.chatgpt.com/docs/plugins#use-plugins-from-a-supported-surface), and [Hooks guide](https://learn.chatgpt.com/docs/hooks#userpromptsubmit).

## Verification

```powershell
python -m pytest -q
python "$env:USERPROFILE\.codex\skills\.system\skill-creator\scripts\quick_validate.py" .\skills\usage-aware-workflow
# If the plugin validator is installed, run it too; CI performs the same best-effort check.
```

## Privacy

Authentication is delegated to the local Codex App Server. This plugin does not access or parse credential/config files, and does not persist credential material; it reuses the App Server's existing authentication. As with any child process, ordinary environment inheritance remains possible.
