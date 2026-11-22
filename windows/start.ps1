# starts the watcher, which starts and keeps up the rest
. (Join-Path $PSScriptRoot 'common.ps1')

if (Get-WatcherProcess) {
    Write-Output 'Already running.'
    return
}
New-Item -ItemType Directory -Force $deviceFolder | Out-Null
# conhost --headless runs the watcher without a window, also at login
Start-Process (Join-Path $env:SystemRoot 'System32\conhost.exe') -ArgumentList @(
    '--headless', 'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$watcherScript`""
)
Write-Output 'Started. windows\stop.ps1 stops it, spotiafk status shows what it does.'
