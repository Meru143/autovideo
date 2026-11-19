# Registers the factory tick with Windows Task Scheduler: `python -m factory tick` every 15 minutes (Plan 3a Task 6).
# Run it only with the operator's OK. -Billing api uses the test key in secrets/anthropic_api.env (the operator's
# choice for the first autonomous run, 5 Nov); subscription (the default) uses the factory's own sign-in.
#   .\tools\install_tick.ps1 -Billing api        register (or replace) the task
#   .\tools\install_tick.ps1 -Unregister         remove it
param(
    [ValidateSet("api", "subscription")][string]$Billing = "subscription",
    [switch]$Unregister
)
$ErrorActionPreference = "Stop"
$name = "autovideo-tick"
if ($Unregister) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Output "removed $name"
    return
}
$repo = Split-Path -Parent $PSScriptRoot
$set = if ($Billing -eq "api") { "set FACTORY_BILLING=api&&" } else { "set FACTORY_BILLING=&&" }
$arguments = "/c $set cd /d `"$repo`" && .venv\Scripts\python.exe -m factory tick >> factory\out\tick.log 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)
# Parallel instances: the tick's own lock serialises them, and a job the tick started may outlive its instance.
# No time limit, so Windows never kills a job the tick started. It runs only while the operator is signed in.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances Parallel `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force `
    -Description "autovideo factory tick every 15 minutes (billing: $Billing)" | Out-Null
Write-Output "registered $name every 15 minutes (billing: $Billing); log: factory\out\tick.log"
