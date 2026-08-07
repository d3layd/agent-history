<#
.SYNOPSIS
  Brings this machine's installation in line with the repository.

.DESCRIPTION
  The Windows counterpart to sync-local.sh. The checkout, the uv-installed CLI
  and the Claude Code plugin drift independently, and an out-of-date CLI is easy
  to miss because it keeps working.

.PARAMETER Check
  Report drift and exit non-zero; change nothing.

.PARAMETER Force
  Reinstall even when the versions already agree.
#>
[CmdletBinding()]
param([switch]$Check, [switch]$Force)

$ErrorActionPreference = 'Stop'
$RepoDir = Split-Path -Parent $PSScriptRoot

function Write-Section($t) { Write-Host "`n$t" -ForegroundColor White }
function Write-Ok($t)      { Write-Host "  [ok] $t" -ForegroundColor Green }
function Write-Warn2($t)   { Write-Host "  [!] $t"  -ForegroundColor Yellow }

function Get-RepoVersion {
    (Select-String -Path (Join-Path $RepoDir 'pyproject.toml') `
        -Pattern '^version = "(.*)"').Matches[0].Groups[1].Value
}
function Get-CliVersion {
    if (-not (Get-Command agent-history -ErrorAction SilentlyContinue)) { return 'not-installed' }
    try { ((agent-history --version) -split '\s+')[-1] } catch { 'unknown' }
}
function Get-PluginVersion {
    if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { return 'no-claude' }
    try { ((claude plugin details history 2>$null | Select-Object -First 1) -split '\s+')[-1] }
    catch { 'not-installed' }
}

Write-Section 'Repository'
if (-not $Check) {
    try { git -C $RepoDir pull --ff-only --quiet; Write-Ok "pulled $(git -C $RepoDir rev-parse --short HEAD)" }
    catch { Write-Warn2 'could not fast-forward (local commits or no remote?)' }
} else {
    Write-Ok "at $(git -C $RepoDir rev-parse --short HEAD)"
}

$repoV   = Get-RepoVersion
$cliV    = Get-CliVersion
$pluginV = Get-PluginVersion

Write-Section 'Versions'
'{0,-10} {1}' -f 'repo',   $repoV   | ForEach-Object { Write-Host "  $_" }
'{0,-10} {1}' -f 'cli',    $cliV    | ForEach-Object { Write-Host "  $_" }
'{0,-10} {1}' -f 'plugin', $pluginV | ForEach-Object { Write-Host "  $_" }

$drift = ($cliV -ne $repoV) -or (($pluginV -ne $repoV) -and ($pluginV -ne 'no-claude'))

if ($Check) {
    if ($drift) { Write-Warn2 'drift detected - run .\scripts\sync-local.ps1 to fix'; exit 1 }
    Write-Ok 'everything in sync'; exit 0
}

if (-not $drift -and -not $Force) {
    Write-Ok 'already in sync (use -Force to reinstall anyway)'
} else {
    Write-Section 'Reinstalling'
    try { uv tool install --force $RepoDir | Out-Null; Write-Ok "cli -> $(Get-CliVersion)" }
    catch { Write-Warn2 'cli install failed' }

    if (Get-Command claude -ErrorAction SilentlyContinue) {
        try {
            claude plugin marketplace update agent-history 2>$null | Out-Null
            claude plugin install history@agent-history 2>$null | Out-Null
            Write-Ok "plugin -> $(Get-PluginVersion)"
        } catch { Write-Warn2 'plugin install failed (is the marketplace added?)' }
    }
}

Write-Section 'Health'
if (Get-Command agent-history -ErrorAction SilentlyContinue) {
    agent-history doctor | ForEach-Object { Write-Host "  $_" }
} else {
    Write-Warn2 'agent-history is not on PATH'
}
