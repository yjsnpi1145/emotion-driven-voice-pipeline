# Batch 2 open-source reuse

Batch 2 reuses SQLAlchemy, Alembic, aiosqlite and portalocker for durable local state rather than implementing an ORM, migration runner, async SQLite bridge or Windows file locking. It reuses faster-whisper, Silero VAD and RapidFuzz for quality analysis instead of implementing ASR, VAD or alignment algorithms.

Project code remains a thin layer only where the product needs semantics external packages do not provide: immutable file/SQLite publication, job and version snapshots, optimistic activation, reference dependency protection, canonical cache payloads and retention decisions. The GPT-SoVITS model profile feature does not copy inference code: it calls the official `/set_gpt_weights`, `/set_sovits_weights` and `/tts` endpoints under the existing single-GPU lease.

SQLModel is deliberately not used because public immutable Pydantic DTOs and mutable migration-managed ORM rows have different lifecycles. DiskCache is deliberately not used because its separate SQLite state cannot atomically protect the same current-version and parent-reference graph as artifact versions.
