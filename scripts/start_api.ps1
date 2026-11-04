# One-shot launcher for the demo backend: Docker -> twi-pg -> Ollama check -> [tunnel] -> api_server.py
#
#   powershell -ExecutionPolicy Bypass -File scripts\start_api.ps1                 # local only (emulator / adb reverse)
#   powershell -ExecutionPolicy Bypass -File scripts\start_api.ps1 -Tunnel         # + public Cloudflare URL, API key on
#   powershell -ExecutionPolicy Bypass -File scripts\start_api.ps1 -Lan            # + LAN clients on :8000, API key on
#   options: -MaxChapter 586  -Port 8000  -NewKey (rotate the API key)
#
# The API key lives in data\api_key.txt (gitignored) so it survives restarts; $env:TWI_API_KEY overrides it.
# Quick-tunnel URLs change every time the tunnel starts; the current one is also written to data\tunnel_url.txt.
param([int]$MaxChapter = 0, [int]$Port = 8000, [switch]$Tunnel, [switch]$Lan, [switch]$NewKey)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 1. Docker Desktop (the daemon isn't always running after a reboot)
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Starting Docker Desktop..."
    Start-Process "$env:LOCALAPPDATA\Programs\DockerDesktop\Docker Desktop.exe"
    for ($i = 0; $i -lt 60; $i++) { Start-Sleep 3; docker info *> $null; if ($LASTEXITCODE -eq 0) { break } }
    if ($LASTEXITCODE -ne 0) { throw "Docker daemon did not come up" }
}

# 2. Postgres container (it may spend a few seconds in crash recovery)
docker start twi-pg | Out-Null
for ($i = 0; $i -lt 30; $i++) { docker exec twi-pg pg_isready -U postgres *> $null; if ($LASTEXITCODE -eq 0) { break }; Start-Sleep 2 }
if ($LASTEXITCODE -ne 0) { throw "twi-pg is not accepting connections" }

# 3. Ollama
try { Invoke-WebRequest -UseBasicParsing http://localhost:11434/api/tags -TimeoutSec 5 | Out-Null }
catch { throw "Ollama is not responding on http://localhost:11434 -- start the Ollama app first" }

# 4. API key (required whenever anything beyond this PC can reach the server)
$keyFile = Join-Path $root "data\api_key.txt"
if ($Tunnel -or $Lan) {
    if (-not $env:TWI_API_KEY) {
        if ($NewKey -or -not (Test-Path $keyFile)) {
            $bytes = New-Object byte[] 24
            [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
            $k = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
            Set-Content -Path $keyFile -Value $k -Encoding ascii -NoNewline
        }
        $env:TWI_API_KEY = (Get-Content $keyFile -Raw).Trim()
    }
} else {
    $env:TWI_API_KEY = ""   # local-only: no key needed
}

# 5. Optional Cloudflare quick tunnel -> http://127.0.0.1:$Port
$cf = $null
if ($Tunnel) {
    $exe = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
    if (-not $exe) { $exe = "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe" }
    if (-not (Test-Path $exe)) { throw "cloudflared not found -- winget install --id Cloudflare.cloudflared" }
    $cfLog = Join-Path $root "data\logs\cloudflared.log"
    New-Item -ItemType Directory -Force (Split-Path $cfLog) | Out-Null
    if (Test-Path $cfLog) { Remove-Item $cfLog -Force }
    $cf = Start-Process -FilePath $exe -PassThru -WindowStyle Hidden -ArgumentList @(
        "tunnel", "--no-autoupdate", "--logfile", "`"$cfLog`"", "--url", "http://127.0.0.1:$Port")
    $url = $null
    for ($i = 0; $i -lt 40 -and -not $url; $i++) {
        Start-Sleep 1
        if (Test-Path $cfLog) {
            $m = Select-String -Path $cfLog -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' | Select-Object -First 1
            if ($m) { $url = $m.Matches[0].Value }
        }
    }
    if (-not $url) { Stop-Process -Id $cf.Id -Force; throw "cloudflared did not report a tunnel URL; see $cfLog" }
    Set-Content -Path (Join-Path $root "data\tunnel_url.txt") -Value $url -Encoding ascii -NoNewline
}

if ($Tunnel -or $Lan) {
    Write-Host ""
    Write-Host "================ give these to remote clients ================" -ForegroundColor Green
    if ($Tunnel) { Write-Host "  Server URL : $url" }
    if ($Lan) {
        $ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.PrefixOrigin -eq "Dhcp" } | Select-Object -First 1).IPAddress
        Write-Host "  LAN URL    : http://${ip}:$Port   (needs an inbound firewall rule for TCP $Port)"
    }
    Write-Host "  API key    : $env:TWI_API_KEY"
    Write-Host "  (browser demo page: the URL above, paste the key into the API key box)"
    Write-Host "===============================================================" -ForegroundColor Green
    Write-Host ""
}

# 6. API server (Ctrl+C to stop; the tunnel is stopped with it)
$serverHost = if ($Lan) { "0.0.0.0" } else { "127.0.0.1" }
try {
    & "$root\.venv\Scripts\python.exe" "$root\scripts\api_server.py" "$root\data\emb" --host $serverHost --port $Port --max-chapter $MaxChapter
} finally {
    if ($cf -and -not $cf.HasExited) { Stop-Process -Id $cf.Id -Force -ErrorAction SilentlyContinue }
}
