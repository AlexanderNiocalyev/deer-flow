from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.runtime_binding.model import RuntimeBindingRow

logger = logging.getLogger(__name__)

_PROVIDER = "vercel"


class VercelSandboxRecordStore(Protocol):
    def load(self, sandbox_id: str) -> dict[str, Any] | None:
        """Load a serialized Vercel sandbox record."""

    async def aload(self, sandbox_id: str) -> dict[str, Any] | None:
        """Async load variant used by async tool execution paths."""

    def save(self, record: dict[str, Any]) -> None:
        """Persist a serialized Vercel sandbox record."""

    async def asave(self, record: dict[str, Any]) -> None:
        """Async save variant used by async tool execution paths."""

    def delete(self, record: dict[str, Any]) -> None:
        """Delete a serialized Vercel sandbox record."""

    async def adelete(self, record: dict[str, Any]) -> None:
        """Async delete variant used by async tool execution paths."""

    def try_claim_create(self, record: dict[str, Any]) -> bool:
        """Claim ownership of creating a provider sandbox for this record."""

    async def atry_claim_create(self, record: dict[str, Any]) -> bool:
        """Async claim variant used by async tool execution paths."""


def _run_async_sync[T](factory: Callable[[], Any]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    result: dict[str, Any] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(factory())
        except BaseException as exc:  # pragma: no cover - defensive handoff
            result["error"] = exc

    thread = threading.Thread(target=runner, name="vercel-record-store", daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _record_id(sandbox_id: str) -> str:
    return f"{_PROVIDER}:{sandbox_id}"


def _ts_to_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), UTC)


def _dt_to_ts(value: datetime | None) -> float | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()


