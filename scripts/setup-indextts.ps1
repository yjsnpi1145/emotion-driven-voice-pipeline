#requires -Version 7
<#
.SYNOPSIS
  Idempotent setup of the IndexTTS2 worker environment pinned to
  engines.lock.yaml revisions.  "Create if absent, otherwise verify."
#>
[CmdletBinding()]
param(
  [switch]$WriteInitialEnvLocks,
  [switch]$VerifyDisposableRebuild
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

$IndexRepo = Join-Path $RepoRoot 'external\index-tts'
$IndexPython = Join-Path $IndexRepo '.venv\Scripts\python.exe'
$Checkpoints = Join-Path $IndexRepo 'checkpoints'
$EnvLockDir = Join-Path $RepoRoot 'config\env-locks'
$IndexLock = Join-Path $EnvLockDir 'index-pip-requirements.lock.txt'
$IndexFreeze = Join-Path $EnvLockDir 'index-pip-freeze.txt'
$BootstrapWheelDir = Join-Path $RepoRoot 'runtime\bootstrap-wheel'
$PinnedCommit = '90ca4d608209584bad3a5bd5becc0b80c146e60f'

# 1. Repository: clone if absent, otherwise verify remote/HEAD/dirtiness.
if (-not (Test-Path (Join-Path $IndexRepo '.git'))) {
  New-Item -ItemType Directory -Force -Path (Split-Path $IndexRepo) | Out-Null
  Invoke-Checked 'git' @('-c','http.proxy=','-c','https.proxy=','clone','https://github.com/index-tts/index-tts.git',$IndexRepo)
}
$Head = (git -C $IndexRepo rev-parse HEAD).Trim()
if ($Head -ne $PinnedCommit) {
  Invoke-Checked 'git' @('-C', $IndexRepo, '-c','http.proxy=','-c','https.proxy=','checkout', $PinnedCommit)
}
$Dirty = git -C $IndexRepo status --porcelain |
  Where-Object { $_ -notmatch '^\?\? checkpoints([/\\]|$)' }
if ($Dirty) { throw "index repo is dirty`n$Dirty" }

# 2. Initial env locks (explicit flag only).
if ($WriteInitialEnvLocks) {
  $Bootstrap = Join-Path $RepoRoot 'runtime\index-lock-bootstrap'
  New-Item -ItemType Directory -Force -Path $Bootstrap | Out-Null
  if (-not (Test-Path -LiteralPath $IndexLock -PathType Leaf)) {
    uv export --project $Bootstrap --frozen --no-dev --no-emit-project `
      --format requirements.txt -o (Join-Path $Bootstrap 'requirements-base.txt')
    if ($LASTEXITCODE -ne 0) { throw 'uv export failed' }
    Get-Content (Join-Path $RepoRoot 'workers\indextts2\requirements.txt') `
      | Add-Content (Join-Path $Bootstrap 'requirements-base.txt')
    uv pip compile (Join-Path $Bootstrap 'requirements-base.txt') `
      --generate-hashes --output-file $IndexLock --python-version 3.11 `
      --index-url https://pypi.tuna.tsinghua.edu.cn/simple `
      --extra-index-url https://download.pytorch.org/whl/cu128 `
      --index-strategy unsafe-best-match
    if ($LASTEXITCODE -ne 0) { throw 'index pip compile failed' }
  }
  if (-not (Test-Path -LiteralPath $IndexFreeze -PathType Leaf)) {
    throw 'index-pip-freeze.txt must be produced from the installed environment'
  }
}

# 3. Worker venv + locked deps.
if (-not (Test-Path -LiteralPath $IndexPython -PathType Leaf)) {
  if (-not (Test-Path -LiteralPath $IndexLock -PathType Leaf)) {
    throw "missing tracked index lock: $IndexLock"
  }
  Invoke-Checked 'uv' @('venv', (Join-Path $IndexRepo '.venv'), '--python', '3.11', '--seed')
  Invoke-Checked 'uv' @(
    'pip', 'sync', '--python', $IndexPython, '--require-hashes', $IndexLock
  )
  uv build --wheel --out-dir $BootstrapWheelDir
  if ($LASTEXITCODE -ne 0) { throw 'wheel build failed' }
  $ProjectWheel = (Get-ChildItem (Join-Path $BootstrapWheelDir '*.whl') | Select-Object -First 1).FullName
  Invoke-Checked 'uv' @(
    'pip', 'install', '--python', $IndexPython, '--no-deps', '--force-reinstall', $ProjectWheel
  )
}

# 4. Verify the interpreter is 3.11.
& $IndexPython -c "import sys; assert sys.version_info[:2] == (3, 11); print(sys.executable)"
if ($LASTEXITCODE -ne 0) { throw 'index python version check failed' }

# 5. Model downloads (pinned revisions only).
if ($WriteInitialEnvLocks) {
  $PinnedRoot = Join-Path $Checkpoints 'hf_cache\pinned'
  New-Item -ItemType Directory -Force -Path $PinnedRoot | Out-Null
  uv tool run --from "huggingface-hub[cli,hf_xet]" hf download `
    IndexTeam/IndexTTS-2 --revision 740dcaff396282ffb241903d150ac011cd4b1ede `
    --local-dir $Checkpoints
  uv tool run --from "huggingface-hub[cli,hf_xet]" hf download `
    facebook/w2v-bert-2.0 --revision da985ba0987f70aaeb84a80f2851cfac8c697a7b `
    --local-dir (Join-Path $PinnedRoot 'w2v_bert')
  uv tool run --from "huggingface-hub[cli,hf_xet]" hf download `
    amphion/MaskGCT semantic_codec/model.safetensors `
    --revision b9ccc6487b9f486b5b4c22c93010e0b54ddce2e2 `
    --local-dir (Join-Path $PinnedRoot 'maskgct')
  uv tool run --from "huggingface-hub[cli,hf_xet]" hf download `
    funasr/campplus campplus_cn_common.bin `
    --revision e4b6ede7ce16997aff4ae69fbca1f0175e2afede `
    --local-dir (Join-Path $PinnedRoot 'campplus')
  uv tool run --from "huggingface-hub[cli,hf_xet]" hf download `
    nvidia/bigvgan_v2_22khz_80band_256x config.json bigvgan_generator.pt `
    --revision d7b6990ac772ed0ebd93f814912b0027629a7978 `
    --local-dir (Join-Path $PinnedRoot 'bigvgan')
}

Write-Host "index setup complete: $IndexPython"
