# Stop the AI Coach server stack (tunnel, gateway, containers). Data is kept: nothing is deleted.
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
$dockerBin = "D:\Docker\app\resources\bin"
if (Test-Path $dockerBin) { $env:Path = "$env:Path;$dockerBin" }

Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like "*deploy\local\gateway.py*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host "Tunnel and gateway stopped."
docker compose stop 2>&1 | Select-Object -Last 3 | ForEach-Object { Write-Host "  $_" }
Write-Host "Containers stopped. Data is kept in the Docker volumes. Start again with START_COACH.bat"
