from __future__ import annotations

from pathlib import Path
from typing import Literal

from voice_pipeline.models.schemas import StrictModel

DesktopResource = Literal["model_library", "model_sources", "artifacts", "logs"]
FilePickKind = Literal["gpt_weight", "sovits_weight", "base_voice"]


class LocalPathsView(StrictModel):
    model_library: Path
    model_sources: tuple[Path, ...]
    artifacts: Path
    logs: Path


class OpenFolderRequest(StrictModel):
    resource: DesktopResource


class OpenFolderResult(StrictModel):
    opened: Literal[True] = True
    path: Path


class PickFileRequest(StrictModel):
    kind: FilePickKind


class PickFileResult(StrictModel):
    selected: bool
    path: Path | None = None
