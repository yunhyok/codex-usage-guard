param([string]$EventName = "UserPromptSubmit")
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
if ($EventName -eq 'SessionStart') {
    $skillPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../skills/usage-aware-workflow/SKILL.md'))
    if (-not (Test-Path -LiteralPath $skillPath -PathType Leaf)) { throw "Installed usage-aware-workflow skill is missing: $skillPath" }
    $context = "Codex Usage Guard v0.2.1 is enabled. Before costly work, read the installed skill at $skillPath and apply its advisory usage checkpoints. Preserve your assigned role and the user's current instructions. Discover this plugin's evaluate_usage_guard tool before calling it; do not guess its namespace. If the MCP runtime is unavailable, report that limitation without claiming usage was checked. This hook does not grant new permissions. Honor any user request to stop using the skill."
    @{ hookSpecificOutput = @{ hookEventName = $EventName; additionalContext = $context } } | ConvertTo-Json -Compress
    exit 0
}
$ErrorActionPreference = "SilentlyContinue"
$raw = [Console]::In.ReadToEnd(); $inputObject = $null
try { $inputObject = $raw | ConvertFrom-Json } catch {}
$dataRoot = if ($env:CODEX_USAGE_GUARD_DATA_DIR) { $env:CODEX_USAGE_GUARD_DATA_DIR } else { Join-Path $env:LOCALAPPDATA "OpenAI\codex-usage-guard" }
$statePath = Join-Path $dataRoot "state.json"; $context = "Codex Usage Guard v0.2.1 advisory: usage state is unknown or stale. Call evaluate_usage_guard before costly work; this hook never blocks or cancels a tool call."
$now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null; $heartbeatTmp = Join-Path $dataRoot ("heartbeat-" + [guid]::NewGuid().ToString() + ".tmp"); Set-Content -LiteralPath $heartbeatTmp -Value $now -Encoding ascii; Move-Item -LiteralPath $heartbeatTmp -Destination (Join-Path $dataRoot "heartbeat.txt") -Force
if ($EventName -eq "PostToolUse") {
    $toolName = if ($inputObject) { [string]$inputObject.tool_name } else { "" }
    if ($toolName -match '^mcp__(?:[A-Za-z0-9_-]+[_-])?codex_usage_guard__(?:get_usage_status|evaluate_usage_guard|doctor)$') { $context = "" }
    else {
        $stamp = Join-Path $dataRoot "post-hook.txt"
        $lockPath = $stamp + ".lock"; $handle = $null; $ownsLock = $false
        try {
            for ($attempt = 0; $attempt -lt 2 -and -not $ownsLock; $attempt++) {
                try { $handle = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None); $ownsLock = $true }
                catch {
                    $stale = $false
                    if ($attempt -eq 0 -and (Test-Path -LiteralPath $lockPath)) { try { $stale = (([DateTime]::UtcNow - (Get-Item -LiteralPath $lockPath).LastWriteTimeUtc).TotalSeconds -ge 30) } catch {} }
                    if ($stale) { Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue } else { break }
                }
            }
            if (-not $ownsLock) { throw 'post hook cooldown lock is active' }
            $last = 0; if (Test-Path -LiteralPath $stamp) { try { $last = [int64](Get-Content -LiteralPath $stamp -Raw) } catch {} }
            if (($now - $last) -ge 600) { $tmp = Join-Path $dataRoot ("hook-" + [guid]::NewGuid().ToString() + ".tmp"); Set-Content -LiteralPath $tmp -Value $now -Encoding ascii; Move-Item -LiteralPath $tmp -Destination $stamp -Force }
            else { $context = "" }
        }
            catch { $context = "" }
            finally { if ($handle) { $handle.Dispose() }; if ($ownsLock) { Remove-Item -LiteralPath $lockPath -Force } }
    }
}
if ($context -and (Test-Path -LiteralPath $statePath)) {
    try {
        $state = [IO.File]::ReadAllText($statePath,[Text.UTF8Encoding]::new($false)) | ConvertFrom-Json
        $remaining = $state.minRemainingPercent; $fetched = $state.fetchedAt; $decision = [string]$state.decision
        $validNumber = $null -ne $remaining -and -not ($remaining -is [bool]) -and ($remaining -is [ValueType]) -and [double]::TryParse([string]$remaining,[Globalization.NumberStyles]::Float,[Globalization.CultureInfo]::InvariantCulture,[ref]$remaining)
        $validFetched = $null -ne $fetched -and -not ($fetched -is [bool]) -and ($fetched -is [ValueType]) -and [double]::TryParse([string]$fetched,[Globalization.NumberStyles]::Float,[Globalization.CultureInfo]::InvariantCulture,[ref]$fetched)
        if ($state.schemaVersion -eq 1 -and $validNumber -and $validFetched -and -not [double]::IsNaN([double]$remaining) -and -not [double]::IsInfinity([double]$remaining) -and [double]$remaining -ge 0 -and [double]$remaining -le 100 -and -not [double]::IsNaN([double]$fetched) -and -not [double]::IsInfinity([double]$fetched) -and @('proceed','watch','checkpoint','critical','unknown') -contains $decision) {
            $age = [int]($now - [double]$fetched); if ($age -ge 0 -and $age -lt 120) { $context = "Codex Usage Guard v0.2.1 advisory: latest $([double]$remaining)% remaining, age ${age}s, decision $decision. Call evaluate_usage_guard before costly work; this hook never blocks or cancels a tool call." }
        }
    } catch {}
}
$output = @{ hookSpecificOutput = @{ hookEventName = $EventName; additionalContext = $context } }
$output | ConvertTo-Json -Compress
