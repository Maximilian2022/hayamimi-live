Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = scriptDir

psCmd = "powershell"
If fso.FileExists("C:\Program Files\PowerShell\7\pwsh.exe") Then
    psCmd = """C:\Program Files\PowerShell\7\pwsh.exe"""
ElseIf fso.FileExists("C:\Program Files (x86)\PowerShell\7\pwsh.exe") Then
    psCmd = """C:\Program Files (x86)\PowerShell\7\pwsh.exe"""
Else
    Dim pathEnv, paths, p
    pathEnv = WshShell.ExpandEnvironmentStrings("%PATH%")
    paths = Split(pathEnv, ";")
    For Each p In paths
        If p <> "" And fso.FolderExists(p) Then
            If fso.FileExists(fso.BuildPath(p, "pwsh.exe")) Then
                psCmd = "pwsh"
                Exit For
            End If
        End If
    Next
End If

targetScript = scriptDir & "\scripts\launcher_gui.ps1"
If Not fso.FileExists(targetScript) Then
    If fso.FileExists(scriptDir & "\scripts\scripts\launcher_gui.ps1") Then
        targetScript = scriptDir & "\scripts\scripts\launcher_gui.ps1"
    Else
        MsgBox "Cannot find launcher script:" & vbCrLf & _
               scriptDir & "\scripts\launcher_gui.ps1" & vbCrLf & vbCrLf & _
               "Please check if the file exists.", _
               16, "hayamimi launcher error"
        WScript.Quit 1
    End If
End If

If InStr(psCmd, "pwsh") > 0 Then
    cmdLine = psCmd & " -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & targetScript & """"
Else
    cmdLine = psCmd & " -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command ""$s=[System.IO.File]::ReadAllText('" & Replace(targetScript, "'", "''") & "',[System.Text.Encoding]::UTF8);&([scriptblock]::Create($s))"""
End If

On Error Resume Next
WshShell.Run cmdLine, 0, False
If Err.Number <> 0 Then
    MsgBox "Failed to launch hayamimi (Error " & Err.Number & "):" & vbCrLf & _
           Err.Description & vbCrLf & vbCrLf & _
           "Command: " & cmdLine, _
           16, "hayamimi launcher error"
    WScript.Quit Err.Number
End If