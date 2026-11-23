$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $projectDir "data"
$logPath = Join-Path $logDir "startup.log"

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Write-StartupLog {
    param([string] $Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logPath -Value "[$timestamp] $Message"
}

Set-Location $projectDir
Write-StartupLog "Starting scanner Docker stack."

$dockerDesktop = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
if (Test-Path $dockerDesktop) {
    Start-Process -FilePath $dockerDesktop -WindowStyle Hidden -ErrorAction SilentlyContinue
}

$dockerReady = $false
for ($attempt = 1; $attempt -le 60; $attempt++) {
    cmd /c "docker info >NUL 2>NUL"
    if ($LASTEXITCODE -eq 0) {
        $dockerReady = $true
        break
    }

    Start-Sleep -Seconds 2
}

if (-not $dockerReady) {
    Write-StartupLog "Docker did not become ready."
    exit 1
}

$composeArgs = @("compose", "-f", "docker-compose.yml")
$driveConfig = Join-Path $projectDir "docker-compose.drive.yml"
if (Test-Path -LiteralPath $driveConfig -PathType Leaf) {
    $composeArgs += @("-f", "docker-compose.drive.yml")
    Write-StartupLog "Including backup-drive configuration: docker-compose.drive.yml."
}

# If a configured drive is unavailable, fail rather than silently starting
# a replacement recorder without its backup mount.
& docker @composeArgs up -d *> $null
if ($LASTEXITCODE -ne 0) {
    Write-StartupLog "Docker Compose startup failed with exit code $LASTEXITCODE. Check Docker and the configured backup drive."
    exit $LASTEXITCODE
}

Write-StartupLog "Scanner Docker stack is running."
