from __future__ import annotations

from pathlib import Path
from uuid import UUID

from voice_pipeline.models.model_profiles import (
    ImportModelProfileRequest,
    ModelProfileView,
    ResolvedModelProfile,
)
from voice_pipeline.storage.model_importer import ModelProfileImporter
from voice_pipeline.storage.model_profile_store import SqliteModelProfileStore


class ModelProfileService:
    def __init__(self, *, importer: ModelProfileImporter, store: SqliteModelProfileStore) -> None:
        self._importer = importer
        self._store = store

    async def import_profile(self, request: ImportModelProfileRequest) -> ModelProfileView:
        record = await self._importer.import_pair(request)
        await self._store.insert_published(record)
        return await self._store.get_view(record.profile_id)

    async def list_profiles(self) -> list[ModelProfileView]:
        return await self._store.list()

    async def get_profile(self, profile_id: UUID) -> ModelProfileView:
        return await self._store.get_view(profile_id)

    async def profile_directory(self, profile_id: UUID) -> Path:
        return await self._store.profile_directory(profile_id)

    async def activate_profile(self, profile_id: UUID) -> ModelProfileView:
        return await self._store.activate(profile_id)

    async def resolve_selected_profile(self, profile_id: UUID | None) -> ResolvedModelProfile:
        return await self._store.resolve_selected_profile(profile_id)
