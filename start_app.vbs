Option Explicit

Dim shell
Dim fso
Dim baseDir
Dim command

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
command = Chr(34) & fso.BuildPath(baseDir, "start_app.bat") & Chr(34)

shell.CurrentDirectory = baseDir
shell.Run command, 0, False
