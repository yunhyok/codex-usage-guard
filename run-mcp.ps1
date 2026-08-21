$ErrorActionPreference = "Stop"
$dataRoot = if ($env:CODEX_USAGE_GUARD_DATA_DIR) { $env:CODEX_USAGE_GUARD_DATA_DIR } else { Join-Path $env:LOCALAPPDATA "OpenAI\codex-usage-guard" }
$configPath = Join-Path $dataRoot "plugin-config.json"
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) { throw "Codex Usage Guard runtime is not installed. Run .\install.ps1 first." }
try { $configured = Get-Content -LiteralPath $configPath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop } catch { throw "Invalid Codex Usage Guard plugin-config.json; run .\install.ps1 again." }
if ([int]$configured.schemaVersion -ne 1) { throw "Unsupported plugin-config schema; run .\install.ps1 again." }
foreach ($name in @('python','server','pluginRoot','dataDir')) { if (-not $configured.$name) { throw "Invalid plugin-config.json ($name missing); run .\install.ps1 again." } }
$pythonRaw=[string]$configured.python; $serverRaw=[string]$configured.server; $pluginRootRaw=[string]$configured.pluginRoot; $dataRaw=[string]$configured.dataDir
if (-not [IO.Path]::IsPathRooted($pythonRaw) -or -not [IO.Path]::IsPathRooted($serverRaw) -or -not [IO.Path]::IsPathRooted($pluginRootRaw) -or -not [IO.Path]::IsPathRooted($dataRaw)) { throw "plugin-config paths must be absolute; run .\install.ps1 again." }
$python = [IO.Path]::GetFullPath($pythonRaw); $server = [IO.Path]::GetFullPath($serverRaw); $pluginRoot = [IO.Path]::GetFullPath($pluginRootRaw); $configuredData = [IO.Path]::GetFullPath($dataRaw)
if ($configuredData -ne [IO.Path]::GetFullPath($dataRoot)) { throw "Configured dataDir must match the runtime config directory; run .\install.ps1 again." }
if (-not $server.StartsWith($pluginRoot.TrimEnd('\') + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw "Configured MCP server is outside pluginRoot; run .\install.ps1 again." }
if ($server -ne [IO.Path]::GetFullPath((Join-Path $pluginRoot 'usage_guard_mcp.py'))) { throw "Configured MCP server path is not the plugin server; run .\install.ps1 again." }
if ($python -ne [IO.Path]::GetFullPath((Join-Path $configuredData 'runtime\Scripts\python.exe'))) { throw "Configured Python path is not the private runtime; run .\install.ps1 again." }
if (-not (Test-Path -LiteralPath $configuredData -PathType Container) -or -not (Test-Path -LiteralPath $pluginRoot -PathType Container)) { throw "Configured plugin/data directories are missing; run .\install.ps1 again." }
if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or -not (Test-Path -LiteralPath $server -PathType Leaf)) { throw "Codex Usage Guard runtime is incomplete. Run .\install.ps1 again." }
$env:CODEX_USAGE_GUARD_DATA_DIR = $configuredData
& $python $server
exit $LASTEXITCODE
