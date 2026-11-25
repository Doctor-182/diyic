# VRDUB keepalive: make sure the console at 127.0.0.1:8899 is up.
# ASCII only: Windows PowerShell 5.1 mis-decodes BOM-less UTF-8, and a CJK path
# inside the script can swallow the closing quote. Root comes from the script path.
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$log  = Join-Path $root "out\keepalive.log"
function Say($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Out-File -Append -Encoding utf8 $log }

$listen = Get-NetTCPConnection -LocalPort 8899 -State Listen -ErrorAction SilentlyContinue
if ($listen) { exit 0 }

# backend python already running? give it time, unless it is stuck.
$py = @(Get-CimInstance Win32_Process -Filter "Name like '%python%'" | Where-Object { $_.CommandLine -like '*vrdub*' })
if ($py.Count -gt 0) {
  $young = $false
  foreach ($p in $py) {
    $age = ((Get-Date) - $p.CreationDate).TotalSeconds
    if ($age -lt 120) { $young = $true }
  }
  if ($young) { exit 0 }
  foreach ($p in $py) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
  Say "backend python stuck (>120s, port closed) -> killed"
}

# leftover run.bat window (crashed / paused) would fight us: clear it first.
$bat = @(Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" | Where-Object { $_.CommandLine -like '*run.bat*' })
foreach ($b in $bat) { Stop-Process -Id $b.ProcessId -Force -ErrorAction SilentlyContinue }
if ($bat.Count -gt 0) { Say ("stale run.bat window(s) killed: " + $bat.Count) }

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $env:ComSpec
$psi.Arguments = '/c run.bat nobrowser'
$psi.WorkingDirectory = $root
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
[void][System.Diagnostics.Process]::Start($psi)
Say "port closed -> started run.bat"
