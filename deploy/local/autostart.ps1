<#
Register / remove a Windows Scheduled Task 'AICoachHost' that starts the local host (run.py) at logon, hidden.
  powershell -ExecutionPolicy Bypass -File deploy\local\autostart.ps1 install
  powershell -ExecutionPolicy Bypass -File deploy\local\autostart.ps1 uninstall
  powershell -ExecutionPolicy Bypass -File deploy\local\autostart.ps1 status
Changes ONLY the scheduled task. It prints (never runs) the power-setting commands below.
#>
param([Parameter(Position = 0)][ValidateSet('install', 'uninstall', 'status')][string]$Action = 'status')

$task = 'AICoachHost'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$run = Join-Path $here 'run.py'
$py = (Get-Command python -ErrorAction Stop).Source
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = $py }   # pythonw = no console window

switch ($Action) {
    'install' {
        $a = New-ScheduledTaskAction -Execute $pyw -Argument "`"$run`"" -WorkingDirectory (Split-Path (Split-Path $here))
        $t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
             -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask -TaskName $task -Action $a -Trigger $t -Settings $s -Description 'AI Coach local host (gateway + backends)' -Force | Out-Null
        Write-Host "Installed '$task' (runs at logon as $env:USERNAME, hidden). Start now: Start-ScheduledTask -TaskName $task"
    }
    'uninstall' {
        Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "Removed '$task'. If it was running, also: python deploy\local\run.py status  (stop any leftover runner from its console / Task Manager)."
    }
    'status' {
        Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue | Select-Object TaskName, State
        python $run status
    }
}

Write-Host ""
Write-Host "KEEP THE PC AWAKE (NOT applied by this script; run yourself in an admin prompt if you want it):"
Write-Host "  powercfg /change standby-timeout-ac 0      # never sleep while plugged in"
Write-Host "  powercfg /change hibernate-timeout-ac 0"
Write-Host "  powercfg /change monitor-timeout-ac 15     # screen may still turn off"
Write-Host "Undo example: powercfg /change standby-timeout-ac 30"
Write-Host "A laptop on battery will still sleep per its battery settings; closing the lid also sleeps unless the lid action is set to 'Do nothing'."
