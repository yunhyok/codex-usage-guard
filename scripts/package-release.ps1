[CmdletBinding()]
param(
  [string]$Output = 'dist\codex-usage-guard-v0.2.0.zip',
  [string]$Ref = '',
  [switch]$Development
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')); $manifestPath = Join-Path $root '.codex-plugin\plugin.json'; $script:Version = '0.2.0'; $script:Name = 'codex-usage-guard'
$allow = @('.codex-plugin','.mcp.json','app_server.py','models.py','policy.py','usage_guard_mcp.py','requirements.txt','pyproject.toml','README.md','SECURITY.md','NOTICE.md','LICENSE','.gitignore','install.ps1','reinstall.ps1','run-mcp.ps1','uninstall.ps1','hooks','scripts','skills','.github')
$out = if ([IO.Path]::IsPathRooted($Output)) { [IO.Path]::GetFullPath($Output) } else { [IO.Path]::GetFullPath((Join-Path $root $Output)) }; $dist = [IO.Path]::GetFullPath((Join-Path $root 'dist'))
if (-not $out.StartsWith($dist.TrimEnd('\') + [IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { throw 'Output must be inside repository dist.' }
New-Item -ItemType Directory -Path (Split-Path -Parent $out) -Force | Out-Null
function Assert-Manifest([string]$Path) { if(-not(Test-Path -LiteralPath $Path -PathType Leaf)){throw 'Release manifest is missing.'}; try{$m=Get-Content -LiteralPath $Path -Raw|ConvertFrom-Json}catch{throw 'Release manifest is invalid.'}; if([string]$m.name -ne $script:Name -or [string]$m.version -ne $script:Version){throw 'Release manifest name/version mismatch.'}; return $m }
Assert-Manifest $manifestPath | Out-Null
function Invoke-Git([string[]]$Arguments) { $result = & git -C $root @Arguments 2>&1; if($LASTEXITCODE -ne 0){throw "git $($Arguments -join ' ') failed ($LASTEXITCODE): $result"}; return @($result) }
function Assert-NoSecretContent([string]$Path) { $text=[Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($Path)); if($text -match '(?i)-----BEGIN [A-Z0-9 ]+ PRIVATE KEY-----|(?:sk-(?:proj-)?[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9_-]{16,}|github_pat_[A-Za-z0-9_]{16,}|xoxb-[A-Za-z0-9-]{16,})|Bearer\s+[A-Za-z0-9._-]{30,}'){throw "Potential secret content found in $Path."} }
$hasHead = $false; $head = $null
try { $head = (Invoke-Git @('rev-parse','--verify','HEAD'))[0]; $hasHead = [bool]$head } catch { $hasHead = $false }
$stage = Join-Path ([IO.Path]::GetTempPath()) ('codex-usage-guard-package-' + [guid]::NewGuid().ToString('N')); New-Item -ItemType Directory -Path $stage -Force | Out-Null
$zipTemp = Join-Path ([IO.Path]::GetTempPath()) ('codex-usage-guard-package-' + [guid]::NewGuid().ToString('N') + '.zip')
try {
  if ($hasHead) {
    $dirty = Invoke-Git @('status','--porcelain'); if ($dirty.Count -gt 0 -and -not [string]::IsNullOrWhiteSpace(($dirty -join ''))) { throw 'Packaging requires a clean git tree when HEAD exists.' }
    if (-not $Ref) { if($Development){$Ref='HEAD'} else { throw 'Release mode requires -Ref v0.2.0.' } }
    $refCommit = ([string]((Invoke-Git @('rev-parse',($Ref + '^{commit}')))[0])).Trim()
    if (-not $Development) {
      if ($Ref -ne ('v' + $script:Version)) { throw "Release mode requires exact annotated tag v$($script:Version)." }
      $tagType = ((& git -C $root cat-file -t ('refs/tags/'+$Ref) 2>&1 | Out-String).Trim()); if ($tagType -ne 'tag') { throw 'Release tag must be annotated.' }
      if ($refCommit -ne ([string]$head).Trim()) { throw "Ref $Ref must resolve to HEAD for this release." }
    }
    $refManifestText = (& git -C $root show "$Ref`:.codex-plugin/plugin.json" 2>&1); if ($LASTEXITCODE -ne 0) { throw "Ref $Ref has no release manifest." }; $refManifest = ($refManifestText -join [Environment]::NewLine) | ConvertFrom-Json; if ([string]$refManifest.version -ne $script:Version) { throw "Ref $Ref manifest version mismatch." }
    $tracked = Invoke-Git @('ls-tree','-r','--name-only',$Ref)
    $sensitive = @($tracked | Where-Object { $_ -match '(^|/)(\.env($|\.)|credentials?|auth|token|secrets?)(/|\.|$)' -or $_ -match '(^|/)(\.codex|__pycache__|\.pytest_cache)(/|$)' })
    if ($sensitive.Count -gt 0) { throw "Tracked sensitive files cannot be packaged: $($sensitive -join ', ')" }
    $pathspec = @('--') + $allow
    & git -C $root archive --format=zip --output=$zipTemp $Ref @pathspec 2>&1 | Out-Null; if($LASTEXITCODE -ne 0){throw "git archive failed ($LASTEXITCODE)"}
  } else {
    if (-not $Development) { throw 'A non-git checkout can only be packaged with -Development.' }
    foreach($name in $allow){$source=Join-Path $root $name;if(Test-Path -LiteralPath $source){Copy-Item -LiteralPath $source -Destination $stage -Recurse -Force}}
    $stageFiles=@(Get-ChildItem -LiteralPath $stage -Recurse -File); $stageNames=@($stageFiles | ForEach-Object { $_.FullName.Substring($stage.Length+1).Replace('\','/') }); $stageBad=@($stageNames|Where-Object {$_ -match '(^|/)(\.env($|\.)|credentials?|auth|token|secrets?|__pycache__|\.pytest_cache)(/|\.|$)' -or $_ -match '(^|/)tests?(/|$)'}); if($stageBad.Count -gt 0){throw "Fallback package contains forbidden paths: $($stageBad -join ', ')"}; foreach($file in $stageFiles){Assert-NoSecretContent $file.FullName}
    Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zipTemp -Force
  }
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $archive = [IO.Compression.ZipFile]::OpenRead($zipTemp)
  try {
    $names = @($archive.Entries | ForEach-Object { $_.FullName.TrimEnd('/') })
    foreach($required in @('.codex-plugin/plugin.json','.mcp.json','usage_guard_mcp.py','README.md','LICENSE')){if($names -notcontains $required){throw "Package is missing $required"}}
    $entry = $archive.GetEntry('.codex-plugin/plugin.json'); $reader=[IO.StreamReader]::new($entry.Open()); try{$packedManifest=$reader.ReadToEnd()|ConvertFrom-Json}finally{$reader.Dispose()}; if([string]$packedManifest.version -ne $script:Version){throw 'Packaged manifest version mismatch.'}
    $bad = @($names | Where-Object { $_ -match '(^|/)(\.env($|\.)|credentials?|auth|token|secrets?|__pycache__|\.pytest_cache)(/|\.|$)' -or $_ -match '(^|/)tests?(/|$)' }); if($bad.Count -gt 0){throw "Package contains forbidden paths: $($bad -join ', ')"}; foreach($entry in $archive.Entries | Where-Object { -not $_.FullName.EndsWith('/') }){ $tmpEntry=Join-Path ([IO.Path]::GetTempPath()) ('codex-entry-'+[guid]::NewGuid().ToString('N')); $inStream=$null;$outStream=$null; try{ $inStream=$entry.Open();$outStream=[IO.File]::Create($tmpEntry);$inStream.CopyTo($outStream);$outStream.Flush() } finally { if($inStream){$inStream.Dispose()};if($outStream){$outStream.Dispose()} }; try { Assert-NoSecretContent $tmpEntry } finally { if(Test-Path -LiteralPath $tmpEntry){Remove-Item -LiteralPath $tmpEntry -Force -ErrorAction SilentlyContinue} } }
  } finally { $archive.Dispose() }
  Move-Item -LiteralPath $zipTemp -Destination $out -Force
} finally {
  if(Test-Path -LiteralPath $zipTemp){Remove-Item -LiteralPath $zipTemp -Force -ErrorAction SilentlyContinue}
  $resolvedStage=[IO.Path]::GetFullPath($stage); $tempRoot=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()); if($resolvedStage.StartsWith($tempRoot,[StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $resolvedStage)){Remove-Item -LiteralPath $resolvedStage -Recurse -Force -ErrorAction SilentlyContinue}
}
$sha=[Security.Cryptography.SHA256]::Create(); try{$hash=([BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($out))) -replace '-','')}finally{$sha.Dispose()}; Set-Content -LiteralPath ($out+'.sha256') -Value "$hash  $(Split-Path $out -Leaf)" -Encoding ascii; Write-Output "$out`n$hash"
