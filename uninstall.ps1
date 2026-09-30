[CmdletBinding()]
param(
  [switch]$KeepVenv,
  [switch]$PurgeData,
  [string]$MarketplacePath = "",
  [string]$PluginInstallRoot = ""
)
$ErrorActionPreference = 'Stop'
$product = 'Codex Usage Guard v0.2.0'; $pluginName = 'codex-usage-guard'; $dataRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'OpenAI\codex-usage-guard')); $configPath = Join-Path $dataRoot 'plugin-config.json'
function Write-AtomicText([string]$Path, [string]$Text) {
  $parent = Split-Path -Parent $Path; New-Item -ItemType Directory -Path $parent -Force | Out-Null; $tmp = Join-Path $parent ('.' + [IO.Path]::GetFileName($Path) + '.' + [guid]::NewGuid().ToString('N') + '.tmp')
  try { $bytes=[Text.UTF8Encoding]::new($false).GetBytes($Text); $s=[IO.File]::Open($tmp,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None); try {$s.Write($bytes,0,$bytes.Length);$s.Flush($true)} finally {$s.Dispose()}; Move-Item -LiteralPath $tmp -Destination $Path -Force } finally { if(Test-Path -LiteralPath $tmp){Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue} }
}
function Assert-Under([string]$Child,[string]$Parent) { $c=[IO.Path]::GetFullPath($Child);$p=[IO.Path]::GetFullPath($Parent);if(-not $c.StartsWith($p.TrimEnd('\')+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Plugin target is outside PluginInstallRoot.'};return $c }
function Get-MarketplacePluginRoot([string]$Path) { $dir=[IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Path));$leaf=[IO.Path]::GetFileName($dir);$parent=[IO.Path]::GetDirectoryName($dir);if($leaf -ieq 'plugins' -and [IO.Path]::GetFileName($parent) -ieq '.agents'){return [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetDirectoryName($parent)) 'plugins'))};return [IO.Path]::GetFullPath((Join-Path $dir 'plugins')) }
$configured = $null; $oldConfigText = $null
if (Test-Path -LiteralPath $configPath -PathType Leaf) { $oldConfigText=[IO.File]::ReadAllText($configPath,[Text.UTF8Encoding]::new($false)); try { $configured = $oldConfigText | ConvertFrom-Json } catch { throw 'Invalid plugin-config.json; refusing to remove an unscoped target.' } }
if (-not $MarketplacePath -and $configured -and $configured.marketplacePath) { $MarketplacePath = [string]$configured.marketplacePath }
# A source-checkout install has no marketplacePath and must never recursively delete pluginRoot.
$market = if ($MarketplacePath) { [IO.Path]::GetFullPath($MarketplacePath) } else { $null }
$target = $null; $backup = $null; $oldMarketText = $null; $marketExisted = $false; $targetMoved = $false
if ($market) {
  $parentRaw = if ($PluginInstallRoot) { $PluginInstallRoot } elseif ($configured -and $configured.pluginInstallRoot) { [string]$configured.pluginInstallRoot } else { $null }
  $derivedRoot=Get-MarketplacePluginRoot $market
  if ($parentRaw -and [IO.Path]::IsPathRooted($parentRaw)) {
    $parent = [IO.Path]::GetFullPath($parentRaw); if($parent -ne $derivedRoot){throw "PluginInstallRoot must resolve to the marketplace ./plugins source root: $derivedRoot"}; $target = Assert-Under (Join-Path $parent $pluginName) $parent
    if ([IO.Path]::GetFileName($target) -ne $pluginName) { throw 'Refusing to remove a non-plugin target.' }
    if ($configured -and $configured.pluginRoot -and [IO.Path]::GetFullPath([string]$configured.pluginRoot) -ne $target) { throw 'Configured pluginRoot does not match the marketplace target.' }
    if (Test-Path -LiteralPath $target) {
      if (-not (Test-Path -LiteralPath (Join-Path $target '.codex-plugin\plugin.json') -PathType Leaf)) { throw 'Target manifest missing; refusing recursive removal.' }
      try { $manifest=[IO.File]::ReadAllText((Join-Path $target '.codex-plugin\plugin.json'),[Text.UTF8Encoding]::new($false))|ConvertFrom-Json } catch { throw 'Target manifest invalid; refusing recursive removal.' }
      if ([string]$manifest.name -ne $pluginName) { throw 'Target manifest name mismatch; refusing recursive removal.' }
    }
  } elseif ($configured -and $configured.pluginRoot) { throw 'Marketplace uninstall requires an absolute PluginInstallRoot.' }
  if (Test-Path -LiteralPath $market -PathType Leaf) { $marketExisted=$true;$oldMarketText=[IO.File]::ReadAllText($market,[Text.UTF8Encoding]::new($false)) }
}
try {
  if ($target -and (Test-Path -LiteralPath $target)) { $backup=Join-Path (Split-Path -Parent $target) ('.'+$pluginName+'.uninstall-'+[guid]::NewGuid().ToString('N'));Move-Item -LiteralPath $target -Destination $backup -Force;$targetMoved=$true }
  if ($market -and $marketExisted) {
    try { $obj=$oldMarketText|ConvertFrom-Json -ErrorAction Stop } catch { throw "Invalid marketplace JSON: $market" }
    $obj.plugins=@($obj.plugins|Where-Object {[string]$_.name -ne $pluginName})
    if ($env:CODEX_USAGE_GUARD_TEST_FAIL_UNINSTALL_MARKETPLACE -eq '1'){throw 'Injected uninstall marketplace failure.'}
    Write-AtomicText $market (($obj|ConvertTo-Json -Depth 20)+[Environment]::NewLine)
  }
  if ($env:CODEX_USAGE_GUARD_TEST_FAIL_UNINSTALL_CONFIG -eq '1'){throw 'Injected uninstall config failure.'}
  if ($oldConfigText -ne $null -and (Test-Path -LiteralPath $configPath)) { Remove-Item -LiteralPath $configPath -Force }
  if ($backup -and (Test-Path -LiteralPath $backup)) { Remove-Item -LiteralPath $backup -Recurse -Force }
} catch {
  if ($targetMoved -and $backup -and (Test-Path -LiteralPath $backup)) { if(Test-Path -LiteralPath $target){Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue};Move-Item -LiteralPath $backup -Destination $target -Force -ErrorAction SilentlyContinue }
  if ($market -and $marketExisted -and $oldMarketText -ne $null) { Write-AtomicText $market $oldMarketText }
  if ($oldConfigText -ne $null) { Write-AtomicText $configPath $oldConfigText }
  throw
}
if (-not $KeepVenv) { $venv=Join-Path $dataRoot 'runtime';if(Test-Path -LiteralPath $venv){Remove-Item -LiteralPath $venv -Recurse -Force} }
if ($PurgeData -and (Test-Path -LiteralPath $dataRoot)) { Remove-Item -LiteralPath $dataRoot -Recurse -Force }
Write-Output "$product runtime removed."
