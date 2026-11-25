# VRDub push helper: keep retrying until one attempt gets through.
# No Chinese text here on purpose (PS 5.1 reads BOM-less UTF-8 as ANSI).
param(
  [int]$Attempts = 60,
  [int]$GapSec = 12,
  [string]$Remote = "origin",
  [string]$Branch = "main",
  [switch]$Force
)
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $root
$logDir = Join-Path $root "out"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "push.log"
function Note([string]$m) {
  $line = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + "  " + $m
  Write-Host $line
  Add-Content -Path $log -Value $line -Encoding UTF8
}
$env:GIT_TERMINAL_PROMPT = "0"
$env:GIT_SSH_COMMAND = "ssh -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=5"
$pushArgs = @("push", $Remote, $Branch)
if ($Force) { $pushArgs += "--force" }
Note ("start: attempts=" + $Attempts + " gap=" + $GapSec + "s remote=" + $Remote + " branch=" + $Branch)
$ok = $false
for ($i = 1; $i -le $Attempts; $i++) {
  $out = (& git @pushArgs 2>&1 | Out-String)
  $code = $LASTEXITCODE
  if ($code -eq 0) {
    Note ("attempt " + $i + ": OK")
    if ($out.Trim()) { Note ("  " + ($out.Trim() -replace "\r?\n", " | ")) }
    $ok = $true
    break
  }
  $first = ($out.Trim() -split "\r?\n" | Where-Object { $_ -match "\S" } | Select-Object -Last 1)
  Note ("attempt " + $i + "/" + $Attempts + ": fail (" + $code + ") " + $first)
  if ($i -lt $Attempts) { Start-Sleep -Seconds $GapSec }
}
if ($ok) { Note "DONE" } else { Note "GIVE-UP after $Attempts attempts" }
if (-not $ok) { exit 1 }
