[CmdletBinding()]
param(
  [switch]$Force,
  [string]$MarketplacePath = "",
  [switch]$SkipRuntime,
  [string]$PluginInstallRoot = ""
)

$ErrorActionPreference = "Stop"
$script:Product = "Codex Usage Guard v0.1.1"
$script:Version = "0.1.1"
$script:PluginName = "codex-usage-guard"
$script:Root = [IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$script:DataRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "OpenAI\codex-usage-guard"))

function Assert-UnderPath([string]$Child, [string]$Parent, [string]$Label) {
  $c = [IO.Path]::GetFullPath($Child); $p = [IO.Path]::GetFullPath($Parent)
  if (-not $c.StartsWith($p.TrimEnd('\') + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw "$Label must remain under $p." }
  return $c
}
function Write-AtomicText([string]$Path, [string]$Text) {
  $parent = Split-Path -Parent $Path; New-Item -ItemType Directory -Path $parent -Force | Out-Null
  $tmp = Join-Path $parent ("." + [IO.Path]::GetFileName($Path) + "." + [guid]::NewGuid().ToString("N") + ".tmp")
  try {
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($Text)
    $stream = [IO.File]::Open($tmp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
    Move-Item -LiteralPath $tmp -Destination $Path -Force
  } finally { if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue } }
}
function Get-ReleaseAllowlist() {
  return @('.codex-plugin','.mcp.json','app_server.py','models.py','policy.py','usage_guard_mcp.py','requirements.txt','pyproject.toml','README.md','SECURITY.md','NOTICE.md','LICENSE','.gitignore','install.ps1','reinstall.ps1','run-mcp.ps1','uninstall.ps1','hooks','scripts','skills','.github')
}
function Copy-ReleaseTree([string]$Source, [string]$Destination) {
  New-Item -ItemType Directory -Path $Destination -Force | Out-Null
  foreach ($name in (Get-ReleaseAllowlist)) { $from = Join-Path $Source $name; if (Test-Path -LiteralPath $from) { Copy-Item -LiteralPath $from -Destination $Destination -Recurse -Force } }
  $manifestPath = Join-Path $Destination '.codex-plugin\plugin.json'
  if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'Release manifest is missing.' }
  $manifest = [IO.File]::ReadAllText($manifestPath, [Text.UTF8Encoding]::new($false)) | ConvertFrom-Json
  if ([string]$manifest.name -ne $script:PluginName -or [string]$manifest.version -ne $script:Version) { throw 'Release manifest name/version is invalid.' }
}
function Read-Marketplace([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return [pscustomobject]@{ name='personal'; interface=[pscustomobject]@{ displayName='Personal' }; plugins=@() } }
  try { return ([IO.File]::ReadAllText($Path, [Text.UTF8Encoding]::new($false)) | ConvertFrom-Json -ErrorAction Stop) } catch { throw "Invalid marketplace JSON: $Path" }
}
function Write-Marketplace([string]$Path, $Value) { Write-AtomicText $Path (($Value | ConvertTo-Json -Depth 20) + [Environment]::NewLine) }
function Get-MarketplacePluginRoot([string]$Path) {
  $dir=[IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Path)); $leaf=[IO.Path]::GetFileName($dir); $parent=[IO.Path]::GetDirectoryName($dir)
  if ($leaf -ieq 'plugins' -and [IO.Path]::GetFileName($parent) -ieq '.agents') { return [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetDirectoryName($parent)) 'plugins')) }
  return [IO.Path]::GetFullPath((Join-Path $dir 'plugins'))
}

