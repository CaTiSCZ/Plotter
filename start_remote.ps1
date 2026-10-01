# FDDS SCADA — Remote launch via Scheduled Task
# ===============================================
# scada.py is a PyQt GUI app and cannot render inside a headless SSH session.
# This script creates a scheduled task that runs it on the interactive desktop
# session instead (same trick as utils/plotter/start_remote.ps1 in the FW repo),
# with the REST remote-control API (remote_control.py) force-enabled via the
# --remote_control CLI flag. Once running, use the HTTP API (see Invoke-Api
# below, or curl/Invoke-RestMethod) to inspect/drive the GUI from the SSH shell:
#   GET  /api/v1/status        - system/device/capture summary
#   GET  /api/v1/ui/state      - full widget tree + values
#   GET  /api/v1/log?tail=200  - tail of the GUI log pane
#   GET  /api/v1/screenshot    - PNG of the window (base64)
#   GET  /api/v1/plots?points= - decimated plot data
#   POST /api/v1/widgets/<id>  - click/set_text/set_value/select_index on a widget
#   POST /api/v1/measurement/start|stop|save
#   POST /api/v1/application/shutdown
#
# Usage (from SSH session):
#   powershell -ExecutionPolicy Bypass -File start_remote.ps1
#   powershell -ExecutionPolicy Bypass -File start_remote.ps1 -Action stop
#   powershell -ExecutionPolicy Bypass -File start_remote.ps1 -Action restart
#   powershell -ExecutionPolicy Bypass -File start_remote.ps1 -Action status

param(
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action = "start",
    [string]$ApiAddress = "127.0.0.1:8765",
    [string]$ApiToken = ""
)

$ErrorActionPreference = "Stop"
$TaskName = "FDDS_Scada"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = (Get-Command py -ErrorAction SilentlyContinue).Source
if (-not $PythonExe) { $PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $PythonExe) { Write-Error "Python not found on PATH"; exit 1 }

$ScadaScript = Join-Path $ScriptDir "scada.py"
$ApiBase = "http://$ApiAddress/api/v1"

function Invoke-Api {
    param([string]$Method, [string]$Path, $Body = $null)
    $headers = @{}
    if ($ApiToken) { $headers["Authorization"] = "Bearer $ApiToken" }
    try {
        if ($null -ne $Body) {
            return Invoke-RestMethod -Method $Method -Uri "$ApiBase$Path" -Headers $headers `
                -Body ($Body | ConvertTo-Json -Depth 5) -ContentType "application/json" -TimeoutSec 5
        }
        return Invoke-RestMethod -Method $Method -Uri "$ApiBase$Path" -Headers $headers -TimeoutSec 5
    } catch {
        return $null
    }
}

function Test-ScadaRunning {
    return ($null -ne (Invoke-Api -Method GET -Path "/status"))
}

switch ($Action) {
    "start" {
        if (Test-ScadaRunning) {
            Write-Host "[start_remote] SCADA already running."
            (Invoke-Api -Method GET -Path "/status") | ConvertTo-Json -Depth 5 | Write-Host
            exit 0
        }

        Write-Host "[start_remote] Creating scheduled task to launch SCADA..."

        $scadaArgs = "`"$ScadaScript`" --remote_control $ApiAddress"
        if ($ApiToken) { $scadaArgs += " --remote_control_token $ApiToken" }

        $taskAction = New-ScheduledTaskAction `
            -Execute $PythonExe `
            -Argument $scadaArgs `
            -WorkingDirectory $ScriptDir

        $principal = New-ScheduledTaskPrincipal `
            -UserId $env:USERNAME `
            -LogonType Interactive `
            -RunLevel Limited

        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Hours 0)

        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $taskAction `
            -Principal $principal `
            -Settings $settings | Out-Null

        Start-ScheduledTask -TaskName $TaskName
        Write-Host "[start_remote] Task started. Waiting for REST API at $ApiBase ..."

        for ($i = 0; $i -lt 40; $i++) {
            Start-Sleep -Milliseconds 500
            if (Test-ScadaRunning) {
                Write-Host "[start_remote] SCADA REST API is up."
                (Invoke-Api -Method GET -Path "/status") | ConvertTo-Json -Depth 5 | Write-Host
                exit 0
            }
        }
        Write-Host "[start_remote] WARNING: API did not respond within 20s. Check the interactive session / Task Scheduler manually."
        exit 1
    }

    "stop" {
        if (-not (Test-ScadaRunning)) {
            Write-Host "[start_remote] SCADA is not running (or API not reachable)."
        } else {
            Write-Host "[start_remote] Requesting graceful shutdown via REST API..."
            Invoke-Api -Method POST -Path "/application/shutdown" | Out-Null
            Start-Sleep -Seconds 2
        }
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "[start_remote] Stopped."
    }

    "restart" {
        & $MyInvocation.MyCommand.Path -Action stop -ApiAddress $ApiAddress -ApiToken $ApiToken
        Start-Sleep -Seconds 1
        & $MyInvocation.MyCommand.Path -Action start -ApiAddress $ApiAddress -ApiToken $ApiToken
    }

    "status" {
        $resp = Invoke-Api -Method GET -Path "/status"
        if ($null -eq $resp) {
            Write-Host "[start_remote] SCADA is NOT reachable at $ApiBase."
            exit 1
        }
        $resp | ConvertTo-Json -Depth 5 | Write-Host
    }
}
