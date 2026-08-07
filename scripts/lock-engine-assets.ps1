#requires -Version 7
<#
.SYNOPSIS
  Generate or verify the tracked config/checkpoints.lock.yaml SHA-256 allowlist
  for the real model assets used by IndexTTS2 and GPT-SoVITS.
#>
[CmdletBinding()]
param(
  [switch]$WriteInitialLock
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$RepoRoot = 'D:\TTSsystem'
$LockPath = Join-Path $RepoRoot 'config\checkpoints.lock.yaml'
$EngineLock = Join-Path $RepoRoot 'config\engines.lock.yaml'

function Get-Sha256OfFile {
  param([string]$Path)
  return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLower()
}

function Add-Assets {
  param([string]$Root, [string]$Engine, [System.Collections.Generic.List[object]]$Assets)
  if (-not (Test-Path -LiteralPath $Root)) { return }
  Get-ChildItem -LiteralPath $Root -Recurse -File -Force | ForEach-Object {
    $relative = $_.FullName.Substring($Root.Length).TrimStart('\', '/').Replace('\', '/')
    $obj = [pscustomobject]@{
      path = $relative
      size = $_.Length
      sha256 = (Get-Sha256OfFile $_.FullName)
    }
    $Assets.Add($obj)
  }
}

$Assets = [System.Collections.Generic.List[object]]::new()

# Index assets actually used by config.yaml + the four explicit aux_paths.
$IndexRepo = Join-Path $RepoRoot 'external\index-tts'
$IndexCheckpoints = Join-Path $IndexRepo 'checkpoints'
if (Test-Path -LiteralPath (Join-Path $IndexCheckpoints 'config.yaml')) {
  Add-Assets -Root $IndexCheckpoints -Engine 'indextts' -Assets $Assets
}

# GSV assets actually used by tts_infer.yaml + v2 weights.
$GsvRepo = Join-Path $RepoRoot 'external\GPT-SoVITS'
$GsvModels = Join-Path $GsvRepo 'GPT_SoVITS\pretrained_models'
if (Test-Path -LiteralPath $GsvModels) {
  Add-Assets -Root $GsvModels -Engine 'gpt_sovits' -Assets $Assets
}

if ($Assets.Count -eq 0) {
  Write-Error 'no model assets found; cannot generate an empty checkpoint lock'
  exit 2
}

$EngineLockRaw = Get-Content -LiteralPath $EngineLock -Raw
$Payload = [ordered]@{
  schema_version = 1
  index_source_revision = '90ca4d608209584bad3a5bd5becc0b80c146e60f'
  index_model_revision = '740dcaff396282ffb241903d150ac011cd4b1ede'
  gsv_source_revision = 'd523079fc05d9a8028d6085bffe4a2757c32abb6'
  gsv_pretrained_revision = '4fae8ec36d3d0373864e580b5d8acfba8da29630'
  gsv_archive_sha256 = '82881ee064a0a49c84160908fd08e4dd0c8946e32567ff8df1ad4dad4c358793'
  assets = @($Assets | Sort-Object path)
}

if ($WriteInitialLock) {
  if (Test-Path -LiteralPath $LockPath -PathType Leaf) {
    Write-Error 'checkpoints.lock.yaml already exists; refusing to overwrite'
    exit 1
  }
  $Payload | ConvertTo-Yaml | Set-Content -LiteralPath $LockPath -Encoding UTF8
  Write-Host "wrote $LockPath ($($Assets.Count) assets)"
  exit 0
}

# Verify mode: the lock must exist and match every asset.
if (-not (Test-Path -LiteralPath $LockPath -PathType Leaf)) {
  Write-Error "missing checkpoint lock: $LockPath (run with -WriteInitialLock after assets are present)"
  exit 1
}
$Existing = Get-Content -LiteralPath $LockPath -Raw | ConvertFrom-Yaml
$Failures = @()
foreach ($asset in $Assets) {
  $Match = $Existing.assets | Where-Object { $_.path -eq $asset.path }
  if (-not $Match) { $Failures += "untracked asset: $($asset.path)" }
  elseif ([long]$Match.size -ne [long]$asset.size -or $Match.sha256 -ne $asset.sha256) {
    $Failures += "asset mismatch: $($asset.path)"
  }
}
foreach ($tracked in $Existing.assets) {
  if (-not ($Assets | Where-Object { $_.path -eq $tracked.path })) {
    $Failures += "missing asset: $($tracked.path)"
  }
}
if ($Failures.Count -gt 0) {
  $Failures | ForEach-Object { Write-Error $_ }
  exit 1
}
Write-Host "checkpoint lock verified ($($Assets.Count) assets)"
exit 0
