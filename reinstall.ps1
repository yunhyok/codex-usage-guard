[CmdletBinding()]
param(
  [switch]$Force,
  [string]$MarketplacePath = "",
  [string]$PluginInstallRoot = "",
  [switch]$SkipRuntime
)
$ErrorActionPreference = 'Stop'
$installParams = @{}
if ($Force) { $installParams.Force = $true }
if ($MarketplacePath) { $installParams.MarketplacePath = $MarketplacePath }
if ($PluginInstallRoot) { $installParams.PluginInstallRoot = $PluginInstallRoot }
if ($SkipRuntime) { $installParams.SkipRuntime = $true }
$install = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'install.ps1'
& $install @installParams
exit $LASTEXITCODE
