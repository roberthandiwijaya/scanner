Option Explicit

Dim shell, files, scriptFolder, powerShell, scriptPath, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")

scriptFolder = files.GetParentFolderName(WScript.ScriptFullName)
powerShell = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
scriptPath = files.BuildPath(scriptFolder, "update-kiri-port-forwarding.ps1")

command = """" & powerShell & """ -NoProfile -NonInteractive -File """ & scriptPath & """"
WScript.Quit shell.Run(command, 0, True)
