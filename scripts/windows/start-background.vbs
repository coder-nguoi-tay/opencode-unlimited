Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
ScriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
ProjectDir = fso.GetParentFolderName(ScriptDir)
WshShell.CurrentDirectory = ProjectDir
WshShell.Run "cmd.exe /c run.bat", 0, False
