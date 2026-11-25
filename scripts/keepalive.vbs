' VRDUB keepalive launcher - GUI subsystem, never allocates a console window.
Dim fso, sh, dir
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Run "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & dir & "\keepalive.ps1""", 0, False