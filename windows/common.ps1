# shared by the other scripts in this folder, dot-source it
$root = Split-Path $PSScriptRoot
$deviceFolder = Join-Path $root 'device'
$playerProfile = Join-Path $deviceFolder 'web-player'
# while this exists the watcher leaves the window on screen
$playerShownFlag = Join-Path $deviceFolder 'player-window-shown'
$watcherScript = Join-Path $PSScriptRoot 'watch.ps1'
$python = Join-Path $root 'venv\Scripts\python.exe'

function Find-Chrome {
    # chrome installs for all users or just you
    $candidates = @(
        (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
    )
    $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}

function Get-PlayerProcess {
    # only the chrome running the player profile, never your own chrome
    Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" |
        Where-Object { $_.CommandLine -like "*$playerProfile*" }
}

function Get-FarmProcess {
    Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like '*-m spotiafk run*' }
}

function Get-WatcherProcess {
    Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
        Where-Object { $_.CommandLine -like "*$watcherScript*" }
}

function Start-Player {
    $chrome = Find-Chrome
    if (-not $chrome) {
        throw 'Chrome is not installed, the hidden web player needs it'
    }
    Start-Process $chrome -ArgumentList @(
        "--user-data-dir=`"$playerProfile`"",
        '--app=https://open.spotify.com',
        '--mute-audio',
        '--autoplay-policy=no-user-gesture-required',
        '--no-first-run',
        '--no-default-browser-check',
        '--hide-crash-restore-bubble',
        # a tucked away window still has to answer play commands right away
        '--disable-background-timer-throttling',
        '--disable-renderer-backgrounding',
        '--disable-backgrounding-occluded-windows'
    )
}

function Show-Notice {
    param([string]$Text)
    [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
    $toastText = [Windows.UI.Notifications.ToastTemplateType]::ToastText02
    $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($toastText)
    $lines = $template.GetElementsByTagName('text')
    [void]$lines.Item(0).AppendChild($template.CreateTextNode('SpotiAFK'))
    [void]$lines.Item(1).AppendChild($template.CreateTextNode($Text))
    $notice = [Windows.UI.Notifications.ToastNotification]::new($template)
    # windows only shows toasts from apps it knows, powershell is one
    $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($notice)
}

function Start-Farm {
    # pythonw has no console, the farm logs to ~/.local/share/spotiafk/logs itself
    $pythonWithoutConsole = Join-Path $root 'venv\Scripts\pythonw.exe'
    Start-Process $pythonWithoutConsole -ArgumentList '-m', 'spotiafk', 'run' -WorkingDirectory $root
}
