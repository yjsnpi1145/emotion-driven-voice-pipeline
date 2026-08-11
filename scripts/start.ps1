#requires -Version 7
<#
.SYNOPSIS
  Start the control plane as a hidden process and wait for readiness.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory)][string]$Config,
  [string]$PythonExecutable,
  [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location $RepoRoot

$RunFile = Join-Path $RepoRoot 'runtime\run\processes.json'
$LogDir = Join-Path $RepoRoot 'runtime\logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
. (Join-Path $PSScriptRoot 'process-registry.ps1')

$ConfigAbs = (Resolve-Path -LiteralPath $Config).Path
if (-not $PythonExecutable) {
  $ControlVenv = Join-Path $RepoRoot '.venv-control\Scripts\python.exe'
  $PythonExecutable = if (Test-Path -LiteralPath $ControlVenv -PathType Leaf) {
    $ControlVenv
  } else {
    Join-Path $RepoRoot '.venv\Scripts\python.exe'
  }
}
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
  Write-Error "control python missing: $PythonExecutable"
  exit 1
}

# Reject a second instance if the run file references the same live control
# process.  If the control process is gone, preserve whether recorded model
# workers are still alive; the Python runtime safely reconciles exact-match
# orphan listeners before the next worker launch.
if (Test-Path -LiteralPath $RunFile -PathType Leaf) {
  $run = Get-Content -LiteralPath $RunFile -Raw | ConvertFrom-Json
  if (Test-RecordedProcessIdentity -Record $run.control) {
    $liveControlPid = [int]$run.control.pid
    Write-Error "control plane already running (pid $liveControlPid)"
    exit 2
  }
  $archive = Move-StaleProcessRegistry -RunFile $RunFile -RunPayload $run
  if ($archive.classification -eq 'orphaned') {
    $workerList = ($archive.live_worker_pids -join ', ')
    Write-Warning "stale control registry contains live model worker PID(s): $workerList; ownership preserved for safe startup reconciliation"
  }
}

# Launch the control process (hidden window).
$StdoutLog = Join-Path $LogDir 'control.stdout.log'
$StderrLog = Join-Path $LogDir 'control.stderr.log'
$procInfo = Start-Process -FilePath $PythonExecutable `
  -ArgumentList @('-m', 'voice_pipeline', 'serve', '--config', $ConfigAbs) `
  -WindowStyle Hidden -PassThru -RedirectStandardOutput $StdoutLog -RedirectStandardError $StderrLog
$controlPid = $procInfo.Id
$controlCreateTime = (Get-Process -Id $controlPid).StartTime.ToUniversalTime()
$controlEpoch = ($controlCreateTime - [datetime]::new(1970,1,1,0,0,0,[datetimekind]::Utc)).TotalSeconds

function Cleanup-Control {
  try {
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $controlPid" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
      Stop-Process -Id $child.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Stop-Process -Id $controlPid -Force -ErrorAction SilentlyContinue
  } catch {}
}

# Wait for health (up to 120s).
$HealthUrl = 'http://127.0.0.1:8765/api/v1/health'
$Health = $null
$deadline = (Get-Date).AddSeconds(120)
do {
  try {
    $Health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5
    if ($Health.status -eq 'ready' -and
        $Health.storage.status -eq 'ready' -and
        $Health.dispatcher.state -eq 'running' -and
        $Health.quality.status -eq 'ready') { break }
  } catch {
    Start-Sleep -Milliseconds 500
  }
} while ((Get-Date) -lt $deadline)

function Test-IsDescendant {
  param([int]$ChildPid, [int]$AncestorPid)
  $current = $ChildPid
  for ($i = 0; $i -lt 16; $i++) {
    if ($current -eq $AncestorPid) { return $true }
    $parent = (Get-CimInstance Win32_Process -Filter "ProcessId = $current" -ErrorAction SilentlyContinue)
    if (-not $parent -or $null -eq $parent.ParentProcessId) { return $false }
    $current = [int]$parent.ParentProcessId
    if ($current -le 0) { return $false }
  }
  return $false
}

if (-not $Health) {
  Cleanup-Control
  Write-Error 'control plane did not become ready'
  exit 3
}
# The launched interpreter may be a venv redirector that spawns the real
# interpreter as a child; accept a direct descendant of the launched PID.
$HealthPid = [int]$Health.control.pid
if ($HealthPid -ne $controlPid -and -not (Test-IsDescendant -ChildPid $HealthPid -AncestorPid $controlPid)) {
  Cleanup-Control
  Write-Error 'health control.pid does not belong to the launched process'
  exit 3
}
$controlPid = $HealthPid
$controlCreateTime = (Get-Process -Id $controlPid).StartTime.ToUniversalTime()
$controlEpoch = ($controlCreateTime - [datetime]::new(1970,1,1,0,0,0,[datetimekind]::Utc)).TotalSeconds
if (-not $Health.control.instance_id) {
  Cleanup-Control
  Write-Error 'health instance_id is empty'
  exit 3
}

# Persist the PID registry via the supervisor (start.ps1 must not write a
# separate static PID list; the supervisor owns runtime/run/processes.json).
$instanceId = $Health.control.instance_id
$auditLog = $Health.control.audit_log
New-Item -ItemType Directory -Force -Path (Split-Path $RunFile) | Out-Null

$payload = [ordered]@{
  schema_version = 1
  instance_id = $instanceId
  audit_log = $auditLog
  control = @{ pid = $controlPid; create_time = $controlEpoch }
  engine_lifecycle = $Health.engine_lifecycle
  workers = @{
    indextts = $Health.workers.indextts
    gpt_sovits = $Health.workers.gpt_sovits
  }
  updated_at_utc = (Get-Date).ToUniversalTime().ToString('o')
}
$tmp = Join-Path (Split-Path $RunFile) ('.processes.' + [guid]::NewGuid().ToString('N') + '.tmp')
$payload | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $tmp -Encoding UTF8
Move-Item -LiteralPath $tmp -Destination $RunFile -Force

if ($Json) {
  [pscustomobject]@{
    control_url = 'http://127.0.0.1:8765'
    control_pid = $controlPid
    control_create_time = $controlEpoch
    instance_id = $instanceId
    audit_log = $auditLog
    database_path = $Health.storage.database_path
    alembic_revision = $Health.storage.alembic_revision
    run_file = $RunFile
  } | ConvertTo-Json | Write-Output
} else {
  Write-Host "control plane started: pid=$controlPid instance=$instanceId"
}
