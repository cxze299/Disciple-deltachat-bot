$ErrorActionPreference = "Continue"

$botDir = $PSScriptRoot
Set-Location -LiteralPath $botDir

# Multi-site configuration. Edit sites.json to add websites and chat IDs.
$env:MENXUN_SITES_FILE = Join-Path $botDir "sites.json"
$env:MENXUN_ADMIN_KEY_FILE = Join-Path $botDir "data\admin-key.json"
$env:BOT_TIMEZONE = "Asia/Shanghai"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONUTF8 = "1"

$pythonPath = Join-Path $botDir ".venv\Scripts\python.exe"
$botPath = Join-Path $botDir "menxun_bot.py"
$configPath = Join-Path $botDir "data\cmsv-config"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    Write-Host "Python virtual environment not found: $pythonPath" -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}

Write-Host "Starting Menxun bot in DEBUG mode..." -ForegroundColor Cyan
Write-Host "Sites file: $env:MENXUN_SITES_FILE"
if (-not (Test-Path -LiteralPath $env:MENXUN_ADMIN_KEY_FILE)) {
    Write-Host "Admin key is not configured. Run set_admin_key.py when needed." -ForegroundColor Yellow
}
Write-Host "Keep this window open."

# -l debug enables detailed Delta Chat and bot logs.
& $pythonPath $botPath -l debug -c $configPath serve

$exitCode = $LASTEXITCODE
Write-Host "Bot stopped. Exit code: $exitCode" -ForegroundColor Yellow
Read-Host "Press Enter to close"
exit $exitCode
