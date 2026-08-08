from __future__ import annotations

from uuid import UUID

from voice_pipeline.models.model_profiles import ImportModelProfileRequest, ModelProfileView
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

    async def activate_profile(self, profile_id: UUID) -> ModelProfileView:
        return await self._store.activate(profile_id)
