Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
ScriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
ScriptsDir = fso.GetParentFolderName(ScriptDir)
ProjectDir = fso.GetParentFolderName(ScriptsDir)
WshShell.CurrentDirectory = ProjectDir
' Truyen duong dan tuyet doi de stop-background.ps1 nhan dung tien trinh cua project nay
WshShell.Run "cmd.exe /c pythonw.exe """ & ProjectDir & "\main.py""", 0, False
