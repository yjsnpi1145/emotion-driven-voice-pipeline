#requires -Version 7
<#
.SYNOPSIS
  Graceful shutdown of the control plane with process-tree fallback and a
  machine-readable stop receipt.  Hard 10 second overall budget.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory)][string]$RunFile,
  [Parameter(Mandatory)][string]$ReceiptPath,
  [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$StartedAt = Get-Date
$StartedEpoch = ($StartedAt.ToUniversalTime() - [datetime]::new(1970,1,1,0,0,0,[datetimekind]::Utc)).TotalSeconds

$ShutdownHttp = [ordered]@{
  attempted = $false
  timeout_seconds = 6
  outcome = 'not_attempted'
  status_code = $null
}
$ObservedProcesses = @()
$RunFileDeleted = $false
$Status = 'stopped'

function Add-ObservedProcess {
  param([string]$Role, [int]$ProcessId, [double]$CreateTime, [int]$ParentPid, [string]$StopMethod, [bool]$VerifiedExited)
  $script:ObservedProcesses += [pscustomobject]@{
    role = $Role
    pid = $ProcessId
    create_time = $CreateTime
    parent_pid = $ParentPid
    stop_method = $StopMethod
    verified_exited = $VerifiedExited
    verified_at_utc = (Get-Date).ToUniversalTime().ToString('o')
  }
}

function Get-CreateTimeEpoch {
  param([int]$ProcessId)
  try {
    $proc = Get-Process -Id $ProcessId -ErrorAction Stop
    return ($proc.StartTime.ToUniversalTime() - [datetime]::new(1970,1,1,0,0,0,[datetimekind]::Utc)).TotalSeconds
  } catch {
    return $null
  }
}

function Test-PidAliveWithCreateTime {
  param([int]$ProcessId, [double]$ExpectedCreateTime)
  try {
    $actual = Get-CreateTimeEpoch $ProcessId
    if ($null -eq $actual) { return $false }
    return [math]::Abs($actual - $ExpectedCreateTime) -lt 1.0
  } catch {
    return $false
  }
}

if (-not (Test-Path -LiteralPath $RunFile -PathType Leaf)) {
  $Status = 'stopped'
  $Receipt = [ordered]@{
    schema_version = 1
    instance_id = $null
    run_file = $RunFile
    started_at_utc = $StartedAt.ToUniversalTime().ToString('o')
    finished_at_utc = (Get-Date).ToUniversalTime().ToString('o')
    elapsed_seconds = 0
    shutdown_http = $ShutdownHttp
    observed_processes = $ObservedProcesses
    run_file_deleted = $false
    status = 'stopped'
  }
  $Receipt | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $ReceiptPath -Encoding UTF8
  if ($Json) { $Receipt | ConvertTo-Json -Depth 10 | Write-Output }
  exit 1
}

$Run = Get-Content -LiteralPath $RunFile -Raw | ConvertFrom-Json
$InstanceId = $Run.instance_id
$ControlPid = [int]$Run.control.pid
$ControlCreateTime = [double]$Run.control.create_time
$BaseUrl = 'http://127.0.0.1:8765'

# 1. Graceful shutdown request (6s client timeout).
if ($ControlPid -gt 0 -and (Test-PidAliveWithCreateTime $ControlPid $ControlCreateTime)) {
  $ShutdownHttp.attempted = $true
  try {
    $resp = Invoke-WebRequest -Uri "$BaseUrl/api/v1/control/shutdown" -Method Post -TimeoutSec 6
    $ShutdownHttp.outcome = 'completed'
    $ShutdownHttp.status_code = [int]$resp.StatusCode
  } catch [System.Net.WebException] {
    $ShutdownHttp.outcome = 'connection_error'
    $ShutdownHttp.status_code = $null
  } catch {
    $ShutdownHttp.outcome = 'timeout'
    $ShutdownHttp.status_code = $null
  }
} else {
  $ShutdownHttp.attempted = $false
  $ShutdownHttp.outcome = 'not_attempted'
}

# 2. Re-read the registry and fall back to process-tree termination for any
#    PID/create-time matching record still alive.
$Run = Get-Content -LiteralPath $RunFile -Raw | ConvertFrom-Json
$AllPids = [System.Collections.Generic.List[object]]::new()
$AllPids.Add([pscustomobject]@{ role = 'control'; pid = $ControlPid; create_time = $ControlCreateTime })
foreach ($workerName in @('indextts', 'gpt_sovits')) {
  $worker = $Run.workers.$workerName
  if ($worker.pid -and [int]$worker.pid -gt 0) {
    $AllPids.Add([pscustomobject]@{ role = $workerName; pid = [int]$worker.pid; create_time = [double]$worker.create_time })
  }
}

$AliveNow = @()
foreach ($entry in $AllPids) {
  $alive = Test-PidAliveWithCreateTime $entry.pid $entry.create_time
  if ($alive) {
    $AliveNow += $entry
    # find children
    try {
      $proc = Get-Process -Id $entry.pid -ErrorAction Stop
      $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $($entry.pid)" -ErrorAction SilentlyContinue
      foreach ($child in $children) {
        $childCreate = Get-CreateTimeEpoch $child.ProcessId
        if ($null -ne $childCreate) {
          $AliveNow += [pscustomobject]@{ role = 'child'; pid = [int]$child.ProcessId; create_time = $childCreate; parent = $entry.pid }
        }
      }
    } catch {}
  }
}

foreach ($entry in $AliveNow) {
  $parentPid = if ($entry.role -eq 'child') { $entry.parent } else { $null }
  $stopMethod = 'graceful'
  if ($ShutdownHttp.outcome -ne 'completed') { $stopMethod = 'terminate' }
  try {
    $proc = Get-Process -Id $entry.pid -ErrorAction Stop
    if ($stopMethod -eq 'graceful') {
      try { $proc.CloseMainWindow() | Out-Null } catch {}
      Start-Sleep -Milliseconds 300
    }
    if (-not $proc.HasExited) {
      $stopMethod = 'kill'
      $proc | Stop-Process -Force
      $proc.WaitForExit()
    }
    $verified = -not (Test-PidAliveWithCreateTime $entry.pid $entry.create_time)
  } catch {
    $verified = -not (Test-PidAliveWithCreateTime $entry.pid $entry.create_time)
    if ($verified) { $stopMethod = 'already_exited' }
  }
  if ($verified) {
    Add-ObservedProcess -Role $entry.role -ProcessId $entry.pid -CreateTime $entry.create_time `
      -ParentPid $parentPid -StopMethod $stopMethod -VerifiedExited $true
  } else {
    Add-ObservedProcess -Role $entry.role -ProcessId $entry.pid -CreateTime $entry.create_time `
      -ParentPid $parentPid -StopMethod $stopMethod -VerifiedExited $false
  }
}

# 3. Delete the run file only after all verified.
$NotVerified = @($ObservedProcesses | Where-Object { -not $_.verified_exited })
$AllVerified = ($ObservedProcesses.Count -gt 0) -and ($NotVerified.Count -eq 0)
$Elapsed = ((Get-Date) - $StartedAt).TotalSeconds
if ($AllVerified -and $Elapsed -le 10) {
  Remove-Item -LiteralPath $RunFile -Force
  $RunFileDeleted = $true
  $Status = 'stopped'
} else {
  $Status = 'failed'
}

$Receipt = [ordered]@{
  schema_version = 1
  instance_id = $InstanceId
  run_file = $RunFile
  started_at_utc = $StartedAt.ToUniversalTime().ToString('o')
  finished_at_utc = (Get-Date).ToUniversalTime().ToString('o')
  elapsed_seconds = [math]::Round($Elapsed, 3)
  shutdown_http = $ShutdownHttp
  observed_processes = $ObservedProcesses
  run_file_deleted = $RunFileDeleted
  status = $Status
}
$Receipt | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $ReceiptPath -Encoding UTF8
if ($Json) { $Receipt | ConvertTo-Json -Depth 10 | Write-Output }

if ($Status -ne 'stopped') {
  Write-Error "stop incomplete: status=$Status"
  exit 1
}
exit 0
