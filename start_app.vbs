Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
scriptPath = Chr(34) & fso.BuildPath(baseDir, "desktop_app.py") & Chr(34)

Sub TryRun(command)
  On Error Resume Next
  shell.Run command, 0, False
  If Err.Number = 0 Then WScript.Quit 0
  Err.Clear
  On Error GoTo 0
End Sub

localApp = shell.ExpandEnvironmentStrings("%LocalAppData%")
TryRun "pyw -3.14 " & scriptPath
TryRun "pyw -3 " & scriptPath
If fso.FileExists(localApp & "\Python\pythoncore-3.14-64\pythonw.exe") Then TryRun Chr(34) & localApp & "\Python\pythoncore-3.14-64\pythonw.exe" & Chr(34) & " " & scriptPath
If fso.FileExists(localApp & "\Programs\Python\Python314\pythonw.exe") Then TryRun Chr(34) & localApp & "\Programs\Python\Python314\pythonw.exe" & Chr(34) & " " & scriptPath
shell.Run "py -3.14 " & Chr(34) & fso.BuildPath(baseDir, "desktop_app.py") & Chr(34), 0, False
