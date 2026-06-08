<#
.SYNOPSIS
    Cold-Email Research Assistant - production deploy script (Windows).

    app (uvicorn, 2 workers) :8000 inside the container, published on host :4767.
    Served behind the edge nginx under /coldemail/  ->  127.0.0.1:4767.
    Public entrypoint: https://ai.arttechgroup.com:7777/coldemail/

.EXAMPLE
    .\deploy.ps1            # pull, build, (re)start the stack, wait for health
    .\deploy.ps1 -NoPull    # skip git pull (deploy the current checkout)
#>
[CmdletBinding()]
param(
    [switch]$NoPull
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

# docker compose v2 (plugin) preferred, fall back to legacy docker-compose.
$Compose = $null
try {
    docker compose version *> $null
    if ($LASTEXITCODE -eq 0) { $Compose = 'docker compose' }
} catch {}
if (-not $Compose) {
    if (Get-Command docker-compose -ErrorAction SilentlyContinue) {
        $Compose = 'docker-compose'
    } else {
        Write-Error "Docker Compose not found. Install Docker Desktop first."
        exit 1
    }
}

# Secrets must exist (injected at runtime via env_file, never baked into the image).
if (-not (Test-Path .env)) {
    Write-Host "X .env is missing. Create it from the template:" -ForegroundColor Red
    Write-Host "     Copy-Item .env.example .env   # then set OPENAI_API_KEY (+ APIFY_TOKEN for LinkedIn)"
    exit 1
}

function Invoke-Compose {
    param([string[]]$Args)
    & ($Compose -split ' ')[0] (($Compose -split ' ')[1..99] + $Args)
    if ($LASTEXITCODE -ne 0) { throw "compose $($Args -join ' ') failed (exit $LASTEXITCODE)" }
}

if (-not $NoPull -and (Test-Path .git)) {
    Write-Host "> Pulling latest changes..."
    git pull --ff-only
}

Write-Host "> Building image..."
Invoke-Compose @('build')

Write-Host "> Starting stack..."
Invoke-Compose @('up', '-d')

Write-Host "> Waiting for the app to come up..."
$ok = $false
foreach ($i in 1..30) {
    try {
        $resp = Invoke-WebRequest -Uri 'http://localhost:4767/api/health' -UseBasicParsing -TimeoutSec 3
        if ($resp.StatusCode -eq 200) { $ok = $true; break }
    } catch {}
    Start-Sleep -Seconds 2
}

Write-Host ""
Invoke-Compose @('ps')
Write-Host ""
if ($ok) {
    Write-Host "OK Deploy complete - health 200 on localhost:4767." -ForegroundColor Green
    Write-Host "   Public URL (via edge nginx): https://ai.arttechgroup.com:7777/coldemail/"
    Write-Host "   (Container is mounted under /coldemail; the UI works through the edge, not direct on :4767.)"
} else {
    Write-Host "!! Stack started but health check did not return 200 in time." -ForegroundColor Yellow
    Write-Host "   Check logs:  $Compose logs -f"
    exit 1
}
