[CmdletBinding()]
param(
    [string]$Root,
    [switch]$Offline,
    [switch]$AcceptModelLicenses
)

$ErrorActionPreference = 'Stop'
$Root = if ($Root) { $Root } else { Join-Path $PSScriptRoot '..' }
$Root = (Resolve-Path -LiteralPath $Root).Path
$LockPath = Join-Path $Root 'config\quality-model.lock.yaml'
$Destination = Join-Path $Root 'runtime\models\faster-whisper-small'

if (-not (Test-Path -LiteralPath $LockPath -PathType Leaf)) {
    throw "Tracked quality model lock is missing: $LockPath"
}

$env:VOICE_PIPELINE_QUALITY_LOCK = $LockPath
$env:VOICE_PIPELINE_QUALITY_DESTINATION = $Destination
$env:VOICE_PIPELINE_QUALITY_OFFLINE = if ($Offline) { '1' } else { '0' }
$env:VOICE_PIPELINE_ACCEPT_MODEL_LICENSES = if ($AcceptModelLicenses) { '1' } else { '0' }

@'
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import yaml

from huggingface_hub import snapshot_download


lock_path = Path(os.environ["VOICE_PIPELINE_QUALITY_LOCK"])
destination = Path(os.environ["VOICE_PIPELINE_QUALITY_DESTINATION"])
offline = os.environ["VOICE_PIPELINE_QUALITY_OFFLINE"] == "1"
accepted = os.environ["VOICE_PIPELINE_ACCEPT_MODEL_LICENSES"] == "1"
lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
if lock.get("schema_version") != 1 or not isinstance(lock.get("files"), list):
    raise SystemExit("quality model lock has an invalid schema")


def verify() -> bool:
    for item in lock["files"]:
        relative = Path(item["path"])
        path = (destination / relative).resolve()
        try:
            path.relative_to(destination.resolve())
        except ValueError as exc:
            raise SystemExit(f"invalid locked relative path: {relative}") from exc
        if not path.is_file() or path.is_symlink() or path.stat().st_size != int(item["size"]):
            return False
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item["sha256"]:
            return False
    return True


if not verify():
    if offline:
        raise SystemExit("pinned faster-whisper model asset is missing or does not match lock")
    if not accepted:
        raise SystemExit(
            "model download requires -AcceptModelLicenses; read MODEL_LICENSES.md first"
        )
    destination.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=lock["repository"],
        revision=lock["revision"],
        local_dir=str(destination),
        local_files_only=False,
    )
if not verify():
    raise SystemExit("downloaded faster-whisper model does not match tracked lock")
print(f"quality-model-verified {destination}")
'@ | & uv run --project $Root python -

if ($LASTEXITCODE -ne 0) {
    throw "quality model setup failed with exit code $LASTEXITCODE"
}
