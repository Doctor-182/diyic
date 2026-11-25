# Cold-start check: kill leftovers, launch via keepalive.vbs, report process layout.
Set-Location "D:\vrc无言势"
for ($i = 0; $i -lt 3; $i++) {
  Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='pythonw.exe'" | Where-Object { $_.CommandLine -match "vrdub" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" | Where-Object { $_.CommandLine -match "run.bat" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Milliseconds 900
}
$left = @(Get-CimInstance Win32_Process -Filter "Name='cmd.exe' or Name='python.exe' or Name='pythonw.exe'" | Where-Object { $_.CommandLine -match "vrdub|run.bat" })
"left after cleanup: " + $left.Count
Start-Process -FilePath "wscript.exe" -ArgumentList '"D:\vrc无言势\scripts\keepalive.vbs"'
Start-Sleep -Seconds 20
try { $h = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8899/api/health" -TimeoutSec 8).Content } catch { $h = "health failed: $_" }
"health: $h"
""
"=== processes ==="
Get-CimInstance Win32_Process -Filter "Name='cmd.exe' or Name='python.exe' or Name='pythonw.exe' or Name='wscript.exe'" |
  Where-Object { $_.CommandLine -match "vrdub|run.bat|keepalive" } |
  ForEach-Object { "{0,6} {1,-12} {2}" -f $_.ProcessId, $_.Name, ($_.CommandLine -replace "\s+"," ") }
""
"=== keepalive.log (user dir) ==="
$kp = Join-Path $env:LOCALAPPDATA "VRDub\out\keepalive.log"
if (Test-Path $kp) { Get-Content $kp -Tail 4 -Encoding UTF8 } else { "(none yet)" }
