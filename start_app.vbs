Option Explicit

Dim shell
Dim fso
Dim baseDir
Dim command
Dim executable
Dim python
Dim environment
Dim activateScript

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = baseDir
Set environment = shell.Environment("PROCESS")
environment("YARA_PROJECT_ROOT") = baseDir
python = fso.BuildPath(baseDir, ".venv\Scripts\python.exe")
If environment("YARA_PYTHON") = "" And fso.FileExists(python) Then
    environment("YARA_PYTHON") = python
End If

activateScript = fso.BuildPath(baseDir, "scripts\windows\activate_existing_app.ps1")
If fso.FileExists(activateScript) Then
    command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File " & Chr(34) & activateScript & Chr(34)
    If shell.Run(command, 0, True) = 0 Then WScript.Quit 0
End If

executable = fso.BuildPath(baseDir, "src-tauri\target\release\yara-control.exe")
If Not fso.FileExists(executable) Then executable = fso.BuildPath(baseDir, "src-tauri\target\debug\yara-control.exe")
If fso.FileExists(executable) Then
    shell.Run Chr(34) & executable & Chr(34), 1, False
Else
    command = Chr(34) & shell.ExpandEnvironmentStrings("%ComSpec%") & Chr(34) & " /d /s /c " & Chr(34) & Chr(34) & fso.BuildPath(baseDir, "start_app.bat") & Chr(34) & Chr(34)
    shell.Run command, 0, False
End If
