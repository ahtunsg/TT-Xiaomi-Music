' Put a shortcut to this file in the Windows Startup folder for autostart.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.Run """" & root & "\scripts\run_service.bat""", 0, False
