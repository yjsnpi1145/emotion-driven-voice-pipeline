from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from voice_pipeline.core.desktop_service import DesktopService
from voice_pipeline.models.desktop import OpenFolderRequest, PickFileRequest


@pytest.mark.asyncio
async def test_desktop_service_creates_managed_product_directories_before_open(
    tmp_path: Path,
) -> None:
    opened: list[Path] = []
    model_root = tmp_path / "managed" / "models"
    artifacts = tmp_path / "managed" / "artifacts"
    logs = tmp_path / "managed" / "logs"
    service = DesktopService(
        model_library=model_root,
        model_sources=(),
        artifacts=artifacts,
        logs=logs,
        profile_directory=None,
        opener=lambda path: opened.append(path),
    )

    await service.open_resource(OpenFolderRequest(resource="model_library"))
    await service.open_resource(OpenFolderRequest(resource="artifacts"))
    await service.open_resource(OpenFolderRequest(resource="logs"))

    assert opened == [model_root.resolve(), artifacts.resolve(), logs.resolve()]
    assert all(path.is_dir() for path in opened)


@pytest.mark.asyncio
async def test_desktop_service_opens_only_named_resources_and_profile_children(
    tmp_path: Path,
) -> None:
    model_root = tmp_path / "models"
    source_root = tmp_path / "sources"
    artifacts = tmp_path / "artifacts"
    logs = tmp_path / "logs"
    profile_dir = model_root / "profiles" / str(uuid4())
    for path in (model_root, source_root, artifacts, logs, profile_dir):
        path.mkdir(parents=True, exist_ok=True)
    opened: list[Path] = []

    async def resolve_profile(_profile_id):
        return profile_dir

    service = DesktopService(
        model_library=model_root,
        model_sources=(source_root,),
        artifacts=artifacts,
        logs=logs,
        profile_directory=resolve_profile,
        opener=lambda path: opened.append(path),
    )

    result = await service.open_resource(OpenFolderRequest(resource="model_library"))
    profile = await service.open_profile(uuid4())

    assert result.opened is True
    assert result.path == model_root.resolve()
    assert profile.path == profile_dir.resolve()
    assert opened == [model_root.resolve(), profile_dir.resolve()]
    paths = service.paths()
    assert paths.model_sources == (source_root.resolve(),)


@pytest.mark.asyncio
async def test_desktop_picker_validates_selected_extension(tmp_path: Path) -> None:
    root = tmp_path / "models"
    root.mkdir()
    wrong = tmp_path / "wrong.pth"
    wrong.write_bytes(b"x")
    service = DesktopService(
        model_library=root,
        model_sources=(root,),
        artifacts=root,
        logs=root,
        profile_directory=None,
        opener=lambda _path: None,
        picker=lambda _kind, _initial, _filters: wrong,
    )

    with pytest.raises(ValueError, match="selected file type"):
        await service.pick_file(PickFileRequest(kind="gpt_weight"))


@pytest.mark.asyncio
async def test_desktop_picker_cancellation_has_no_path(tmp_path: Path) -> None:
    root = tmp_path / "models"
    root.mkdir()
    service = DesktopService(
        model_library=root,
        model_sources=(root,),
        artifacts=root,
        logs=root,
        profile_directory=None,
        opener=lambda _path: None,
        picker=lambda _kind, _initial, _filters: None,
    )

    result = await service.pick_file(PickFileRequest(kind="base_voice"))

    assert result.selected is False
    assert result.path is None
