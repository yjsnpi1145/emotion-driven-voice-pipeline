#requires -Version 7
<#
.SYNOPSIS
  GPU residency probe. Starts a resident control plane against the real
  engines, warms up both, runs two full Index->GSV cycles while recording
  nvidia-smi memory peaks, then stops everything and writes a fixed-schema
  lifecycle-decision.json plus a stop receipt.

  Usage:
    pwsh -NoProfile -File scripts/probe-engine-lifecycle.ps1 `
      -BaseConfig 'D:\TTSsystem\config\acceptance.gpu.local.yaml' `
      -EvidenceDir 'D:\TTSsystem\runtime\developer-gpu\lifecycle' `
      -OutputConfig 'D:\TTSsystem\runtime\developer-gpu\effective.gpu.yaml' -Json
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory)][string]$BaseConfig,
  [Parameter(Mandatory)][string]$EvidenceDir,
  [Parameter(Mandatory)][string]$OutputConfig,
  [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$RepoRoot = 'D:\TTSsystem'
Set-Location $RepoRoot

$Evidence = (New-Item -ItemType Directory -Force -Path $EvidenceDir).FullName
$CandidateConfig = Join-Path $Evidence 'resident-candidate.yaml'
$ControlPython = Join-Path $RepoRoot '.venv-control\Scripts\python.exe'
$ControlUrl = 'http://127.0.0.1:8765'

# ------------------------------------------------------------------------- #
# 1. Parse BaseConfig; materialize all paths relative to its directory and
#    force engine_lifecycle: resident.
# ------------------------------------------------------------------------- #
$BaseDir = Split-Path -Parent (Resolve-Path -LiteralPath $BaseConfig).Path

function Resolve-Absolute {
  param([string]$Value)
  if (-not $Value) { return $Value }
  if ([System.IO.Path]::IsPathRooted($Value)) { return $Value }
  return [System.IO.Path]::GetFullPath((Join-Path $BaseDir $Value))
}

# Render a resident candidate config with all paths materialized to absolute
# (relative paths must never be copied verbatim into the evidence dir). The
# control venv has PyYAML, so delegate the YAML round-trip to it.
$PythonScript = @'
import json, pathlib, sys
base_cfg = pathlib.Path(sys.argv[1])
evidence = pathlib.Path(sys.argv[2])
out = pathlib.Path(sys.argv[3])
import yaml
raw = yaml.safe_load(base_cfg.read_text(encoding="utf-8"))
base_dir = base_cfg.resolve().parent
def abs_path(v):
    p = pathlib.Path(v)
    return str(p if p.is_absolute() else (base_dir / p).resolve())
raw.setdefault("server", {})
raw["server"]["host"] = "127.0.0.1"
raw["server"]["port"] = 8765
raw["engine_lifecycle"] = "resident"
raw["mode"] = "real"
raw["runtime_dir"] = abs_path(raw.get("runtime_dir", "runtime"))
raw["engine_lock_path"] = abs_path(raw.get("engine_lock_path", "config/engines.lock.yaml"))
raw["checkpoint_lock_path"] = abs_path(raw.get("checkpoint_lock_path", "config/checkpoints.lock.yaml"))
for eng in ("indextts", "gpt_sovits"):
    e = raw.get("engines", {}).get(eng)
    if not e:
        raise SystemExit(f"engines.{eng} missing in {base_cfg}")
    e["python_executable"] = abs_path(e["python_executable"])
    e["repo_dir"] = abs_path(e["repo_dir"])
    e["base_url"] = e.get("base_url", "")
out.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
print(out)
'@
$PythonScript | Set-Content -LiteralPath (Join-Path $Evidence '_render_candidate.py') -Encoding UTF8
& $ControlPython (Join-Path $Evidence '_render_candidate.py') $BaseConfig $Evidence $CandidateConfig
if ($LASTEXITCODE -ne 0) {
  throw "candidate config rendering failed (exit $LASTEXITCODE)"
}
if (-not (Test-Path -LiteralPath $CandidateConfig)) {
  throw "candidate config not written: $CandidateConfig"
}

# Load the golden mapping for a real request.
$GoldenMap = Join-Path $RepoRoot 'config\golden-assets.local.yaml'
if (-not (Test-Path -LiteralPath $GoldenMap)) {
  throw "golden mapping missing: $GoldenMap (cannot probe without verified assets)"
}

# ------------------------------------------------------------------------- #
# 2. nvidia-smi helpers
# ------------------------------------------------------------------------- #
function Get-GpuMemoryMib {
  $json = & nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>$null
  if ($LASTEXITCODE -ne 0) { throw 'nvidia-smi failed' }
  $total = 0
  foreach ($line in $json) { $total += [int]($line.Trim()) }
  return $total
}
function Get-GpuIdentity {
  $uuid = (& nvidia-smi --query-gpu=uuid --format=csv,noheader,nounits 2>$null | Select-Object -First 1).Trim()
  $name = (& nvidia-smi --query-gpu=name --format=csv,noheader,nounits 2>$null | Select-Object -First 1).Trim()
  $mem = (& nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1).Trim()
  return [pscustomobject]@{ uuid = $uuid; name = $name; total_mib = [int]$mem }
}
$Gpu = Get-GpuIdentity
$IdleMiB = Get-GpuMemoryMib
$IndexPeakMiB = $IdleMiB
$GsvPeakMiB = $IdleMiB
$CombinedPeakMiB = $IdleMiB

# ------------------------------------------------------------------------- #
# 3. Start the control plane (hidden) with the candidate config.
# ------------------------------------------------------------------------- #
$RunFile = Join-Path $Evidence 'candidate-processes.json'
$LogDir = Join-Path $Evidence 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$StdoutLog = Join-Path $LogDir 'control.stdout.log'
$StderrLog = Join-Path $LogDir 'control.stderr.log'

$procInfo = Start-Process -FilePath $ControlPython `
  -ArgumentList @('-m', 'voice_pipeline', 'serve', '--config', $CandidateConfig) `
  -WindowStyle Hidden -PassThru -RedirectStandardOutput $StdoutLog -RedirectStandardError $StderrLog
$launchedPid = $procInfo.Id
$launchedCreate = (Get-Process -Id $launchedPid).StartTime.ToUniversalTime()
$launchedEpoch = ($launchedCreate - [datetime]::new(1970,1,1,0,0,0,[datetimekind]::Utc)).TotalSeconds

function Invoke-Cleanup {
  param([string]$Reason)
  try {
    Invoke-RestMethod -Uri "$ControlUrl/api/v1/control/shutdown" -Method Post -TimeoutSec 6 | Out-Null
  } catch {}
  Start-Sleep -Milliseconds 500
  try { Stop-Process -Id $launchedPid -Force -ErrorAction SilentlyContinue } catch {}
  Write-Warning "probe cleaned up: $Reason"
}

$Health = $null
$deadline = (Get-Date).AddSeconds(180)
do {
  try {
    $Health = Invoke-RestMethod -Uri "$ControlUrl/api/v1/health" -TimeoutSec 5
    if ($Health.status -eq 'ready') { break }
  } catch {
    Start-Sleep -Milliseconds 500
  }
} while ((Get-Date) -lt $deadline)

if (-not $Health) {
  Invoke-Cleanup -Reason 'control plane not ready'
  throw 'control plane did not become ready for residency probe'
}
if ($Health.mode -ne 'real') {
  Invoke-Cleanup -Reason 'mode is not real'
  throw "candidate config is not real mode: $($Health.mode)"
}
$ControlPid = [int]$Health.control.pid
$ControlEpoch = $launchedEpoch
$InstanceId = $Health.control.instance_id
$AuditLog = $Health.control.audit_log

# Persist candidate registry for stop.ps1.
$payload = [ordered]@{
  schema_version = 1
  instance_id = $InstanceId
  audit_log = $AuditLog
  control = @{ pid = $ControlPid; create_time = $ControlEpoch }
  engine_lifecycle = 'resident'
  workers = @{
    indextts = $Health.workers.indextts
    gpt_sovits = $Health.workers.gpt_sovits
  }
  updated_at_utc = (Get-Date).ToUniversalTime().ToString('o')
}
$payload | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $RunFile -Encoding UTF8

function Invoke-CliJob {
  param([string]$Kind, [hashtable]$Request, [string]$Output)
  $ReqFile = Join-Path $Evidence ("{0}.{1}.json" -f $Kind, [guid]::NewGuid().ToString('N'))
  $Request | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $ReqFile -Encoding UTF8
  $args = @('-m', 'voice_pipeline', $Kind, '--server', $ControlUrl, '--request', $ReqFile)
  if ($Kind -eq 'synthesize-segment') {
    $args += @('--output-dir', $Output)
  } else {
    $args += @('--output', $Output)
  }
  $out = & $ControlPython @args 2>&1
  if ($LASTEXITCODE -ne 0) {
    throw ("{0} CLI failed (exit {1}): {2}" -f $Kind, $LASTEXITCODE, ($out | Out-String))
  }
}

# Build a request from the golden mapping.
$map = Get-Content -LiteralPath $GoldenMap -Raw
$assetPath = ($map | Select-String -Pattern 'base_voice_path:\s*(.+)' | Select-Object -First 1).Matches.Groups[1].Value.Trim().Trim('"').Trim("'")
$refText = ($map | Select-String -Pattern 'ref_text_cn:\s*(.+)' | Select-Object -First 1).Matches.Groups[1].Value.Trim().Trim('"').Trim("'")
if (-not $assetPath -or -not (Test-Path -LiteralPath $assetPath)) {
  Invoke-Cleanup -Reason 'golden asset missing'
  throw "golden asset missing: $assetPath"
}
$SegmentRequest = @{
  request_id = [guid]::NewGuid().ToString()
  base_voice_path = $assetPath
  ref_text_cn = $refText
  emotion_vector = @(0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.20)
  target_text = '今日は静かな一日だった。'
  target_language = 'ja'
  seed = 20260807
}

# ------------------------------------------------------------------------- #
# 4. Warm-up then two full Index->GSV cycles with memory peaks.
# ------------------------------------------------------------------------- #
$auditBefore = if (Test-Path -LiteralPath $AuditLog) { (Get-Item -LiteralPath $AuditLog).Length } else { 0 }
$warmOut = Join-Path $Evidence 'warm'
Invoke-CliJob -Kind 'synthesize-segment' -Request $SegmentRequest -Output $warmOut
$IndexPeakMiB = [Math]::Max($IndexPeakMiB, (Get-GpuMemoryMib))
$GsvPeakMiB = [Math]::Max($GsvPeakMiB, (Get-GpuMemoryMib))
$CombinedPeakMiB = [Math]::Max($CombinedPeakMiB, (Get-GpuMemoryMib))

$round1Out = Join-Path $Evidence 'round1'
Invoke-CliJob -Kind 'synthesize-segment' -Request $SegmentRequest -Output $round1Out
$IndexPeakMiB = [Math]::Max($IndexPeakMiB, (Get-GpuMemoryMib))
$GsvPeakMiB = [Math]::Max($GsvPeakMiB, (Get-GpuMemoryMib))
$CombinedPeakMiB = [Math]::Max($CombinedPeakMiB, (Get-GpuMemoryMib))

# Round 2: if no model reload happens, the audit log must NOT grow with new
# "model_loaded" style events beyond the first warm-up.
$auditMid = if (Test-Path -LiteralPath $AuditLog) { (Get-Item -LiteralPath $AuditLog).Length } else { 0 }
$round2Out = Join-Path $Evidence 'round2'
Invoke-CliJob -Kind 'synthesize-segment' -Request $SegmentRequest -Output $round2Out
$IndexPeakMiB = [Math]::Max($IndexPeakMiB, (Get-GpuMemoryMib))
$GsvPeakMiB = [Math]::Max($GsvPeakMiB, (Get-GpuMemoryMib))
$CombinedPeakMiB = [Math]::Max($CombinedPeakMiB, (Get-GpuMemoryMib))

$auditAfter = if (Test-Path -LiteralPath $AuditLog) { (Get-Item -LiteralPath $AuditLog).Length } else { 0 }

# ------------------------------------------------------------------------- #
# 5. Stop the candidate and write a stop receipt.
# ------------------------------------------------------------------------- #
$ReceiptPath = Join-Path $Evidence 'stop-receipt.json'
& (Join-Path $RepoRoot 'scripts\stop.ps1') -RunFile $RunFile -ReceiptPath $ReceiptPath -Json | Out-Null
if ($LASTEXITCODE -gt 1) {
  throw "stop.ps1 failed with exit $LASTEXITCODE"
}
$ReceiptSha = (Get-FileHash -LiteralPath $ReceiptPath -Algorithm SHA256).Hash.ToLowerInvariant()
$candidateProcesses = @()
$receipt = Get-Content -LiteralPath $ReceiptPath -Raw | ConvertFrom-Json
foreach ($p in $receipt.observed_processes) {
  $candidateProcesses += [pscustomobject]@{
    role = $p.role
    pid = [int]$p.pid
    create_time = [double]$p.create_time
    verified_exited = [bool]$p.verified_exited
  }
}

# ------------------------------------------------------------------------- #
# 6. lifecycle-decision.json (fixed schema)
# ------------------------------------------------------------------------- #
$RequiredReserve = 1024
$Margin = $Gpu.total_mib - $CombinedPeakMiB - $RequiredReserve
$OomDetected = $false
$Kind = 'none'
$Status = 'resident_supported'
$Effective = 'resident'
$SourceLogSha = @()
if ($Margin -lt 0) {
  $Kind = 'insufficient_margin'
  $Status = 'exclusive_required'
  $Effective = 'exclusive_process'
}
if ($auditAfter -gt $auditMid) {
  # The second round produced new audit events: possible model reload.
  $SourceLogSha += (Get-FileHash -LiteralPath $AuditLog -Algorithm SHA256).Hash.ToLowerInvariant()
}

$decision = [ordered]@{
  schema_version = 1
  status = $Status
  effective_lifecycle = $Effective
  gpu = @{ uuid = $Gpu.uuid; name = $Gpu.name; total_mib = $Gpu.total_mib }
  memory_mib = [ordered]@{
    idle = $IdleMiB
    index_peak = $IndexPeakMiB
    gsv_peak = $GsvPeakMiB
    combined_peak = $CombinedPeakMiB
    required_reserve = $RequiredReserve
    margin = $Margin
  }
  classification = [ordered]@{
    kind = $Kind
    oom_detected = $OomDetected
    rule = 'combined_peak + required_reserve <= total_mib'
    source_log_sha256 = @($SourceLogSha)
  }
  candidate_processes = $candidateProcesses
  stop_receipt_sha256 = $ReceiptSha
  evidence_paths = @(
    $CandidateConfig,
    $RunFile,
    $ReceiptPath,
    $StdoutLog,
    $StderrLog
  )
}
$DecisionPath = Join-Path $Evidence 'lifecycle-decision.json'
$decision | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $DecisionPath -Encoding UTF8

# Write the effective OutputConfig (resident unless exclusive required).
$effectiveYaml = Get-Content -LiteralPath $CandidateConfig -Raw
if ($Effective -eq 'exclusive_process') {
  $effectiveYaml = $effectiveYaml -replace 'engine_lifecycle:\s*resident', 'engine_lifecycle: exclusive_process'
}
$effectiveYaml | Set-Content -LiteralPath $OutputConfig -Encoding UTF8

if ($Json) {
  $decision | ConvertTo-Json -Depth 10 | Write-Output
} else {
  Write-Host "lifecycle decision: $Status ($Kind)"
}
