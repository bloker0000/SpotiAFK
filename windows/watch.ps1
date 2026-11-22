# keeps the player and farm running, only speaks up when it needs you
. (Join-Path $PSScriptRoot 'common.ps1')

$checkIntervalSeconds = 60
$needsYouAfterMinutes = 10
$loginWaitMinutes = 2
$failedStartsBeforeNotice = 3
$watchLog = Join-Path $deviceFolder 'watch.log'
$conditionSince = @{}
$noticed = @{}

function Write-WatchLog {
    param([string]$Message)
    "$(Get-Date -Format 'dd/MM/yyyy HH:mm:ss') $Message" | Add-Content -Path $watchLog -Encoding utf8
}

function Update-Condition {
    # one notice per problem, again only after it went away
    param([string]$Name, [bool]$IsTrue, [double]$AfterMinutes, [string]$Text)
    if (-not $IsTrue) {
        $conditionSince.Remove($Name)
        $noticed.Remove($Name)
        return
    }
    if (-not $conditionSince.ContainsKey($Name)) {
        $conditionSince[$Name] = Get-Date
    }
    $lastedMinutes = ((Get-Date) - $conditionSince[$Name]).TotalMinutes
    if ($lastedMinutes -ge $AfterMinutes -and -not $noticed.ContainsKey($Name)) {
        Write-WatchLog "notice: $Text"
        Show-Notice $Text
        $noticed[$Name] = $true
    }
}

function Read-FarmStatus {
    try {
        & $python -m spotiafk status --json 2>$null | ConvertFrom-Json
    } catch {
        $null
    }
}

New-Item -ItemType Directory -Force $deviceFolder | Out-Null
Set-Content -Path $watchLog -Value "$(Get-Date -Format 'dd/MM/yyyy HH:mm:ss') watcher started" -Encoding utf8
$failedStarts = 0
while ($true) {
    $isChromeMissing = $false
    try {
        if (-not (Get-PlayerProcess)) {
            Write-WatchLog 'starting the web player'
            Start-Player
        }
        if (-not (Test-Path $playerShownFlag)) {
            & (Join-Path $PSScriptRoot 'player-window.ps1') -WaitSeconds 30
        }
    } catch {
        $isChromeMissing = $true
        Write-WatchLog "web player: $_"
    }
    Update-Condition 'chrome' $isChromeMissing 0 'Chrome is not installed, the hidden web player needs it.'

    if (Get-FarmProcess) {
        $failedStarts = 0
    } else {
        $failedStarts += 1
        Write-WatchLog 'starting the farm'
        Start-Farm
    }
    $keepsStopping = $failedStarts -ge $failedStartsBeforeNotice
    Update-Condition 'farm' $keepsStopping 0 'SpotiAFK keeps stopping. Run spotiafk doctor to see why.'

    $status = Read-FarmStatus
    $state = ''
    if ($status) {
        $state = $status.state
    }
    $loginText = 'Spotify wants you to log in again, look for the login tab in your browser.'
    Update-Condition 'login' ($state -eq 'logging in') $loginWaitMinutes $loginText
    $deviceText = 'The hidden web player is offline. Run windows\player-window.ps1 -Show and log in to Spotify there.'
    Update-Condition 'device' ($state -eq 'waiting for device') $needsYouAfterMinutes $deviceText
    Update-Condition 'network' ($state -eq 'retrying') $needsYouAfterMinutes "Can't reach Spotify: $($status.error)"

    Start-Sleep -Seconds $checkIntervalSeconds
}
