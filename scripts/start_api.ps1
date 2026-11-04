# One-shot launcher for the demo backend: Docker -> twi-pg -> Ollama check -> api_server.py
# Usage (from anywhere):  powershell -ExecutionPolicy Bypass -File scripts\start_api.ps1 [-MaxChapter 586] [-Port 8000]
param([int]$MaxChapter = 0, [int]$Port = 8000)
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

# 4. API server (Ctrl+C to stop)
& "$root\.venv\Scripts\python.exe" "$root\scripts\api_server.py" "$root\data\emb" --port $Port --max-chapter $MaxChapter
