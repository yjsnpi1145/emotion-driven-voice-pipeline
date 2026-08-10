#requires -Version 7
<#
.SYNOPSIS
  Idempotent setup of the GPT-SoVITS v2 conda + pip environment pinned to
  engines.lock.yaml revisions.  "Create if absent, otherwise verify."
#>
[CmdletBinding()]
param(
  [switch]$WriteInitialEnvLocks,
  [switch]$VerifyDisposableRebuild,
  [switch]$DownloadModels,
  [switch]$AcceptModelLicenses
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location $RepoRoot

function Invoke-Checked {
  param([Parameter(Mandatory)][string]$FilePath, [Parameter(Mandatory)][string[]]$ArgumentList)
  & $FilePath @ArgumentList
  if ($LASTEXITCODE -ne 0) { throw "$FilePath exited $LASTEXITCODE" }
}

$GsvRepo = Join-Path $RepoRoot 'external\GPT-SoVITS'
$GsvPython = Join-Path $GsvRepo '.conda\python.exe'
$EnvLockDir = Join-Path $RepoRoot 'config\env-locks'
$CondaExplicit = Join-Path $EnvLockDir 'gsv-conda-explicit.txt'
$PipLock = Join-Path $EnvLockDir 'gsv-pip-requirements.lock.txt'
$PipFreeze = Join-Path $EnvLockDir 'gsv-pip-freeze.txt'
$Downloads = Join-Path $RepoRoot 'runtime\downloads\gsv'
$PinnedCommit = 'd523079fc05d9a8028d6085bffe4a2757c32abb6'
$PinnedArchiveSha = '82881ee064a0a49c84160908fd08e4dd0c8946e32567ff8df1ad4dad4c358793'

# 0. conda prerequisite (never silently installed by this script).
$CondaExe = (Get-Command conda -ErrorAction SilentlyContinue)
if (-not $CondaExe) {
  Write-Error 'conda is required. Install Miniforge with:'
  Write-Error '  winget install -e --id CondaForge.Miniforge3 --scope user'
  exit 1
}
$CondaExe = $CondaExe.Source

# 1. Repository: clone if absent, otherwise verify remote/HEAD/dirtiness.
if (-not (Test-Path (Join-Path $GsvRepo '.git'))) {
  New-Item -ItemType Directory -Force -Path (Split-Path $GsvRepo) | Out-Null
  Invoke-Checked 'git' @('-c','http.proxy=','-c','https.proxy=','clone','https://github.com/RVC-Boss/GPT-SoVITS.git',$GsvRepo)
}
$Head = (git -C $GsvRepo rev-parse HEAD).Trim()
if ($Head -ne $PinnedCommit) {
  Invoke-Checked 'git' @('-C', $GsvRepo, '-c','http.proxy=','-c','https.proxy=','checkout', $PinnedCommit)
}
$AllowedRuntimePaths = @(
  'GPT_SoVITS/configs/tts_infer.yaml',
  'GPT_SoVITS/text/ja_userdic/user.dict',
  'GPT_SoVITS/text/ja_userdic/userdict.md5'
)
$Dirty = git -C $GsvRepo status --porcelain |
  Where-Object {
    $StatusPath = $_.Substring(3).Replace('\', '/')
    $StatusPath -notmatch '^\.conda(/|$)' -and $StatusPath -notin $AllowedRuntimePaths
  }
if ($Dirty) { throw "gsv repo is dirty`n$Dirty" }

# 2. Initial env locks (explicit flag only).
if ($WriteInitialEnvLocks) {
  if (-not (Test-Path -LiteralPath $CondaExplicit -PathType Leaf)) {
    throw 'gsv-conda-explicit.txt must be produced from a real conda environment'
  }
  if (-not (Test-Path -LiteralPath $PipLock -PathType Leaf)) {
    $Bootstrap = Join-Path $RepoRoot 'runtime\gsv-lock-bootstrap'
    New-Item -ItemType Directory -Force -Path $Bootstrap | Out-Null
    $reqs = Get-Content (Join-Path $GsvRepo 'requirements.txt') | Where-Object { $_ -notmatch '^--no-binary' }
    $extra = Get-Content (Join-Path $GsvRepo 'extra-req.txt')
    @($reqs + $extra + @(
      'torch==2.7.0+cu128',
      'torchaudio==2.7.0+cu128',
      'torchcodec==0.4.0; sys_platform != "win32"'
    )) | Set-Content (Join-Path $Bootstrap 'gsv-requirements.in') -Encoding UTF8
    uv pip compile (Join-Path $Bootstrap 'gsv-requirements.in') `
      --generate-hashes --output-file $PipLock --python-version 3.11 `
      --index-url https://pypi.tuna.tsinghua.edu.cn/simple `
      --extra-index-url https://download.pytorch.org/whl/cu128 `
      --index-strategy unsafe-best-match
    if ($LASTEXITCODE -ne 0) { throw 'gsv pip compile failed' }
  }
  if (-not (Test-Path -LiteralPath $PipFreeze -PathType Leaf)) {
    throw 'gsv-pip-freeze.txt must be produced from the installed environment'
  }
}

# 3. Conda environment + pip sync.  An interrupted earlier setup can leave
# the conda interpreter present without the lock contents, so always reconcile it.
if (-not (Test-Path -LiteralPath $CondaExplicit -PathType Leaf)) {
  throw "missing tracked gsv conda lock: $CondaExplicit"
}
if (-not (Test-Path -LiteralPath $PipLock -PathType Leaf)) {
  throw "missing tracked gsv pip lock: $PipLock"
}
$NeedsPipSync = $true
if (-not (Test-Path -LiteralPath $GsvPython -PathType Leaf)) {
  & $CondaExe create -y --prefix (Join-Path $GsvRepo '.conda') --file $CondaExplicit
  if ($LASTEXITCODE -ne 0) { throw 'conda create failed' }
}
if ($NeedsPipSync) {
  Invoke-Checked 'uv' @(
    'pip', 'sync', '--python', $GsvPython, '--require-hashes', $PipLock,
    '--extra-index-url','https://download.pytorch.org/whl/cu128',
    '--index-strategy','unsafe-best-match'
  )
}

# 4. Verify interpreter is 3.11.
& $GsvPython -c "import sys; assert sys.version_info[:2] == (3, 11); print(sys.executable)"
if ($LASTEXITCODE -ne 0) { throw 'gsv python version check failed' }

# 5. Pretrained assets (pinned revision, SHA verified, explicit license acceptance).
if ($DownloadModels -and -not $AcceptModelLicenses) {
  throw 'Model download requires -AcceptModelLicenses. Read MODEL_LICENSES.md first.'
}
if ($DownloadModels) {
  New-Item -ItemType Directory -Force -Path $Downloads | Out-Null
  uv tool run --from "huggingface-hub[cli,hf_xet]" hf download `
    XXXXRT/GPT-SoVITS-Pretrained `
    --revision 4fae8ec36d3d0373864e580b5d8acfba8da29630 `
    --local-dir $Downloads
  if ($LASTEXITCODE -ne 0) { throw 'pretrained download failed' }
  $Archive = Join-Path $Downloads 'pretrained_models.zip'
  if (-not (Test-Path -LiteralPath $Archive -PathType Leaf)) {
    throw "pretrained archive is missing after download: $Archive"
  }
  $ActualSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $Archive).Hash.ToLower()
  if ($ActualSha -ne $PinnedArchiveSha) {
    throw "pretrained_models.zip SHA mismatch: $ActualSha"
  }

  $Staging = Join-Path $Downloads 'pretrained-extracted'
  if (Test-Path -LiteralPath $Staging) {
    Remove-Item -LiteralPath $Staging -Recurse -Force
  }
  New-Item -ItemType Directory -Force -Path $Staging | Out-Null
  Expand-Archive -LiteralPath $Archive -DestinationPath $Staging -Force

  $ExtractedRoot = $Staging
  foreach ($Candidate in @(
    (Join-Path $Staging 'pretrained_models'),
    (Join-Path $Staging 'GPT_SoVITS\pretrained_models')
  )) {
    if (Test-Path -LiteralPath $Candidate -PathType Container) {
      $ExtractedRoot = $Candidate
      break
    }
  }
  $PretrainedModels = Join-Path $GsvRepo 'GPT_SoVITS\pretrained_models'
  New-Item -ItemType Directory -Force -Path $PretrainedModels | Out-Null
  Get-ChildItem -LiteralPath $ExtractedRoot -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $PretrainedModels -Recurse -Force
  }
  Remove-Item -LiteralPath $Staging -Recurse -Force
}

# 6. GPU sanity (only meaningful on a CUDA machine).
& $GsvPython -c "import torch; assert torch.cuda.is_available(); print(torch.__version__, torch.version.cuda)"
if ($LASTEXITCODE -ne 0) { Write-Warning 'CUDA not available in gsv environment (expected on GPU hosts only)' }

Write-Host "gsv setup complete: $GsvPython"
