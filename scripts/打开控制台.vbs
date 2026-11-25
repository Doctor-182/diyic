' VRDub launcher (ASCII only on purpose: wscript reads .vbs as ANSI).
' 1) if the backend already listens on 8899, just open the console page;
' 2) otherwise start run.bat nobrowser hidden, wait up to ~30 s, then open it.
Option Explicit
Dim fso, sh, root, url, i, probe, f
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
url = "http://127.0.0.1:8899/"

If PortOpen() Then
  sh.Run url, 1, False
  WScript.Quit 0
End If

sh.CurrentDirectory = root
sh.Run "cmd /c """ & root & "\run.bat"" nobrowser", 0, False

For i = 1 To 60
  WScript.Sleep 500
  If PortOpen() Then
    sh.Run url, 1, False
    WScript.Quit 0
  End If
Next
MsgBox "VRDub backend did not come up in 30 s." & vbCrLf & _
       "Check the logs under %LOCALAPPDATA%\VRDub\out\srv.log", 48, "VRDub"
WScript.Quit 1

Function PortOpen()
  Dim tmp
  tmp = sh.ExpandEnvironmentStrings("%TEMP%") & "\vrdub_port.txt"
  sh.Run "cmd /c netstat -ano -p tcp | findstr LISTENING | findstr :8899 > """ & tmp & """", 0, True
  PortOpen = False
  If fso.FileExists(tmp) Then
    Set f = fso.OpenTextFile(tmp, 1)
    If Not f.AtEndOfStream Then
      If InStr(f.ReadAll, ":8899") > 0 Then PortOpen = True
    End If
    f.Close
  End If
End Function
