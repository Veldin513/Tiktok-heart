Option Explicit
Dim shell, files, root, python, script
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
python = files.BuildPath(root, ".venv\Scripts\pythonw.exe")
script = files.BuildPath(root, "scripts\recover_profiles.py")
If Not files.FileExists(python) Then
  MsgBox "Open INSTALL.md and install Python dependencies first.", vbExclamation, "TikTok Heart"
  WScript.Quit 1
End If
shell.CurrentDirectory = root
shell.Run """" & python & """ """ & script & """", 0, False
