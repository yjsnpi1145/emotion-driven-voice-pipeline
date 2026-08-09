from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from uuid import UUID

from voice_pipeline.models.desktop import (
    DesktopResource,
    FilePickKind,
    LocalPathsView,
    OpenFolderRequest,
    OpenFolderResult,
    PickFileRequest,
    PickFileResult,
)

FolderOpener = Callable[[Path], None]
FilePicker = Callable[[FilePickKind, Path, Sequence[tuple[str, str]]], Path | None]
ProfileDirectoryResolver = Callable[[UUID], Awaitable[Path]]

_PICKERS: dict[FilePickKind, tuple[str, tuple[tuple[str, str], ...], str]] = {
    "gpt_weight": ("选择 GPT 权重", (("GPT checkpoint", "*.ckpt"),), ".ckpt"),
    "sovits_weight": ("选择 SoVITS 权重", (("SoVITS model", "*.pth"),), ".pth"),
    "base_voice": ("选择参考音频", (("WAV audio", "*.wav"),), ".wav"),
}


class DesktopService:
    """Constrained bridge from the loopback UI to native Windows affordances."""

    def __init__(
        self,
        *,
        model_library: Path,
        model_sources: Sequence[Path],
        artifacts: Path,
        logs: Path,
        profile_directory: ProfileDirectoryResolver | None,
        opener: FolderOpener | None = None,
        picker: FilePicker | None = None,
    ) -> None:
        self._model_library = model_library.resolve()
        self._model_sources = tuple(path.resolve() for path in model_sources)
        self._artifacts = artifacts.resolve()
        self._logs = logs.resolve()
        self._profile_directory = profile_directory
        self._opener = opener or _open_folder
        self._picker = picker or _pick_file

    def paths(self) -> LocalPathsView:
        return LocalPathsView(
            model_library=self._model_library,
            model_sources=self._model_sources,
            artifacts=self._artifacts,
            logs=self._logs,
        )

    async def open_resource(self, request: OpenFolderRequest) -> OpenFolderResult:
        path = self._resource_path(request.resource)
        if request.resource != "model_sources":
            await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
        await self._open_verified(path)
        return OpenFolderResult(path=path)

    async def open_profile(self, profile_id: UUID) -> OpenFolderResult:
        if self._profile_directory is None:
            raise KeyError(profile_id)
        raw_path = await self._profile_directory(profile_id)
        path = await asyncio.to_thread(_verified_profile_directory, raw_path, self._model_library)
        await self._open_verified(path)
        return OpenFolderResult(path=path)

    async def pick_file(self, request: PickFileRequest) -> PickFileResult:
        _title, filters, suffix = _PICKERS[request.kind]
        initial = self._initial_directory(request.kind)

        def choose() -> Path | None:
            return self._picker(request.kind, initial, filters)

        selected = await asyncio.to_thread(choose)
        if selected is None:
            return PickFileResult(selected=False)
        path = await asyncio.to_thread(_verified_file, selected)
        if path.suffix.casefold() != suffix:
            raise ValueError(f"selected file type must be {suffix}")
        return PickFileResult(selected=True, path=path)

    async def _open_verified(self, path: Path) -> None:
        verified = await asyncio.to_thread(_verified_directory, path)
        await asyncio.to_thread(self._opener, verified)

    def _resource_path(self, resource: DesktopResource) -> Path:
        if resource == "model_library":
            return self._model_library
        if resource == "model_sources":
            for path in self._model_sources:
                if path.is_dir():
                    return path
            raise FileNotFoundError("no configured model source directory exists")
        if resource == "artifacts":
            return self._artifacts
        return self._logs

    def _initial_directory(self, kind: FilePickKind) -> Path:
        if kind in {"gpt_weight", "sovits_weight"}:
            return next(
                (path for path in self._model_sources if path.is_dir()), self._model_library
            )
        return self._artifacts.parent


def _open_folder(path: Path) -> None:
    if os.name != "nt" or not hasattr(os, "startfile"):
        raise OSError("native folder opening is available on Windows only")
    os.startfile(str(path))


def _verified_directory(path: Path) -> Path:
    verified = path.resolve(strict=True)
    if not verified.is_dir() or verified.is_symlink():
        raise ValueError("desktop resource must be an existing regular directory")
    return verified


def _verified_profile_directory(path: Path, root: Path) -> Path:
    verified = _verified_directory(path)
    try:
        verified.relative_to(root)
    except ValueError as exc:
        raise ValueError("profile directory escapes the model library") from exc
    return verified


def _verified_file(path: Path) -> Path:
    verified = path.resolve(strict=True)
    if not verified.is_file() or verified.is_symlink():
        raise ValueError("selected path must be a regular file")
    return verified


def _pick_file(
    kind: FilePickKind, initial_directory: Path, filters: Sequence[tuple[str, str]]
) -> Path | None:
    import tkinter as tk
    from tkinter import filedialog

    title = _PICKERS[kind][0]
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askopenfilename(
            parent=root,
            title=title,
            initialdir=str(initial_directory),
            filetypes=list(filters) + [("所有文件", "*.*")],
        )
    finally:
        root.destroy()
    return Path(selected) if selected else None
