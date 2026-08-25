$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    throw "Project Python environment not found. Create it with: python -m venv .venv"
}

$backend = Start-Process -FilePath $python -ArgumentList 'backend\app.py' -WorkingDirectory $root -PassThru
$frontend = Start-Process -FilePath $python -ArgumentList '-m http.server 5500 --bind 127.0.0.1' -WorkingDirectory $root -PassThru

Write-Host "Backend:  http://127.0.0.1:5000/api/health"
Write-Host "Frontend: http://127.0.0.1:5500/frontend/index.html"
Write-Host "Backend PID: $($backend.Id)"
Write-Host "Frontend PID: $($frontend.Id)"
Write-Host 'Stop with: Stop-Process -Id <PID>'
