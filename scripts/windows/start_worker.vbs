Option Explicit

Dim shell
Dim fso
Dim baseDir
Dim repoRoot
Dim scriptPath
Dim command

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
repoRoot = fso.GetParentFolderName(fso.GetParentFolderName(baseDir))
scriptPath = fso.BuildPath(baseDir, "start_worker.ps1")
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Chr(34) & scriptPath & Chr(34)

shell.CurrentDirectory = repoRoot
shell.Run command, 0, False
