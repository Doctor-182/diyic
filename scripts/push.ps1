# VRDub push helper -- HTTPS through the local proxy, with retry.
# ASCII only on purpose: PowerShell 5.1 decodes BOM-less UTF-8 as ANSI, so any
# Chinese character in here would turn the tail of the file into garbage.
#
# Why HTTPS + proxy: on this machine plain git-over-HTTPS to github.com gets its
# TLS stream reset (recv failure) while a local sing-box proxy on 127.0.0.1:2080
# pushes it through fine (measured 3/3, ~2 s). SSH auth is a separate dead end:
# the registered key answers "Server accepts key" and then "Permission denied",
# which means it is not an authentication key on the account.
param(
  [int]$Attempts = 40,
  [int]$GapSec = 8,
  [string]$Remote = "origin",
  [string]$Branch = "main",
  [string]$Repo = "https://github.com/Doctor-182/diyic.git",
  [string]$Login = "Doctor-182",
  [switch]$Force
)
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $root

$outDir = Join-Path $root "out"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$log = Join-Path $outDir "push.log"

function Note([string]$m) {
  $line = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + "  " + $m
  Write-Host $line
  Add-Content -Path $log -Value $line -Encoding UTF8
}

# ---- 1. find a working proxy -------------------------------------------------
function Test-Proxy([string]$addr) {
  $env:GIT_TERMINAL_PROMPT = "0"
  $o = (& git -c "http.proxy=$addr" ls-remote --heads $Repo 2>&1 | Out-String)
  return ($LASTEXITCODE -eq 0)
}

$candidates = @()
if ($env:VRDUB_PROXY) { $candidates += $env:VRDUB_PROXY }
foreach ($p in @(2080, 7890, 7897, 10809, 1080, 10808, 8080)) {
  $candidates += ("http://127.0.0.1:" + $p)
}

$proxy = $null
foreach ($addr in $candidates) {
  Write-Host ("  probe " + $addr + " ...") -NoNewline
  if (Test-Proxy $addr) { Write-Host " ok"; $proxy = $addr; break }
  Write-Host " no"
}
if (-not $proxy) {
  Note "no proxy worked; trying direct"
} else {
  Note ("proxy = " + $proxy)
}
$proxyArgs = @()
if ($proxy) { $proxyArgs = @("-c", "http.proxy=$proxy", "-c", "https.proxy=$proxy") }

# ---- 2. remote over HTTPS ----------------------------------------------------
$old = (& git remote get-url $Remote 2>&1 | Out-String).Trim()
if ($old -ne $Repo) {
  & git remote set-url $Remote $Repo 2>&1 | Out-Null
  Note ("remote " + $Remote + ": " + $old + " -> " + $Repo)
}

# ---- 3. credentials ----------------------------------------------------------
$credFile = Join-Path $env:USERPROFILE ".git-credentials"
$hasCred = $false
if (Test-Path $credFile) {
  $hasCred = [bool](Select-String -Path $credFile -Pattern "github.com" -Quiet)
}
if (-not $hasCred) {
  Write-Host ""
  Write-Host "  Need a GitHub token once (it is stored in $credFile, nothing goes into the repo)."
  Write-Host "  Create one here:  https://github.com/settings/tokens/new?scopes=repo&description=VRDub"
  Write-Host "  Scope: repo (classic token). Paste it below."
  Write-Host ""
  $tok = Read-Host -AsSecureString ("Token for " + $Login)
  $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($tok))
  if (-not $plain) { Note "no token entered"; exit 2 }
  & git config --local credential.helper store
  $line = "https://" + $Login + ":" + $plain + "@github.com"
  Add-Content -Path $credFile -Value $line -Encoding ASCII
  Note "token stored (git credential helper = store, local config)"
  $plain = $null
}

# ---- 4. push with retry ------------------------------------------------------
$pushArgs = @()
# Skip the GUI helper (Git Credential Manager) entirely: it pops a window and
# blocks a non-interactive push. store reads ~/.git-credentials instead.
$pushArgs += @("-c", "credential.helper=", "-c", "credential.helper=store")
$pushArgs += $proxyArgs
$pushArgs += @("push", $Remote, $Branch)
if ($Force) { $pushArgs += "--force" }
$env:GIT_TERMINAL_PROMPT = "0"
Note ("push start: attempts=" + $Attempts + " gap=" + $GapSec + "s")
$ok = $false
for ($i = 1; $i -le $Attempts; $i++) {
  $o = (& git @pushArgs 2>&1 | Out-String)
  $code = $LASTEXITCODE
  if ($code -eq 0) {
    Note ("attempt " + $i + ": OK")
    if ($o.Trim()) { Note ("  " + ($o.Trim() -replace "\r?\n", " | ")) }
    $ok = $true
    break
  }
  $last = ($o.Trim() -split "\r?\n" | Where-Object { $_ -match "\S" } | Select-Object -Last 1)
  Note ("attempt " + $i + "/" + $Attempts + ": fail (" + $code + ") " + $last)
  if ($o -match "Authentication failed|could not read Username|403") {
    Note "auth problem -- delete the github line in ~/.git-credentials and run again"
    break
  }
  if ($i -lt $Attempts) { Start-Sleep -Seconds $GapSec }
}
if ($ok) { Note "DONE" } else { Note "GIVE-UP" }
if (-not $ok) { exit 1 }
