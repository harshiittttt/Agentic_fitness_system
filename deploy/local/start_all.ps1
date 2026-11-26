# Start the whole AI Coach server stack on this PC:  Docker engine -> one container per person -> gateway -> public tunnel.
# Safe to run again. Normal use: double-click START_COACH.bat in the project folder.
#   powershell -ExecutionPolicy Bypass -File deploy\local\start_all.ps1            (everything)
#   powershell -ExecutionPolicy Bypass -File deploy\local\start_all.ps1 -SkipTunnel (keep the current public address)
param([switch]$SkipTunnel)

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
$logs = Join-Path $root "deploy\local\logs"
New-Item -ItemType Directory -Force $logs | Out-Null
$dockerBin = "D:\Docker\app\resources\bin"
if (Test-Path $dockerBin) { $env:Path = "$env:Path;$dockerBin" }
$cloudflared = "D:\Docker\tools\cloudflared\cloudflared.exe"
$gatewayPort = 8100

function Say($m) { Write-Host $m }
function EngineUp { try { $o = docker info 2>&1 | Out-String; return ($o -match "Server Version") } catch { return $false } }
function Listening($p) { return [bool](Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) }

# 1. Docker engine
if (-not (EngineUp)) {
  $dd = "D:\Docker\app\Docker Desktop.exe"
  if (-not (Test-Path $dd)) { Say "Docker Desktop was not found at $dd"; exit 1 }
  Say "Starting Docker Desktop (can take a minute or two)..."
  Start-Process $dd
  $t = 0
  while (-not (EngineUp) -and $t -lt 40) { Start-Sleep 5; $t++ }
  if (-not (EngineUp)) { Say "Docker did not start in time. Open Docker Desktop, wait until it says running, then run this again."; exit 1 }
}
Say "Docker engine: up"

# 2. one container per person (data stays in their volumes)
docker compose up -d 2>&1 | Select-Object -Last 4 | ForEach-Object { Say "  $_" }

# 3. gateway (one address for everyone; the access key picks the person)
if (-not (Listening $gatewayPort)) {
  Start-Process python -ArgumentList "deploy\local\gateway.py", "--port", "$gatewayPort" -WindowStyle Hidden `
    -RedirectStandardOutput "$logs\gateway.out.log" -RedirectStandardError "$logs\gateway.err.log" | Out-Null
  $t = 0
  while (-not (Listening $gatewayPort) -and $t -lt 20) { Start-Sleep 1; $t++ }
}
if (Listening $gatewayPort) { Say "Gateway: up on port $gatewayPort" } else { Say "Gateway did not start. See deploy\local\logs\gateway.err.log"; exit 1 }

# 3b. live voice agent (uses the GPU; optional: when it is off the app uses the phone's own recogniser). Setup: voice_agent\README.md
$voicePy = "D:\voice-proto\venv\Scripts\python.exe"
$voicePort = 8765
if (Test-Path $voicePy) {
  if (-not (Listening $voicePort)) {
    if (-not $env:VOICE_BRAIN) { $env:VOICE_BRAIN = "converse" }   # the Conversation Core (docs/CONVO_CONTRACTS.md); set VOICE_BRAIN=legacy before starting to go back
    Start-Process $voicePy -ArgumentList "voice_agent\server.py" -WindowStyle Hidden `
      -RedirectStandardOutput "$logs\voice.out.log" -RedirectStandardError "$logs\voice.err.log" | Out-Null
    $t = 0
    while (-not (Listening $voicePort) -and $t -lt 90) { Start-Sleep 1; $t++ }
  }
  if (Listening $voicePort) { Say "Live voice agent: up on port $voicePort" } else { Say "Live voice agent did not start (see deploy\local\logs\voice.err.log). The app will use the phone's recogniser." }
} else {
  Say "Live voice agent: not installed (optional, see voice_agent\README.md)"
}

# 4. public address (free Cloudflare quick tunnel: the address CHANGES every time it starts)
if ($SkipTunnel) {
  Say "Tunnel: skipped (keeping whatever is running)"
} elseif (-not (Test-Path $cloudflared)) {
  Say "Tunnel: cloudflared not found at $cloudflared, so there is no public address"
} else {
  Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
  Start-Sleep 1
  Remove-Item "$logs\tunnel.err.log", "$logs\tunnel.out.log" -ErrorAction SilentlyContinue
  Start-Process $cloudflared -ArgumentList "tunnel", "--url", "http://127.0.0.1:$gatewayPort", "--no-autoupdate" -WindowStyle Hidden `
    -RedirectStandardOutput "$logs\tunnel.out.log" -RedirectStandardError "$logs\tunnel.err.log" | Out-Null
  $addr = $null
  for ($i = 0; $i -lt 40 -and -not $addr; $i++) {
    Start-Sleep 2
    $m = Select-String -Path "$logs\tunnel.err.log" -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($m) { $addr = [regex]::Match($m.Line, "https://[a-z0-9-]+\.trycloudflare\.com").Value }
  }
  if (-not $addr) {
    Say "Tunnel: no address yet. See deploy\local\logs\tunnel.err.log"
  } else {
    Set-Content -Path (Join-Path $root "deploy\local\PUBLIC_ADDRESS.txt") -Value $addr -Encoding ascii
    $ok = "not reachable yet (try again in a minute)"
    for ($i = 0; $i -lt 8; $i++) {
      try { if ((Invoke-WebRequest "$addr/health" -UseBasicParsing -TimeoutSec 15).StatusCode -eq 200) { $ok = "reachable"; break } } catch { Start-Sleep 3 }
    }
    Say "Tunnel: $ok"
    Say ""
    Say "=================================================================="
    Say " Server address for the app (Connect to your coach):"
    Say "   $addr"
    Say " Each person's access key is in deploy\users\<name>.txt"
    Say " (the address is also saved in deploy\local\PUBLIC_ADDRESS.txt)"
    Say "=================================================================="
  }
}
Say ""
Say "Done. Keep Docker Desktop open. To stop everything: deploy\local\stop_all.ps1"
