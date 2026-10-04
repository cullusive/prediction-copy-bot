# Registers a Windows scheduled task that runs `python -m copybot collect` every day.
# Run once from this repo folder in PowerShell:  .\scripts\schedule_collect.ps1
# Remove it later with:  Unregister-ScheduledTask -TaskName copybot-collect

param(
    [string]$Time = "04:00",
    [string]$Python = (Get-Command python).Source
)

$repo = Split-Path -Parent $PSScriptRoot
$log = Join-Path $repo "collect.log"
$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"$Python`" -m copybot collect -v >> `"$log`" 2>&1" `
    -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
# StartWhenAvailable: if the PC was off at $Time, run as soon as it's back on.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6)
Register-ScheduledTask -TaskName "copybot-collect" -Action $action `
    -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Scheduled copybot collect daily at $Time (log: $log)"
