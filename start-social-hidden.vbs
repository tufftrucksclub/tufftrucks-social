' Starts Tuff Trucks Social in the background (no window). Does nothing if it's already running.
' Open it at http://localhost:8420. Stop it with stop-social.vbs. To watch it in a window instead, stop it and run start.bat.
Option Explicit
Dim sh, fso, dir, p, c, running
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)

running = False
For Each p In GetObject("winmgmts:\\.\root\cimv2").ExecQuery("SELECT CommandLine FROM Win32_Process WHERE Name='cmd.exe'")
  If Not IsNull(p.CommandLine) Then
    If InStr(1, p.CommandLine, "social-service.cmd", 1) > 0 Then running = True
  End If
Next
On Error Resume Next
For Each c In GetObject("winmgmts:\\.\root\StandardCimv2").ExecQuery("SELECT OwningProcess FROM MSFT_NetTCPConnection WHERE LocalPort=8420 AND State=2")
  running = True
Next
On Error GoTo 0
If running Then WScript.Quit 0

sh.CurrentDirectory = dir
sh.Run "cmd /c """ & dir & "\social-service.cmd""", 0, False
