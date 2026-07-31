param(
  [switch]$NoInstall,
  [switch]$NoOpen,
  [switch]$HiddenWindows
)

$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RootDir

function Write-Step {
  param([string]$Message)
  Write-Host ""
  Write-Host "==> $Message" -ForegroundColor Cyan
}

function Read-DotEnv {
  param([string]$Path)

  if (-not (Test-Path $Path)) {
    $ExamplePath = Join-Path $RootDir ".env.example"
    if (Test-Path $ExamplePath) {
      Copy-Item $ExamplePath $Path
    }
    throw ".env is missing. A template was copied from .env.example; fill it and rerun start.bat."
  }

  foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
    $trimmed = $line.Trim()
    if ($trimmed.Length -eq 0 -or $trimmed.StartsWith("#")) {
      continue
    }
    if ($trimmed -notmatch "^\s*([^#=\s]+)\s*=\s*(.*)\s*$") {
      continue
    }

    $name = $Matches[1].Trim()
    $value = $Matches[2].Trim()
    if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
      $value = $value.Substring(1, $value.Length - 2)
    }
    [Environment]::SetEnvironmentVariable($name, $value, "Process")
  }
}

function Get-EnvValue {
  param(
    [string]$Name,
    [string]$DefaultValue = ""
  )

  $value = [Environment]::GetEnvironmentVariable($Name, "Process")
  if ([string]::IsNullOrWhiteSpace($value)) {
    return $DefaultValue
  }
  return $value.Trim()
}

function Require-Command {
  param([string]$Name)
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    throw "Required command not found: $Name"
  }
}

function Test-Http {
  param([string]$Url)
  try {
    $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
    return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500)
  } catch {
    return $false
  }
}

function Wait-Http {
  param(
    [string]$Url,
    [int]$Seconds = 30
  )

  for ($i = 0; $i -lt $Seconds; $i++) {
    if (Test-Http $Url) {
      return $true
    }
    Start-Sleep -Seconds 1
  }
  return $false
}

function Start-CmdWindow {
  param(
    [string]$Title,
    [string]$Command,
    [string]$WorkingDirectory
  )

  $args = @("/k", "title $Title && $Command")
  $startArgs = @{
    FilePath = "cmd.exe"
    ArgumentList = $args
    WorkingDirectory = $WorkingDirectory
  }
  if ($HiddenWindows) {
    $startArgs["WindowStyle"] = "Hidden"
  }
  Start-Process @startArgs | Out-Null
}

Read-DotEnv (Join-Path $RootDir ".env")

$AdminPassword = Get-EnvValue "AUTO_TEAM_ADMIN_PASSWORD"
$AdminApiKey = Get-EnvValue "AUTO_TEAM_API_KEY"
if ($AdminPassword.Length -lt 8 -or $AdminPassword.StartsWith("change_me")) {
  throw "AUTO_TEAM_ADMIN_PASSWORD must be set in .env and must be at least 8 characters."
}
if ($AdminApiKey.Length -lt 12 -or $AdminApiKey.StartsWith("atk_change_me")) {
  throw "AUTO_TEAM_API_KEY must be set in .env."
}

$BackendHost = Get-EnvValue "AUTO_TEAM_BACKEND_HOST" "127.0.0.1"
$BackendPort = Get-EnvValue "AUTO_TEAM_BACKEND_PORT" "8000"
$FrontendHost = Get-EnvValue "AUTO_TEAM_FRONTEND_HOST" "127.0.0.1"
$FrontendPort = Get-EnvValue "AUTO_TEAM_FRONTEND_PORT" "5173"
$InstallDeps = (Get-EnvValue "AUTO_TEAM_INSTALL_DEPS" "1") -ne "0"
$OpenBrowser = ((Get-EnvValue "AUTO_TEAM_OPEN_BROWSER" "1") -ne "0") -and (-not $NoOpen)

$BackendDir = Join-Path $RootDir "backend"
$FrontendDir = Join-Path $RootDir "frontend"
$DataDir = Join-Path $BackendDir "data"
$SessionsDir = Join-Path $DataDir "sessions"
$VenvDir = Join-Path $BackendDir ".venv"
$PythonExe = Join-Path $VenvDir "Scripts\python.exe"

Write-Host "Auto Team one-click launcher" -ForegroundColor Green
Write-Host "Root: $RootDir"

