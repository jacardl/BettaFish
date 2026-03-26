param(
  [switch]$ResetDb = $false
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

function Remove-DirectoryIfExists {
  param([string]$Path)
  if (Test-Path -LiteralPath $Path) {
    Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue
  }
}

Write-Host "Stopping containers..."
if ($ResetDb) {
  docker compose down --remove-orphans -v | Out-Host
} else {
  docker compose down --remove-orphans | Out-Host
}

Write-Host "Cleaning Python cache directories..."
Get-ChildItem -Path $projectRoot -Recurse -Directory -Force -Filter "__pycache__" -ErrorAction SilentlyContinue | ForEach-Object {
  Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
}
Remove-DirectoryIfExists (Join-Path $projectRoot ".pytest_cache")

Write-Host "Starting containers (build enabled)..."
docker compose up -d --build | Out-Host

Write-Host ""
docker compose ps | Out-Host

Write-Host ""
Write-Host "URLs:"
Write-Host "  Main:    http://localhost:5000"
Write-Host "  Adminer: http://localhost:8080"
