Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
ScriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
ScriptsDir = fso.GetParentFolderName(ScriptDir)
ProjectDir = fso.GetParentFolderName(ScriptsDir)
WshShell.CurrentDirectory = ProjectDir
WshShell.Run "cmd.exe /c pythonw.exe main.py", 0, False
