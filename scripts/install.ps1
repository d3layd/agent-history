<#
.SYNOPSIS
  Installs agent-history on Windows and optionally schedules automatic indexing.

.DESCRIPTION
  The Windows counterpart to install.sh. Checks prerequisites, installs the CLI
  with uv, and offers to register a Scheduled Task as the equivalent of the
  cron safety net described in the README.

.PARAMETER Yes
  Accept defaults without prompting.

.EXAMPLE
  .\scripts\install.ps1
#>
[CmdletBinding()]
param([switch]$Yes)

$ErrorActionPreference = 'Stop'
$RepoDir = Split-Path -Parent $PSScriptRoot
$Model   = if ($env:AGENT_HISTORY_MODEL) { $env:AGENT_HISTORY_MODEL } else { 'nomic-embed-text' }
$Ollama  = if ($env:OLLAMA_URL) { $env:OLLAMA_URL } else { 'http://localhost:11434' }

function Write-Section($text) { Write-Host "`n$text" -ForegroundColor White }
function Write-Ok($text)      { Write-Host "  [ok] $text" -ForegroundColor Green }
function Write-Warn2($text)   { Write-Host "  [!] $text"  -ForegroundColor Yellow }

function Confirm-Step($prompt, $default) {
    if ($Yes) { return $default -eq 'y' }
    $hint = if ($default -eq 'y') { '[Y/n]' } else { '[y/N]' }
    $reply = Read-Host "  $prompt $hint"
    if ([string]::IsNullOrWhiteSpace($reply)) { $reply = $default }
    return $reply -match '^[Yy]'
}

Write-Section 'Checking prerequisites'

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Warn2 'uv is not installed. See https://docs.astral.sh/uv/ then re-run.'
    exit 1
}
Write-Ok "uv $((uv --version) -replace '^uv\s*','')"

try {
    $tags = Invoke-RestMethod -Uri "$Ollama/api/tags" -TimeoutSec 5
    Write-Ok "Ollama reachable at $Ollama"
} catch {
    Write-Warn2 "Ollama is not responding at $Ollama."
    Write-Warn2 "Start it, then re-run this script."
    exit 1
}

if ($tags.models.name -match [regex]::Escape($Model)) {
    Write-Ok "model '$Model' present"
} elseif ((Get-Command ollama -ErrorAction SilentlyContinue) -and
          (Confirm-Step "Pull embedding model '$Model' now?" 'y')) {
    ollama pull $Model
} else {
    Write-Warn2 "Model '$Model' is missing. Run: ollama pull $Model"
}

# A Python whose sqlite3 cannot load extensions cannot run this tool at all.
Write-Section 'Checking sqlite3 extension support'
$probe = uv run --no-project python -c @"
import sqlite3
try:
    sqlite3.connect(':memory:').enable_load_extension(True)
    print('YES')
except Exception as e:
    print('NO', e)
"@
if ($probe -notmatch '^YES') {
    Write-Warn2 "This Python cannot load SQLite extensions: $probe"
    Write-Warn2 'Install under a Python where it can — see the README.'
} else {
    Write-Ok 'extensions loadable'
}

Write-Section 'Installing agent-history'
uv tool install --force $RepoDir | Out-Null
if (Get-Command agent-history -ErrorAction SilentlyContinue) {
    Write-Ok "installed: $((Get-Command agent-history).Source)"
} else {
    Write-Warn2 'agent-history is not on PATH yet. Add %USERPROFILE%\.local\bin.'
}

Write-Section 'Automatic indexing'
Write-Host @"
  The Claude Code plugin installs SessionEnd and PreCompact hooks for you:

      /plugin marketplace add d3layd/agent-history
      /plugin install history@agent-history

  Those hooks call ``agent-history trigger``, which works the same on every
  platform. SessionEnd only fires on a clean exit, so a scheduled safety net
  catches sessions that were killed.
"@

if (Confirm-Step 'Register an hourly Scheduled Task as the safety net?' 'n') {
    $action  = New-ScheduledTaskAction -Execute 'agent-history' `
                                       -Argument 'trigger scheduled --if-changed'
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
                 -RepetitionInterval (New-TimeSpan -Hours 1)
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
                 -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
    Register-ScheduledTask -TaskName 'agent-history index' -Action $action `
        -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Ok "scheduled task 'agent-history index' registered (hourly)"
    Write-Host "  Remove with: Unregister-ScheduledTask -TaskName 'agent-history index'"
}

Write-Section 'Building the initial index'
if (Confirm-Step 'Index your history now? (first run can take a while)' 'y') {
    agent-history index
} else {
    Write-Host "  Run 'agent-history index' when you're ready."
}

Write-Section 'Done'
Write-Host '  Try:   agent-history search "something you discussed recently"'
Write-Host '  Check: agent-history doctor'
