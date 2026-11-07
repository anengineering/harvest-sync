<#
Registers a daily Windows scheduled task that runs `harvest-sync sync --apply`.
Only needed if this laptop, rather than the Linux server, runs the schedule; don't schedule both.

Run from the repository folder, after `uv sync` and creating config.toml and .env:
    powershell -ExecutionPolicy Bypass -File deploy\register-windows-task.ps1
    powershell -ExecutionPolicy Bypass -File deploy\register-windows-task.ps1 -Time 18:00
#>
param(
    [string]$Time = "23:30",
    [string]$TaskName = "harvest-sync"
)

$repo = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $repo ".venv\Scripts\harvest-sync.exe"
if (-not (Test-Path $exe)) {
    throw "Couldn't find $exe. Run 'uv sync' in $repo first."
}
$config = Join-Path $repo "config.toml"
$log = Join-Path $repo "logs\harvest-sync.log"

$action = New-ScheduledTaskAction -Execute $exe -WorkingDirectory $repo `
    -Argument "--config `"$config`" --log-file `"$log`" sync --apply"
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
# StartWhenAvailable runs a missed sync when the laptop next wakes up.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force `
    -Description "One-way sync of Harvest time entries to the client's Harvest" | Out-Null

Write-Host "Registered '$TaskName' to run daily at $Time. Output is appended to $log"
Write-Host "To remove it: Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
