param(
  [int]$IntervalMinutes = 60,
  [string]$TaskName = "FeishuCodexAutomation"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunnerPath = Join-Path $ScriptDir "run_feishu_once.ps1"

if (-not (Test-Path -LiteralPath $RunnerPath)) {
  throw "Runner script not found: $RunnerPath"
}

$action = New-ScheduledTaskAction `
  -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$RunnerPath`""

$trigger = New-ScheduledTaskTrigger `
  -Once `
  -At (Get-Date).AddMinutes(1) `
  -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
  -RepetitionDuration ([TimeSpan]::MaxValue)

$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -MultipleInstances IgnoreNew

Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $action `
  -Trigger $trigger `
  -Settings $settings `
  -Description "Run Feishu Bitable Codex automation once, then exit." `
  -Force | Out-Null

Write-Output "Registered Windows scheduled task: $TaskName, every $IntervalMinutes minutes."
Write-Output "Runner script: $RunnerPath"