class FileVercelSandboxRecordStore:
    def __init__(self, record_path: Callable[[str, str | None, str], Path]) -> None:
        self._record_path = record_path

    def _path_for_record(self, record: dict[str, Any]) -> Path | None:
        thread_id = record.get("thread_id")
        if thread_id is None:
            return None
        return self._record_path(str(thread_id), record.get("user_id"), str(record["sandbox_id"]))

    def load(self, sandbox_id: str) -> dict[str, Any] | None:
        # File-backed records are scoped by thread/user, so the provider keeps
        # using ``load_for_thread`` for this store.
        return None

    async def aload(self, sandbox_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.load, sandbox_id)

    def load_for_thread(self, thread_id: str, user_id: str | None, sandbox_id: str) -> dict[str, Any] | None:
        path = self._record_path(thread_id, user_id, sandbox_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read Vercel sandbox record %s: %s", path, exc)
            return None

    def save(self, record: dict[str, Any]) -> None:
        path = self._path_for_record(record)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")

    async def asave(self, record: dict[str, Any]) -> None:
        await asyncio.to_thread(self.save, record)

    def delete(self, record: dict[str, Any]) -> None:
        path = self._path_for_record(record)
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to remove Vercel sandbox record %s: %s", path, exc)

    async def adelete(self, record: dict[str, Any]) -> None:
        await asyncio.to_thread(self.delete, record)

    def try_claim_create(self, record: dict[str, Any]) -> bool:
        return True

    async def atry_claim_create(self, record: dict[str, Any]) -> bool:
        return await asyncio.to_thread(self.try_claim_create, record)


class DatabaseVercelSandboxRecordStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    def load(self, sandbox_id: str) -> dict[str, Any] | None:
        return _run_async_sync(lambda: self._load(sandbox_id))

    async def aload(self, sandbox_id: str) -> dict[str, Any] | None:
        return await self._load(sandbox_id)

    def save(self, record: dict[str, Any]) -> None:
        _run_async_sync(lambda: self._save(record))

    async def asave(self, record: dict[str, Any]) -> None:
        await self._save(record)

    def delete(self, record: dict[str, Any]) -> None:
        _run_async_sync(lambda: self._delete(str(record["sandbox_id"])))

    async def adelete(self, record: dict[str, Any]) -> None:
        await self._delete(str(record["sandbox_id"]))

    def try_claim_create(self, record: dict[str, Any]) -> bool:
        return _run_async_sync(lambda: self._try_claim_create(record))

    async def atry_claim_create(self, record: dict[str, Any]) -> bool:
        return await self._try_claim_create(record)

    async def _load(self, sandbox_id: str) -> dict[str, Any] | None:
        async with self._sf() as session:
            row = await session.get(RuntimeBindingRow, _record_id(sandbox_id))
            if row is None:
                return None
            return self._row_to_record(row)

    async def _save(self, record: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        async with self._sf() as session:
            row = await session.get(RuntimeBindingRow, _record_id(str(record["sandbox_id"])))
            if row is None:
                row = RuntimeBindingRow(
                    id=_record_id(str(record["sandbox_id"])),
                    provider=_PROVIDER,
                    sandbox_id=str(record["sandbox_id"]),
                    provider_sandbox_id=str(record.get("vercel_sandbox_id") or ""),
                    created_at=_ts_to_dt(record.get("created_at")) or now,
                    updated_at=now,
                    last_active_at=_ts_to_dt(record.get("last_active_at")) or now,
                    metadata_json={},
                )
                session.add(row)

            self._apply_record(row, record, updated_at=now)
            await session.commit()

    async def _delete(self, sandbox_id: str) -> None:
        async with self._sf() as session:
            row = await session.get(RuntimeBindingRow, _record_id(sandbox_id))
            if row is None:
                return
            await session.delete(row)
            await session.commit()

    async def _try_claim_create(self, record: dict[str, Any]) -> bool:
        now = datetime.now(UTC)
        row = RuntimeBindingRow(
            id=_record_id(str(record["sandbox_id"])),
            provider=_PROVIDER,
            sandbox_id=str(record["sandbox_id"]),
            provider_sandbox_id="",
            thread_id=record.get("thread_id"),
            user_id=record.get("user_id"),
            status="creating",
            runtime=record.get("runtime"),
            vcpus=record.get("vcpus"),
            memory_mb=record.get("memory_mb"),
            metadata_json={"claimed_at": time.time()},
            created_at=_ts_to_dt(record.get("created_at")) or now,
            updated_at=now,
            last_active_at=_ts_to_dt(record.get("last_active_at")) or now,
        )
        async with self._sf() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
            return True

    @staticmethod
    def _apply_record(row: RuntimeBindingRow, record: dict[str, Any], *, updated_at: datetime) -> None:
        row.provider_sandbox_id = str(record.get("vercel_sandbox_id") or "")
        row.thread_id = record.get("thread_id")
        row.user_id = record.get("user_id")
        row.status = str(record.get("status") or "unknown")
        row.runtime = record.get("runtime")
        row.vcpus = record.get("vcpus")
        row.memory_mb = record.get("memory_mb")
        row.updated_at = updated_at
        row.last_active_at = _ts_to_dt(record.get("last_active_at")) or updated_at
        row.stopped_at = _ts_to_dt(record.get("stopped_at"))
        metadata = dict(row.metadata_json or {})
        metadata["record_updated_at"] = time.time()
        row.metadata_json = metadata
        row.last_error = None

    @staticmethod
    def _row_to_record(row: RuntimeBindingRow) -> dict[str, Any]:
        return {
            "sandbox_id": row.sandbox_id,
            "vercel_sandbox_id": row.provider_sandbox_id,
            "thread_id": row.thread_id,
            "user_id": row.user_id,
            "status": row.status,
            "created_at": _dt_to_ts(row.created_at) or time.time(),
            "last_active_at": _dt_to_ts(row.last_active_at) or time.time(),
            "stopped_at": _dt_to_ts(row.stopped_at),
            "runtime": row.runtime,
            "vcpus": row.vcpus,
            "memory_mb": row.memory_mb,
        }