$venv = Join-Path $script:DataRoot 'runtime'; $runtimeStage=$null; $runtimeBackup=$null; $runtimeCommitted=$false; $runtimeExisted=Test-Path -LiteralPath $venv
New-Item -ItemType Directory -Path $script:DataRoot -Force | Out-Null
try {
  if (-not $SkipRuntime) {
    $currentPython=Join-Path $venv 'Scripts\python.exe'; $needsStage=$Force -or -not (Test-Path -LiteralPath $currentPython -PathType Leaf)
    if ($needsStage) {
      $runtimeStage=Join-Path $script:DataRoot ('.runtime-stage-'+[guid]::NewGuid().ToString('N'))
      if ($env:CODEX_USAGE_GUARD_TEST_FAIL_RUNTIME -eq 'before-venv') { throw 'Injected runtime failure before venv creation.' }
      & python -m venv $runtimeStage; if ($LASTEXITCODE -ne 0) { throw "venv creation failed ($LASTEXITCODE)" }
      $stagePython=Join-Path $runtimeStage 'Scripts\python.exe'; if ($env:CODEX_USAGE_GUARD_TEST_SKIP_PIP -ne '1') { & $stagePython -m pip install --upgrade pip; if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed ($LASTEXITCODE)" }; & $stagePython -m pip install -r (Join-Path $script:Root 'requirements.txt'); if ($LASTEXITCODE -ne 0) { throw "dependency install failed ($LASTEXITCODE)" } }
      if ($env:CODEX_USAGE_GUARD_TEST_FAIL_RUNTIME -eq 'before-commit') { throw 'Injected runtime failure before commit.' }
      if ($runtimeExisted) { $runtimeBackup=Join-Path $script:DataRoot ('.runtime-backup-'+[guid]::NewGuid().ToString('N')); Move-Item -LiteralPath $venv -Destination $runtimeBackup -Force }
      Move-Item -LiteralPath $runtimeStage -Destination $venv -Force; $runtimeStage=$null; $runtimeCommitted=$true
    }
    $python=Join-Path $venv 'Scripts\python.exe'
  } else { $python=Join-Path $venv 'Scripts\python.exe' }
  if (-not (Test-Path -LiteralPath $python -PathType Leaf) -and $SkipRuntime) { Write-Warning "Private runtime is missing $python; run install without -SkipRuntime before launching MCP." }
  if (-not (Test-Path -LiteralPath $python -PathType Leaf) -and -not $SkipRuntime) { throw "Private runtime is missing $python after venv creation." }
} catch {
  if ($runtimeStage -and (Test-Path -LiteralPath $runtimeStage)) { Remove-Item -LiteralPath $runtimeStage -Recurse -Force -ErrorAction SilentlyContinue }
  if ($runtimeBackup -and (Test-Path -LiteralPath $runtimeBackup)) { if(Test-Path -LiteralPath $venv){Remove-Item -LiteralPath $venv -Recurse -Force -ErrorAction SilentlyContinue};Move-Item -LiteralPath $runtimeBackup -Destination $venv -Force -ErrorAction SilentlyContinue }
  throw
}

$market = $null; $pluginRoot = $null; $target = $script:Root; $stage = $null; $backup = $null; $marketExisted = $false; $targetExisted = $false; $targetInstalled = $false
$oldConfig = Join-Path $script:DataRoot 'plugin-config.json'; $oldConfigText = if (Test-Path -LiteralPath $oldConfig -PathType Leaf) { [IO.File]::ReadAllText($oldConfig) } else { $null }; $oldMarketText = $null
try {
  if ($MarketplacePath) {
    $market = [IO.Path]::GetFullPath($MarketplacePath)
    $derivedPluginRoot = Get-MarketplacePluginRoot $market
    $pluginRoot = if ($PluginInstallRoot) { [IO.Path]::GetFullPath($PluginInstallRoot) } else { $derivedPluginRoot }
    if ($pluginRoot -ne $derivedPluginRoot) { throw "PluginInstallRoot must resolve to the marketplace ./plugins source root: $derivedPluginRoot" }
    New-Item -ItemType Directory -Path $pluginRoot -Force | Out-Null; $target = Assert-UnderPath (Join-Path $pluginRoot $script:PluginName) $pluginRoot 'Plugin target'
    if (Test-Path -LiteralPath $market -PathType Leaf) { $marketExisted = $true; $oldMarketText = [IO.File]::ReadAllText($market) }
    $stage = Join-Path $pluginRoot ('.' + $script:PluginName + '.stage-' + [guid]::NewGuid().ToString('N')); Copy-ReleaseTree $script:Root $stage
    if (Test-Path -LiteralPath $target) { $targetExisted = $true; $backup = Join-Path $pluginRoot ('.' + $script:PluginName + '.backup-' + [guid]::NewGuid().ToString('N')); Move-Item -LiteralPath $target -Destination $backup -Force }
    Move-Item -LiteralPath $stage -Destination $target -Force; $stage = $null; $targetInstalled = $true
    if ($env:CODEX_USAGE_GUARD_TEST_FAIL_STAGE -eq 'after-target') { throw 'Injected install failure after target replacement.' }
  }
  $server = [IO.Path]::GetFullPath((Join-Path $target 'usage_guard_mcp.py'))
  $configObject = [ordered]@{ schemaVersion=1; product=$script:Product; version=$script:Version; python=[IO.Path]::GetFullPath($python); server=$server; pluginRoot=[IO.Path]::GetFullPath($target); pluginInstallRoot=$pluginRoot; dataDir=[IO.Path]::GetFullPath($script:DataRoot); marketplacePath=$market }
  Write-AtomicText $oldConfig (($configObject | ConvertTo-Json -Depth 10) + [Environment]::NewLine)
  if ($env:CODEX_USAGE_GUARD_TEST_FAIL_CONFIG -eq '1') { throw 'Injected install failure after config write.' }
  if ($market) {
    $existing = Read-Marketplace $market; $plugins = @($existing.plugins | Where-Object { [string]$_.name -ne $script:PluginName }); $plugins += [pscustomobject]@{ name=$script:PluginName; source=[pscustomobject]@{ source='local'; path='./plugins/codex-usage-guard' }; policy=[pscustomobject]@{ installation='AVAILABLE'; authentication='ON_INSTALL' }; category='Productivity' }; $existing.plugins = $plugins
    if ($env:CODEX_USAGE_GUARD_TEST_FAIL_MARKETPLACE -eq '1') { throw 'Injected install failure before marketplace write.' }; Write-Marketplace $market $existing
  }
  if ($backup -and (Test-Path -LiteralPath $backup)) { Remove-Item -LiteralPath $backup -Recurse -Force }
} catch {
  if ($stage -and (Test-Path -LiteralPath $stage)) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
  if ($backup -and (Test-Path -LiteralPath $backup)) { if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue }; Move-Item -LiteralPath $backup -Destination $target -Force -ErrorAction SilentlyContinue }
  elseif ($targetInstalled -and -not $targetExisted -and (Test-Path -LiteralPath $target)) { Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue }
  if ($oldConfigText -ne $null) { Write-AtomicText $oldConfig $oldConfigText } elseif (Test-Path -LiteralPath $oldConfig) { Remove-Item -LiteralPath $oldConfig -Force -ErrorAction SilentlyContinue }
  if ($market -and $oldMarketText -ne $null) { Write-AtomicText $market $oldMarketText } elseif ($market -and -not $marketExisted -and (Test-Path -LiteralPath $market)) { Remove-Item -LiteralPath $market -Force -ErrorAction SilentlyContinue }
  if ($runtimeCommitted) { if(Test-Path -LiteralPath $venv){Remove-Item -LiteralPath $venv -Recurse -Force -ErrorAction SilentlyContinue}; if($runtimeBackup -and (Test-Path -LiteralPath $runtimeBackup)){Move-Item -LiteralPath $runtimeBackup -Destination $venv -Force -ErrorAction SilentlyContinue} }
  throw
}
if ($runtimeBackup -and (Test-Path -LiteralPath $runtimeBackup)) { Remove-Item -LiteralPath $runtimeBackup -Recurse -Force }
Write-Output "$script:Product runtime ready: $script:DataRoot"
