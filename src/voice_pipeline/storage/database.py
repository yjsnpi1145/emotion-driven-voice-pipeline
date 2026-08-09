from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import portalocker
from alembic import command
from alembic.config import Config
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from voice_pipeline.core.config import StorageSettings
from voice_pipeline.core.errors import ErrorCode, PipelineError

PACKAGED_HEAD = "0003_chapter_history_soft_delete"


class ControlInstanceLock:
    def __init__(self, path: Path, *, instance_id: UUID, database_path: Path) -> None:
        self._path = path
        self._instance_id = instance_id
        self._database_path = database_path
        self._lock: portalocker.Lock | None = None
        self._handle: object | None = None

    @property
    def owner_path(self) -> Path:
        return self._path.with_name("control-lock-owner.json")

    async def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = portalocker.Lock(
            str(self._path),
            mode="a+",
            timeout=0,
            flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
        )
        try:
            self._handle = await asyncio.to_thread(self._lock.acquire)
        except portalocker.exceptions.LockException as exc:
            raise PipelineError(
                ErrorCode.CONTROL_INSTANCE_CONFLICT,
                "storage",
                "another control instance owns the runtime lock",
                retryable=False,
            ) from exc
        await asyncio.to_thread(self._write_owner)

    async def release(self) -> None:
        if self._lock is None:
            return
        await asyncio.to_thread(self._lock.release)
        self._lock = None
        self._handle = None
        await asyncio.to_thread(self._delete_owned_owner_file)

    def _write_owner(self) -> None:
        payload = {
            "schema_version": 1,
            "instance_id": str(self._instance_id),
            "pid": os.getpid(),
            "create_time": _process_create_time(),
            "database_path": str(self._database_path),
        }
        partial = self.owner_path.with_suffix(".partial")
        with open(partial, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, self.owner_path)

    def _delete_owned_owner_file(self) -> None:
        try:
            payload = json.loads(self.owner_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        if payload.get("instance_id") == str(self._instance_id):
            self.owner_path.unlink(missing_ok=True)


class Database:
    def __init__(
        self,
        *,
        settings: StorageSettings,
        engine: AsyncEngine,
        instance_lock: ControlInstanceLock,
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._instance_lock = instance_lock
        self._write_lock = asyncio.Lock()

    @classmethod
    async def open(
        cls,
        settings: StorageSettings,
        *,
        instance_id: UUID,
        migrate: bool,
    ) -> Database:
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        settings.artifact_root.mkdir(parents=True, exist_ok=True)
        instance_lock = ControlInstanceLock(
            settings.control_lock_path,
            instance_id=instance_id,
            database_path=settings.database_path,
        )
        await instance_lock.acquire()
        try:
            if migrate:
                await asyncio.to_thread(_migrate, settings.database_path)
            await asyncio.to_thread(_prepare_sqlite, settings)
            engine = create_async_engine(f"sqlite+aiosqlite:///{settings.database_path.as_posix()}")
            _install_pragmas(engine, settings)
            database = cls(settings=settings, engine=engine, instance_lock=instance_lock)
            await database.quick_check()
            return database
        except BaseException:
            await instance_lock.release()
            raise

    @asynccontextmanager
    async def read_session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessions() as session:
            yield session

    @asynccontextmanager
    async def write_session(self) -> AsyncIterator[AsyncSession]:
        async with self._write_lock:
            async with self._sessions() as session:
                try:
                    async with session.begin():
                        yield session
                except OperationalError as exc:
                    if _is_busy(exc):
                        raise PipelineError(
                            ErrorCode.DATABASE_BUSY,
                            "storage",
                            "SQLite database is busy",
                            retryable=True,
                        ) from exc
                    raise

    async def scalar_text(self, statement: str) -> str:
        async with self.read_session() as session:
            value = (await session.execute(text(statement))).scalar_one()
        return str(value)

    async def scalar_int(self, statement: str) -> int:
        async with self.read_session() as session:
            value = (await session.execute(text(statement))).scalar_one()
        return int(value)

    async def quick_check(self) -> None:
        if await self.quick_check_text() != "ok":
            raise PipelineError(
                ErrorCode.DATABASE_INTEGRITY_FAILED,
                "storage",
                "SQLite quick_check failed",
                retryable=False,
            )

    async def quick_check_text(self) -> str:
        return await self.scalar_text("PRAGMA quick_check")

    async def alembic_revision(self) -> str:
        return await self.scalar_text("SELECT version_num FROM alembic_version")

    async def close(self) -> None:
        await self._engine.dispose()
        await self._instance_lock.release()


def _migrate(database_path: Path) -> None:
    config = Config()
    migration_root = Path(__file__).with_name("migrations")
    config.set_main_option("script_location", str(migration_root))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    command.upgrade(config, "head")


def _prepare_sqlite(settings: StorageSettings) -> None:
    connection = sqlite3.connect(settings.database_path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(f"PRAGMA busy_timeout={settings.busy_timeout_ms}")
        connection.execute(f"PRAGMA wal_autocheckpoint={settings.wal_autocheckpoint_pages}")
        connection.commit()
    finally:
        connection.close()


def _install_pragmas(engine: AsyncEngine, settings: StorageSettings) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def on_connect(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute(f"PRAGMA busy_timeout={settings.busy_timeout_ms}")
            cursor.execute(f"PRAGMA wal_autocheckpoint={settings.wal_autocheckpoint_pages}")
        finally:
            cursor.close()


def _is_busy(exc: OperationalError) -> bool:
    message = str(exc).casefold()
    return "database is locked" in message or "database is busy" in message


def _process_create_time() -> float:
    if sys.platform == "win32":
        import psutil

        return psutil.Process().create_time()
    return datetime.now(UTC).timestamp()