Require-Command "python"
Require-Command "npm"

if (-not (Test-Path $DataDir)) {
  New-Item -ItemType Directory -Path $DataDir | Out-Null
}
if (-not (Test-Path $SessionsDir)) {
  New-Item -ItemType Directory -Path $SessionsDir | Out-Null
}

if ($InstallDeps -and -not $NoInstall) {
  Write-Step "Preparing Python virtualenv"
  if (-not (Test-Path $PythonExe)) {
    python -m venv $VenvDir
  }

  $RequirementsPath = Join-Path $BackendDir "requirements.txt"
  $PipStamp = Join-Path $VenvDir ".requirements.stamp"
  $NeedPipInstall = -not (Test-Path $PipStamp)
  if (-not $NeedPipInstall -and (Test-Path $RequirementsPath)) {
    $NeedPipInstall = (Get-Item $RequirementsPath).LastWriteTimeUtc -gt (Get-Item $PipStamp).LastWriteTimeUtc
  }
  if ($NeedPipInstall) {
    & $PythonExe -m pip install -r $RequirementsPath
    New-Item -ItemType File -Path $PipStamp -Force | Out-Null
  } else {
    Write-Host "Python dependencies already installed."
  }

  Write-Step "Preparing frontend dependencies"
  $NodeModules = Join-Path $FrontendDir "node_modules"
  if (-not (Test-Path $NodeModules)) {
    Push-Location $FrontendDir
    npm install
    Pop-Location
  } else {
    Write-Host "Node dependencies already installed."
  }
} else {
  if (-not (Test-Path $PythonExe)) {
    $PythonExe = "python"
  }
}

$BackendBrowseHost = $BackendHost
if ($BackendBrowseHost -eq "0.0.0.0" -or $BackendBrowseHost -eq "::") {
  $BackendBrowseHost = "127.0.0.1"
}
$FrontendBrowseHost = $FrontendHost
if ($FrontendBrowseHost -eq "0.0.0.0" -or $FrontendBrowseHost -eq "::") {
  $FrontendBrowseHost = "127.0.0.1"
}

$BackendHealthUrl = "http://${BackendBrowseHost}:$BackendPort/api/health"
$FrontendUrl = "http://${FrontendBrowseHost}:$FrontendPort"

Write-Step "Starting backend on $BackendHost`:$BackendPort"
if (Test-Http $BackendHealthUrl) {
  Write-Host "Backend is already responding: $BackendHealthUrl"
} else {
  $BackendCommand = "cd /d `"$BackendDir`" && `"$PythonExe`" -m uvicorn app.main:app --host $BackendHost --port $BackendPort"
  Start-CmdWindow -Title "Auto Team Backend" -Command $BackendCommand -WorkingDirectory $BackendDir
  if (-not (Wait-Http $BackendHealthUrl 45)) {
    throw "Backend did not become healthy at $BackendHealthUrl. Check the Backend window."
  }
  Write-Host "Backend healthy: $BackendHealthUrl"
}

Write-Step "Starting frontend on $FrontendHost`:$FrontendPort"
if (Test-Http $FrontendUrl) {
  Write-Host "Frontend is already responding: $FrontendUrl"
} else {
  $FrontendCommand = "cd /d `"$FrontendDir`" && npm run dev -- --host $FrontendHost --port $FrontendPort"
  Start-CmdWindow -Title "Auto Team Frontend" -Command $FrontendCommand -WorkingDirectory $FrontendDir
  if (Wait-Http $FrontendUrl 45) {
    Write-Host "Frontend ready: $FrontendUrl"
  } else {
    Write-Host "Frontend was started; it may still be compiling. Check the Frontend window." -ForegroundColor Yellow
  }
}

Write-Host ""
Write-Host "Frontend: $FrontendUrl" -ForegroundColor Green
Write-Host "Backend : http://${BackendBrowseHost}:$BackendPort" -ForegroundColor Green
Write-Host "Docs    : http://${BackendBrowseHost}:$BackendPort/docs" -ForegroundColor Green
Write-Host ""
Write-Host "Admin password: $AdminPassword"
Write-Host "Admin API key : $AdminApiKey"

if ($OpenBrowser) {
  Start-Process $FrontendUrl | Out-Null
}
