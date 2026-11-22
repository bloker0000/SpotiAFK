# adds the startup shortcut for start.ps1, -Remove takes it away
param([switch]$Remove)

$shortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'SpotiAFK.lnk'
if ($Remove) {
    Remove-Item $shortcut -ErrorAction SilentlyContinue
    return
}

$link = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut)
# conhost --headless runs powershell without flashing a window at login
$link.TargetPath = Join-Path $env:SystemRoot 'System32\conhost.exe'
$start = Join-Path $PSScriptRoot 'start.ps1'
$link.Arguments = "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$start`""
$link.WorkingDirectory = Split-Path $PSScriptRoot
$link.Save()
