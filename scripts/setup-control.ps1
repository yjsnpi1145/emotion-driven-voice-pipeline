#requires -Version 7
<#
.SYNOPSIS
  Idempotent setup of the control-plane Python 3.11 environment and the
  tracked control-runtime environment locks.
#>
[CmdletBinding()]
param(
  [switch]$WriteInitialEnvLocks
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$RepoRoot = 'D:\TTSsystem'
Set-Location $RepoRoot

function Invoke-Checked {
  param([Parameter(Mandatory)][string]$FilePath, [Parameter(Mandatory)][string[]]$ArgumentList)
  & $FilePath @ArgumentList
  if ($LASTEXITCODE -ne 0) { throw "$FilePath exited $LASTEXITCODE" }
}

$EnvLockDir = Join-Path $RepoRoot 'config\env-locks'
New-Item -ItemType Directory -Force -Path $EnvLockDir | Out-Null
$RuntimeLock = Join-Path $EnvLockDir 'control-runtime-requirements.lock.txt'
$RuntimeFreeze = Join-Path $EnvLockDir 'control-runtime-freeze.txt'
$ControlPython = Join-Path $RepoRoot '.venv-control\Scripts\python.exe'
$ControlWheelDir = Join-Path $RepoRoot 'runtime\control-wheel'

# 1. Python 3.11
Invoke-Checked 'uv' @('python', 'install', '3.11')
Invoke-Checked 'uv' @('sync', '--frozen', '--extra', 'dev', '--python', '3.11')
uv run python -c "import sys; assert sys.version_info[:2] == (3, 11); print(sys.executable)"

# 2. Control venv
if (-not (Test-Path -LiteralPath $ControlPython -PathType Leaf)) {
  Invoke-Checked 'uv' @('venv', (Join-Path $RepoRoot '.venv-control'), '--python', '3.11')
}

# 3. Initial env locks (only once, only with the explicit flag)
if ($WriteInitialEnvLocks) {
  if (-not (Test-Path -LiteralPath $RuntimeLock -PathType Leaf)) {
    uv export --frozen --no-dev --no-emit-project --format requirements.txt `
      -o $RuntimeLock
    if ($LASTEXITCODE -ne 0) { throw 'uv export failed' }
  }
  if (-not (Test-Path -LiteralPath $RuntimeFreeze -PathType Leaf)) {
    $Sample = Join-Path $env:TEMP 'voice-pipeline-control-freeze'
    Invoke-Checked 'uv' @('venv', $Sample, '--python', '3.11')
    Invoke-Checked 'uv' @(
      'pip', 'sync', '--python', (Join-Path $Sample 'Scripts\python.exe'),
      '--require-hashes', $RuntimeLock
    )
    & (Join-Path $Sample 'Scripts\python.exe') `
      (Join-Path $RepoRoot 'scripts\export-environment-inventory.py') `
      --output $RuntimeFreeze
    if ($LASTEXITCODE -ne 0) { throw 'inventory export failed' }
  }
}

# 4. Install the runtime profile into the control venv
if (-not (Test-Path -LiteralPath $RuntimeLock -PathType Leaf)) {
  throw "missing tracked runtime lock: $RuntimeLock"
}
Invoke-Checked 'uv' @(
  'pip', 'sync', '--python', $ControlPython, '--require-hashes', $RuntimeLock
)

# 5. Build and install the project wheel (no deps)
uv build --wheel --out-dir $ControlWheelDir
if ($LASTEXITCODE -ne 0) { throw 'wheel build failed' }
$ControlWheel = (Get-ChildItem (Join-Path $ControlWheelDir '*.whl') | Select-Object -First 1).FullName
if (-not $ControlWheel) { throw 'control wheel missing' }
Invoke-Checked 'uv' @(
  'pip', 'install', '--python', $ControlPython, '--no-deps', '--force-reinstall', $ControlWheel
)

Write-Host "control setup complete: $ControlPython"
