from __future__ import annotations

from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, Field, field_validator, model_validator

from voice_pipeline.models.schemas import NonBlankText, StrictModel

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ModelProfileStatus = Literal["ready", "missing", "corrupt", "archived"]


def _validate_relative_path(value: PurePosixPath) -> PurePosixPath:
    if value.is_absolute() or ".." in value.parts:
        raise ValueError("model profile paths must be relative and must not traverse parents")
    return value


RelativeModelPath = Annotated[PurePosixPath, AfterValidator(_validate_relative_path)]


class ImportModelProfileRequest(StrictModel):
    display_name: NonBlankText
    gpt_source_path: Path
    sovits_source_path: Path
    declared_family: str | None = None

    @field_validator("gpt_source_path", "sovits_source_path")
    @classmethod
    def source_path_must_be_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("model source paths must be absolute")
        return value

    @field_validator("gpt_source_path")
    @classmethod
    def gpt_source_suffix_must_be_ckpt(cls, value: Path) -> Path:
        if value.suffix.casefold() != ".ckpt":
            raise ValueError("GPT source path must end in .ckpt")
        return value

    @field_validator("sovits_source_path")
    @classmethod
    def sovits_source_suffix_must_be_pth(cls, value: Path) -> Path:
        if value.suffix.casefold() != ".pth":
            raise ValueError("SoVITS source path must end in .pth")
        return value

    @field_validator("declared_family")
    @classmethod
    def declared_family_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("declared_family must not be blank")
        return value.strip() if value is not None else None


class ModelProfileSnapshot(StrictModel):
    profile_id: UUID
    display_name: NonBlankText
    gpt_relative_path: RelativeModelPath
    sovits_relative_path: RelativeModelPath
    gpt_sha256: Sha256
    sovits_sha256: Sha256


class ModelProfileView(ModelProfileSnapshot):
    status: ModelProfileStatus
    created_at_utc: datetime
    active: bool
    declared_family: str | None = None

    @model_validator(mode="after")
    def archived_profile_cannot_be_active(self) -> ModelProfileView:
        if self.status == "archived" and self.active:
            raise ValueError("an archived model profile cannot be active")
        return self
