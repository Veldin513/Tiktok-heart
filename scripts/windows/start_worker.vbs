Option Explicit

Dim shell
Dim fso
Dim baseDir
Dim repoRoot
Dim scriptPath
Dim command
Dim exitCode
Dim extraArgs
Dim index

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
repoRoot = fso.GetParentFolderName(fso.GetParentFolderName(baseDir))
scriptPath = fso.BuildPath(baseDir, "start_worker.ps1")
extraArgs = ""
For index = 0 To WScript.Arguments.Count - 1
    Select Case LCase(CStr(WScript.Arguments(index)))
        Case "--boot-force", "-bootforce"
            extraArgs = extraArgs & " -BootForce"
        Case "--self-test", "-selftest"
            extraArgs = extraArgs & " -SelfTest"
        Case "--no-browser", "-nobrowser"
            extraArgs = extraArgs & " -NoBrowser"
        Case Else
            extraArgs = extraArgs & " " & QuoteArgument(WScript.Arguments(index))
    End Select
Next

command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Chr(34) & scriptPath & Chr(34) & extraArgs

shell.CurrentDirectory = repoRoot
exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode

Function QuoteArgument(value)
    Dim text
    text = CStr(value)
    text = Replace(text, Chr(34), Chr(34) & Chr(34))
    If InStr(text, " ") > 0 Or InStr(text, vbTab) > 0 Or InStr(text, Chr(34)) > 0 Then
        QuoteArgument = Chr(34) & text & Chr(34)
    Else
        QuoteArgument = text
    End If
End Function
