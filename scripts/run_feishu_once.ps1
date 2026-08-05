param(
  [string]$ConfigPath = "config/feishu_automation.local.json"
)

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $ScriptDir
$LogDir = Join-Path $RootDir "logs"
$Date = Get-Date -Format "yyyyMMdd"
$LogPath = Join-Path $LogDir "feishu-automation-$Date.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location $RootDir

function Write-RunLog {
  param([string]$Message)
  $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
  Write-Output $line
  Add-Content -Path $LogPath -Value $line -Encoding UTF8
}

function Invoke-Runner {
  param(
    [string]$Mode
  )

  Write-RunLog "Start mode: $Mode"
  $output = & python "scripts/feishu_task_runner.py" --config $ConfigPath --mode $Mode 2>&1
  $exitCode = $LASTEXITCODE
  foreach ($line in $output) {
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
    Write-Output $line
  }
  if ($exitCode -eq 0) {
    Write-RunLog "Mode completed: $Mode"
  } else {
    Write-RunLog "Mode failed: $Mode, exit code: $exitCode"
  }
  return $exitCode
}

Write-RunLog "Feishu automation run started."
$executeCode = Invoke-Runner -Mode "execute"
$pushCode = Invoke-Runner -Mode "push-approved"
Write-RunLog "Feishu automation run finished. execute=$executeCode, push-approved=$pushCode"

if ($executeCode -ne 0 -or $pushCode -ne 0) {
  exit 1
}
exit 0
