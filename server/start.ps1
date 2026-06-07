# Start Crow server + evo daemon + cloudflared tunnel
# Usage: .\start.ps1  (optionally: .\start.ps1 -NoEvo to skip the daemon)
param([switch]$NoEvo)

Set-Location $PSScriptRoot
if (-not $env:CROW_PASSWORD) {
    Write-Error "CROW_PASSWORD is not set. Export it before running: `$env:CROW_PASSWORD = 'yourpassword'"
    exit 1
}
# $env:CROW_SECRET = "a-long-random-string"  # optional: set for stable session signing

$flask = Start-Process python -ArgumentList "server.py" -PassThru -NoNewWindow
Write-Host "Server started       (PID $($flask.Id))  http://localhost:7429"

if (-not $NoEvo) {
    Start-Sleep -Seconds 2   # let server initialize crow.db before daemon touches it
    $evo = Start-Process python -ArgumentList "evolve.py" -PassThru -NoNewWindow
    Write-Host "Evo daemon started   (PID $($evo.Id))"
} else {
    $evo = $null
    Write-Host "Evo daemon skipped   (-NoEvo flag)"
}

Write-Host "Remote: https://crow.sinceremickey.com"
Write-Host ""
Write-Host "Press Ctrl+C to stop all." -ForegroundColor DarkGray

try {
    cloudflared tunnel --config cloudflared.yml run
} finally {
    Stop-Process -Id $flask.Id -Force -ErrorAction SilentlyContinue
    if ($evo) { Stop-Process -Id $evo.Id -Force -ErrorAction SilentlyContinue }
    Write-Host "Stopped."
}
