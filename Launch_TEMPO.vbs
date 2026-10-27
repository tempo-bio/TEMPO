' TEMPO graphical bootstrap and silent launcher.
Option Explicit
Dim shell, fs, root, pythonw, command
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
root = fs.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = root
pythonw = root & "\.venv\Scripts\pythonw.exe"
If fs.FileExists(pythonw) And fs.FileExists(root & "\.venv\tempo_setup_complete") Then
    command = Chr(34) & pythonw & Chr(34) & " " & Chr(34) & root & "\tempo_gui.py" & Chr(34)
Else
    command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Chr(34) & root & "\bootstrap_windows.ps1" & Chr(34)
End If
shell.Run command, 0, False
