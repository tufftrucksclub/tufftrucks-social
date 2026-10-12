' Stops Tuff Trucks Social, whether it was started in the background or in a window. Leaves the other apps alone.
' Finds the background service, anything listening on port 8420, and the processes that started or belong to them.
Option Explicit
Const PORT = 8420
Const SERVICE = "social-service.cmd"
Const FOLDER = "\tufftrucks-social\"
Dim wmi, quiet, procs, kill, seen, p, c, cur, nm, added, n, k, pass
quiet = WScript.Arguments.Named.Exists("quiet")
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = CreateObject("Scripting.Dictionary")
Set kill = CreateObject("Scripting.Dictionary")
For Each p In wmi.ExecQuery("SELECT ProcessId, ParentProcessId, Name, CommandLine, ExecutablePath FROM Win32_Process")
  procs(CStr(p.ProcessId)) = Array(CStr(p.ParentProcessId), LCase(p.Name), LCase(Nz(p.CommandLine)), LCase(Nz(p.ExecutablePath)))
Next

' The background service loop, and copies started from this app's folder
For Each k In procs.Keys
  nm = procs(k)(1)
  If nm = "cmd.exe" And InStr(procs(k)(2), LCase(SERVICE)) > 0 Then kill(k) = True
  If (nm = "cmd.exe" Or nm = "python.exe" Or nm = "pythonw.exe") And (InStr(procs(k)(2), LCase(FOLDER)) > 0 Or InStr(procs(k)(3), LCase(FOLDER)) > 0) Then kill(k) = True
Next

' Whatever is listening on the port, plus the launcher processes above it (node/python/cmd /c, never your editor or shell)
On Error Resume Next
For Each c In GetObject("winmgmts:\\.\root\StandardCimv2").ExecQuery("SELECT OwningProcess FROM MSFT_NetTCPConnection WHERE LocalPort=" & PORT & " AND State=2")
  cur = CStr(c.OwningProcess)
  Set seen = CreateObject("Scripting.Dictionary")
  Do While procs.Exists(cur) And Not seen.Exists(cur)
    seen(cur) = True
    nm = procs(cur)(1)
    If nm = "node.exe" Or nm = "python.exe" Or nm = "pythonw.exe" Or (nm = "cmd.exe" And InStr(procs(cur)(2), "/c") > 0) Then
      kill(cur) = True
      cur = procs(cur)(0)
    Else
      Exit Do
    End If
  Loop
Next
On Error GoTo 0

' Everything those started
Do
  added = False
  For Each k In procs.Keys
    If Not kill.Exists(k) And kill.Exists(procs(k)(0)) Then kill(k) = True : added = True
  Next
Loop While added

' The restart loop first, so it doesn't start the app again
n = 0
For pass = 1 To 2
  For Each p In wmi.ExecQuery("SELECT ProcessId, Name FROM Win32_Process")
    If kill.Exists(CStr(p.ProcessId)) Then
      If (pass = 1) = (LCase(p.Name) = "cmd.exe") Then
        On Error Resume Next
        p.Terminate
        If Err.Number = 0 Then n = n + 1
        On Error GoTo 0
      End If
    End If
  Next
Next

If Not quiet Then
  If n > 0 Then
    MsgBox "Tuff Trucks Social stopped.", vbInformation, "Tuff Trucks Social"
  Else
    MsgBox "Tuff Trucks Social wasn't running.", vbInformation, "Tuff Trucks Social"
  End If
End If

Function Nz(v)
  If IsNull(v) Then Nz = "" Else Nz = v
End Function
