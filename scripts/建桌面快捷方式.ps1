# Create (or refresh) the desktop shortcut for the dev checkout.
# ASCII only: Windows PowerShell 5.1 decodes BOM-less UTF-8 as ANSI,
# so every non-ASCII character is built from code points at runtime.
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$vbsName = [string][char]0x6253 + [char]0x5f00 + [char]0x63a7 + [char]0x5236 + [char]0x53f0 + ".vbs"
$vbs = Join-Path $root ("scripts\" + $vbsName)
$icon = Join-Path $root "packaging\vrdub.ico"
$desk = [Environment]::GetFolderPath("Desktop")
# "VRDub 无言势.lnk"
$lnkName = "VRDub " + [char]0x65E0 + [char]0x8A00 + [char]0x52BF + ".lnk"
$lnk = Join-Path $desk $lnkName
$w = New-Object -ComObject WScript.Shell
$s = $w.CreateShortcut($lnk)
$s.TargetPath = Join-Path $env:WINDIR "System32\wscript.exe"
$s.Arguments = '"' + $vbs + '"'
$s.WorkingDirectory = $root
if (Test-Path $icon) { $s.IconLocation = $icon + ",0" }
$s.Description = "VRDub - open the console (starts the backend if needed)"
$s.WindowStyle = 7
$s.Save()
Write-Host ("shortcut: " + $lnk)
Write-Host ("launcher: " + $vbs + "  exists=" + (Test-Path $vbs))
