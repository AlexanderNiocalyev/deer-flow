from __future__ import annotations

import atexit
import asyncio
import hashlib
import logging
import os
import signal
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]
    import msvcrt

from deerflow.config import get_app_config
from deerflow.config.paths import get_paths
from deerflow.persistence.engine import get_session_factory
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import SandboxProvider

from .record_store import DatabaseVercelSandboxRecordStore, FileVercelSandboxRecordStore, VercelSandboxRecordStore
from .vercel_sandbox import VercelSandbox

logger = logging.getLogger(__name__)

DEFAULT_RUNTIME = "python3.13"
DEFAULT_TIMEOUT_MS = 60 * 60 * 1000
DEFAULT_VCPUS = 2
DEFAULT_MEMORY_MB = DEFAULT_VCPUS * 2048
DEFAULT_MAX_SYNC_FILE_BYTES = 100 * 1024 * 1024
DEFAULT_STOP_ON_RELEASE = True
DEFAULT_RECORD_CLAIM_TIMEOUT_S = 60.0


def _lock_file_exclusive(lock_file) -> None:
    if fcntl is not None:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        return

    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)


def _unlock_file(lock_file) -> None:
    if fcntl is not None:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        return

    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def _resolve_env_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.startswith("$"):
        return os.environ.get(value[1:], "")
    return str(value)


def _resolve_env_vars(env_config: dict[str, str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for key, value in env_config.items():
        resolved_value = _resolve_env_value(value)
        resolved[key] = "" if resolved_value is None else resolved_value
    return resolved


def _load_vercel_sdk():
    try:
        from vercel.sandbox import Resources
        from vercel.sandbox import Sandbox as VercelSDKSandbox
    except ImportError as exc:
        raise RuntimeError("VercelSandboxProvider requires the `vercel` Python package. Run `uv sync` after enabling this provider.") from exc
    return VercelSDKSandbox, Resources


@dataclass
class VercelSandboxRecord:
    """Persisted mapping from DeerFlow sandbox id to Vercel sandbox id."""

    sandbox_id: str
    vercel_sandbox_id: str
    thread_id: str | None
    user_id: str | None
    status: str
    created_at: float
    last_active_at: float
    stopped_at: float | None = None
    runtime: str | None = None
    vcpus: int | None = None
    memory_mb: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VercelSandboxRecord:
        return cls(
            sandbox_id=str(data["sandbox_id"]),
            vercel_sandbox_id=str(data["vercel_sandbox_id"]),
            thread_id=data.get("thread_id"),
            user_id=data.get("user_id"),
            status=str(data.get("status") or "unknown"),
            created_at=float(data.get("created_at") or time.time()),
            last_active_at=float(data.get("last_active_at") or time.time()),
            stopped_at=data.get("stopped_at"),
            runtime=data.get("runtime"),
            vcpus=data.get("vcpus"),
            memory_mb=data.get("memory_mb"),
        )


class VercelSandboxProvider(SandboxProvider):
    """Sandbox provider that runs DeerFlow thread workspaces in Vercel Sandbox.

    DeerFlow keeps a deterministic per-thread sandbox id and persists the
    provider-owned Vercel sandbox id in the app database when available,
    falling back to per-thread JSON for local development. This separates
    business session identity from the external provider's resource identity
    and lets a stopped persistent Vercel sandbox be resumed across turns.
    """

    uses_thread_data_mounts = False
    needs_upload_permission_adjustment = False

    def __init__(self):
        self._lock = threading.Lock()
        self._sandboxes: dict[str, VercelSandbox] = {}
        self._records: dict[str, VercelSandboxRecord] = {}
        self._thread_sandboxes: dict[tuple[str, str], str] = {}
        self._thread_locks: dict[tuple[str, str], threading.Lock] = {}
        self._last_activity: dict[str, float] = {}
        self._shutdown_called = False
        self._config = self._load_config()
        self._file_record_store = FileVercelSandboxRecordStore(self._record_path)
        self._db_record_store: DatabaseVercelSandboxRecordStore | None = None
        self._db_record_store_session_factory: Any | None = None

        atexit.register(self.shutdown)
        self._register_signal_handlers()

    def acquire(self, thread_id: str | None = None, *, user_id: str | None = None) -> str:
        effective_user_id = self._effective_acquire_user_id(user_id)
        if thread_id:
            thread_lock = self._get_thread_lock(thread_id, effective_user_id)
            with thread_lock:
                return self._acquire_internal(thread_id, user_id=effective_user_id)
        return self._acquire_internal(thread_id, user_id=effective_user_id)

    async def acquire_async(self, thread_id: str | None = None, *, user_id: str | None = None) -> str:
        effective_user_id = self._effective_acquire_user_id(user_id)
        if thread_id:
            thread_lock = self._get_thread_lock(thread_id, effective_user_id)
            await asyncio.to_thread(thread_lock.acquire)
            try:
                return await self._acquire_internal_async(thread_id, user_id=effective_user_id)
            finally:
                thread_lock.release()
        return await self._acquire_internal_async(thread_id, user_id=effective_user_id)

    def get(self, sandbox_id: str) -> Sandbox | None:
        with self._lock:
            sandbox = self._sandboxes.get(sandbox_id)
            if sandbox is not None:
                self._last_activity[sandbox_id] = time.time()
            return sandbox

    def release(self, sandbox_id: str) -> None:
        sandbox: VercelSandbox | None = None
        record: VercelSandboxRecord | None = None
        with self._lock:
            sandbox = self._sandboxes.pop(sandbox_id, None)
            record = self._records.get(sandbox_id)
            self._last_activity.pop(sandbox_id, None)

        if sandbox is None:
            logger.info("Vercel sandbox %s is not active; nothing to release", sandbox_id)
            return

        try:
            sandbox.sync_to_host()
        except Exception as exc:
            logger.warning("Failed to sync Vercel sandbox %s to host during release: %s", sandbox_id, exc)

        try:
            if self._config["stop_on_release"]:
                sandbox.stop(blocking=False)
                if record is not None:
                    record.status = "stopped"
                    record.stopped_at = time.time()
            elif record is not None:
                record.status = "running"
        except Exception as exc:
            logger.warning("Failed to stop Vercel sandbox %s during release: %s", sandbox_id, exc)
        finally:
            if record is not None:
                record.last_active_at = time.time()
                self._persist_record(record)
            sandbox.close()

        logger.info("Released Vercel sandbox %s", sandbox_id)

    async def release_async(self, sandbox_id: str) -> None:
        record_store = self._record_store()
        if not isinstance(record_store, DatabaseVercelSandboxRecordStore):
            await asyncio.to_thread(self.release, sandbox_id)
            return

        sandbox: VercelSandbox | None = None
        record: VercelSandboxRecord | None = None
        with self._lock:
            sandbox = self._sandboxes.pop(sandbox_id, None)
            record = self._records.get(sandbox_id)
            self._last_activity.pop(sandbox_id, None)

        if sandbox is None:
            logger.info("Vercel sandbox %s is not active; nothing to release", sandbox_id)
            return

        try:
            await asyncio.to_thread(sandbox.sync_to_host)
        except Exception as exc:
            logger.warning("Failed to sync Vercel sandbox %s to host during release: %s", sandbox_id, exc)

        try:
            if self._config["stop_on_release"]:
                await asyncio.to_thread(sandbox.stop, blocking=False)
                if record is not None:
                    record.status = "stopped"
                    record.stopped_at = time.time()
            elif record is not None:
                record.status = "running"
        except Exception as exc:
            logger.warning("Failed to stop Vercel sandbox %s during release: %s", sandbox_id, exc)
        finally:
            if record is not None:
                record.last_active_at = time.time()
                await self._persist_record_async(record)
            await asyncio.to_thread(sandbox.close)

        logger.info("Released Vercel sandbox %s", sandbox_id)

    def destroy(self, sandbox_id: str) -> None:
        """Stop and forget a Vercel-backed sandbox mapping."""
        sandbox: VercelSandbox | None = None
        record: VercelSandboxRecord | None = None
        thread_keys_to_remove: list[tuple[str, str]] = []

        with self._lock:
            sandbox = self._sandboxes.pop(sandbox_id, None)
            record = self._records.pop(sandbox_id, None)
            thread_keys_to_remove = [key for key, sid in self._thread_sandboxes.items() if sid == sandbox_id]
            for key in thread_keys_to_remove:
                del self._thread_sandboxes[key]
            self._last_activity.pop(sandbox_id, None)

        if sandbox is not None:
            try:
                sandbox.sync_to_host()
            except Exception as exc:
                logger.warning("Failed to sync Vercel sandbox %s during destroy: %s", sandbox_id, exc)
            try:
                sandbox.stop(blocking=False)
            except Exception as exc:
                logger.warning("Failed to stop Vercel sandbox %s during destroy: %s", sandbox_id, exc)
            finally:
                sandbox.close()

        if record is not None:
            self._delete_record(record)

    def release_thread(self, thread_id: str, *, user_id: str | None = None, reason: str | None = None) -> dict[str, Any]:
        """Release the Vercel sandbox associated with a DeerFlow thread.

        This is used by production lifecycle controllers such as Orpheus' idle
        reaper. It deliberately does not call ``acquire``: lifecycle cleanup
        must never create a fresh remote sandbox just to stop it.
        """
        effective_user_id = self._effective_acquire_user_id(user_id)
        sandbox_id = self._sandbox_id_for_thread(thread_id, effective_user_id)
        thread_lock = self._get_thread_lock(thread_id, effective_user_id)
        with thread_lock:
            paths = get_paths()
            paths.ensure_thread_dirs(thread_id, user_id=effective_user_id)
            lock_path = paths.thread_dir(thread_id, user_id=effective_user_id) / f"{sandbox_id}.vercel.lock"
            with open(lock_path, "a", encoding="utf-8") as lock_file:
                _lock_file_exclusive(lock_file)
                try:
                    return self._release_thread_locked(
                        thread_id,
                        sandbox_id,
                        user_id=effective_user_id,
                        reason=reason,
                    )
                finally:
                    _unlock_file(lock_file)

    async def release_thread_async(self, thread_id: str, *, user_id: str | None = None, reason: str | None = None) -> dict[str, Any]:
        """Async counterpart used by FastAPI request handlers.

        Database-backed bindings must stay on the request event loop because
        SQLAlchemy/asyncpg pools are loop-bound. File-backed local development
        keeps using the existing synchronous implementation in a worker thread.
        """
        if not isinstance(self._record_store(), DatabaseVercelSandboxRecordStore):
            return await asyncio.to_thread(self.release_thread, thread_id, user_id=user_id, reason=reason)

        effective_user_id = self._effective_acquire_user_id(user_id)
        sandbox_id = self._sandbox_id_for_thread(thread_id, effective_user_id)
        thread_lock = self._get_thread_lock(thread_id, effective_user_id)
        await asyncio.to_thread(thread_lock.acquire)
        try:
            await asyncio.to_thread(get_paths().ensure_thread_dirs, thread_id, user_id=effective_user_id)
            return await self._release_thread_locked_async(
                thread_id,
                sandbox_id,
                user_id=effective_user_id,
                reason=reason,
            )
        finally:
            thread_lock.release()

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown_called:
                return
            self._shutdown_called = True
            sandbox_ids = list(self._sandboxes)

        for sandbox_id in sandbox_ids:
            try:
                self.release(sandbox_id)
            except Exception as exc:
                logger.warning("Failed to release Vercel sandbox %s during shutdown: %s", sandbox_id, exc)

    def reset(self) -> None:
        with self._lock:
            self._sandboxes.clear()
            self._records.clear()
            self._thread_sandboxes.clear()
            self._thread_locks.clear()
            self._last_activity.clear()
            self._shutdown_called = False

    def _load_config(self) -> dict[str, Any]:
        config = get_app_config()
        sandbox_config = config.sandbox

        vcpus = getattr(sandbox_config, "vercel_vcpus", None) or DEFAULT_VCPUS
        memory_mb = getattr(sandbox_config, "vercel_memory_mb", None) or (vcpus * 2048)
        if memory_mb != vcpus * 2048:
            raise ValueError("Vercel Sandbox requires sandbox.vercel_memory_mb to equal sandbox.vercel_vcpus * 2048")

        env = _resolve_env_vars(getattr(sandbox_config, "environment", None) or {})
        env.update(_resolve_env_vars(getattr(sandbox_config, "vercel_environment", None) or {}))

        record_store = (getattr(sandbox_config, "vercel_record_store", None) or "auto").lower()
        if record_store not in {"auto", "database", "file"}:
            raise ValueError("sandbox.vercel_record_store must be one of: auto, database, file")

        return {
            "token": _resolve_env_value(getattr(sandbox_config, "vercel_token", None)),
            "project_id": _resolve_env_value(getattr(sandbox_config, "vercel_project_id", None)),
            "team_id": _resolve_env_value(getattr(sandbox_config, "vercel_team_id", None)),
            "runtime": getattr(sandbox_config, "vercel_runtime", None) or DEFAULT_RUNTIME,
            "image": getattr(sandbox_config, "vercel_image", None),
            "timeout": getattr(sandbox_config, "vercel_timeout_ms", None) or DEFAULT_TIMEOUT_MS,
            "vcpus": vcpus,
            "memory_mb": memory_mb,
            "ports": getattr(sandbox_config, "vercel_ports", None) or None,
            "interactive": bool(getattr(sandbox_config, "vercel_interactive", False)),
            "environment": env,
            "max_sync_file_bytes": (getattr(sandbox_config, "vercel_sync_max_file_bytes", None) or DEFAULT_MAX_SYNC_FILE_BYTES),
            "stop_on_release": bool(getattr(sandbox_config, "vercel_stop_on_release", DEFAULT_STOP_ON_RELEASE)),
            "record_store": record_store,
            "record_claim_timeout_s": float(getattr(sandbox_config, "vercel_record_claim_timeout_s", None) or DEFAULT_RECORD_CLAIM_TIMEOUT_S),
        }

    @staticmethod
    def _effective_acquire_user_id(user_id: str | None) -> str:
        return user_id or get_effective_user_id()

    @staticmethod
    def _thread_key(thread_id: str, user_id: str) -> tuple[str, str]:
        return (user_id, thread_id)

    @staticmethod
    def _deterministic_sandbox_id(thread_id: str, user_id: str) -> str:
        digest = hashlib.sha256(f"{user_id}:{thread_id}".encode()).hexdigest()[:12]
        return f"vercel-{digest}"

    def _sandbox_id_for_thread(self, thread_id: str | None, user_id: str) -> str:
        if thread_id:
            return self._deterministic_sandbox_id(thread_id, user_id)
        return f"vercel-{str(uuid.uuid4())[:12]}"

    def _get_thread_lock(self, thread_id: str, user_id: str) -> threading.Lock:
        key = self._thread_key(thread_id, user_id)
        with self._lock:
            if key not in self._thread_locks:
                self._thread_locks[key] = threading.Lock()
            return self._thread_locks[key]

    def _reuse_in_process_sandbox(self, thread_id: str | None, *, user_id: str) -> str | None:
        if thread_id is None:
            return None
        key = self._thread_key(thread_id, user_id)
        with self._lock:
            existing_id = self._thread_sandboxes.get(key)
            if existing_id is None or existing_id not in self._sandboxes:
                return None
            self._last_activity[existing_id] = time.time()
            return existing_id

    def _acquire_internal(self, thread_id: str | None, *, user_id: str) -> str:
        cached_id = self._reuse_in_process_sandbox(thread_id, user_id=user_id)
        if cached_id is not None:
            return cached_id

        sandbox_id = self._sandbox_id_for_thread(thread_id, user_id)
        if thread_id is None:
            return self._create_and_register(None, sandbox_id, user_id=user_id, record=None)

        paths = get_paths()
        paths.ensure_thread_dirs(thread_id, user_id=user_id)
        lock_path = paths.thread_dir(thread_id, user_id=user_id) / f"{sandbox_id}.vercel.lock"
        with open(lock_path, "a", encoding="utf-8") as lock_file:
            locked = False
            try:
                _lock_file_exclusive(lock_file)
                locked = True
                cached_id = self._reuse_in_process_sandbox(thread_id, user_id=user_id)
                if cached_id is not None:
                    return cached_id

                record = self._load_record(thread_id, user_id, sandbox_id)
                if record is not None and record.vercel_sandbox_id:
                    try:
                        client = self._get_vercel_sandbox(record.vercel_sandbox_id)
                        return self._register_sandbox(thread_id, sandbox_id, user_id=user_id, client=client, record=record)
                    except Exception as exc:
                        logger.warning(
                            "Failed to resume Vercel sandbox %s for DeerFlow sandbox %s; creating a new sandbox: %s",
                            record.vercel_sandbox_id,
                            sandbox_id,
                            exc,
                        )
                        self._delete_record(record)
                        record = None

                if record is not None and not record.vercel_sandbox_id:
                    ready_record = self._wait_for_ready_record(thread_id, user_id, sandbox_id)
                    if ready_record is not None and ready_record.vercel_sandbox_id:
                        client = self._get_vercel_sandbox(ready_record.vercel_sandbox_id)
                        return self._register_sandbox(thread_id, sandbox_id, user_id=user_id, client=client, record=ready_record)
                    raise RuntimeError(f"Timed out waiting for Vercel sandbox creation claim for DeerFlow sandbox {sandbox_id}")

                pending_record = self._pending_record(thread_id, sandbox_id, user_id=user_id, previous=record)
                claimed = self._try_claim_record(pending_record)
                if not claimed:
                    ready_record = self._wait_for_ready_record(thread_id, user_id, sandbox_id)
                    if ready_record is not None and ready_record.vercel_sandbox_id:
                        client = self._get_vercel_sandbox(ready_record.vercel_sandbox_id)
                        return self._register_sandbox(thread_id, sandbox_id, user_id=user_id, client=client, record=ready_record)
                    raise RuntimeError(f"Timed out waiting for Vercel sandbox record for DeerFlow sandbox {sandbox_id}")

                try:
                    return self._create_and_register(thread_id, sandbox_id, user_id=user_id, record=pending_record)
                except Exception:
                    self._delete_record(pending_record)
                    raise
            finally:
                if locked:
                    _unlock_file(lock_file)

    async def _acquire_internal_async(self, thread_id: str | None, *, user_id: str) -> str:
        record_store = self._record_store()
        if not isinstance(record_store, DatabaseVercelSandboxRecordStore):
            return await asyncio.to_thread(self._acquire_internal, thread_id, user_id=user_id)

        cached_id = await asyncio.to_thread(self._reuse_in_process_sandbox, thread_id, user_id=user_id)
        if cached_id is not None:
            return cached_id

        sandbox_id = self._sandbox_id_for_thread(thread_id, user_id)
        if thread_id is None:
            return await self._create_and_register_async(None, sandbox_id, user_id=user_id, record=None)

        await asyncio.to_thread(get_paths().ensure_thread_dirs, thread_id, user_id=user_id)

        record = await self._load_record_async(thread_id, user_id, sandbox_id)
        if record is not None and record.vercel_sandbox_id:
            try:
                client = await asyncio.to_thread(self._get_vercel_sandbox, record.vercel_sandbox_id)
                return await self._register_sandbox_async(thread_id, sandbox_id, user_id=user_id, client=client, record=record)
            except Exception as exc:
                logger.warning(
                    "Failed to resume Vercel sandbox %s for DeerFlow sandbox %s; creating a new sandbox: %s",
                    record.vercel_sandbox_id,
                    sandbox_id,
                    exc,
                )
                await self._delete_record_async(record)
                record = None

        if record is not None and not record.vercel_sandbox_id:
            ready_record = await self._wait_for_ready_record_async(thread_id, user_id, sandbox_id)
            if ready_record is not None and ready_record.vercel_sandbox_id:
                client = await asyncio.to_thread(self._get_vercel_sandbox, ready_record.vercel_sandbox_id)
                return await self._register_sandbox_async(thread_id, sandbox_id, user_id=user_id, client=client, record=ready_record)
            raise RuntimeError(f"Timed out waiting for Vercel sandbox creation claim for DeerFlow sandbox {sandbox_id}")

        pending_record = self._pending_record(thread_id, sandbox_id, user_id=user_id, previous=record)
        claimed = await self._try_claim_record_async(pending_record)
        if not claimed:
            ready_record = await self._wait_for_ready_record_async(thread_id, user_id, sandbox_id)
            if ready_record is not None and ready_record.vercel_sandbox_id:
                client = await asyncio.to_thread(self._get_vercel_sandbox, ready_record.vercel_sandbox_id)
                return await self._register_sandbox_async(thread_id, sandbox_id, user_id=user_id, client=client, record=ready_record)
            raise RuntimeError(f"Timed out waiting for Vercel sandbox record for DeerFlow sandbox {sandbox_id}")

        try:
            return await self._create_and_register_async(thread_id, sandbox_id, user_id=user_id, record=pending_record)
        except Exception:
            await self._delete_record_async(pending_record)
            raise

    def _create_and_register(
        self,
        thread_id: str | None,
        sandbox_id: str,
        *,
        user_id: str,
        record: VercelSandboxRecord | None,
    ) -> str:
        client = self._create_vercel_sandbox()
        remote_id = self._remote_sandbox_id(client)
        now = time.time()
        new_record = VercelSandboxRecord(
            sandbox_id=sandbox_id,
            vercel_sandbox_id=remote_id,
            thread_id=thread_id,
            user_id=user_id,
            status="running",
            created_at=record.created_at if record is not None else now,
            last_active_at=now,
            runtime=self._config["runtime"],
            vcpus=self._config["vcpus"],
            memory_mb=self._config["memory_mb"],
        )
        return self._register_sandbox(thread_id, sandbox_id, user_id=user_id, client=client, record=new_record)

    async def _create_and_register_async(
        self,
        thread_id: str | None,
        sandbox_id: str,
        *,
        user_id: str,
        record: VercelSandboxRecord | None,
    ) -> str:
        client = await asyncio.to_thread(self._create_vercel_sandbox)
        remote_id = self._remote_sandbox_id(client)
        now = time.time()
        new_record = VercelSandboxRecord(
            sandbox_id=sandbox_id,
            vercel_sandbox_id=remote_id,
            thread_id=thread_id,
            user_id=user_id,
            status="running",
            created_at=record.created_at if record is not None else now,
            last_active_at=now,
            runtime=self._config["runtime"],
            vcpus=self._config["vcpus"],
            memory_mb=self._config["memory_mb"],
        )
        return await self._register_sandbox_async(thread_id, sandbox_id, user_id=user_id, client=client, record=new_record)

    def _register_sandbox(
        self,
        thread_id: str | None,
        sandbox_id: str,
        *,
        user_id: str,
        client: Any,
        record: VercelSandboxRecord,
        persist: bool = True,
    ) -> str:
        sandbox = VercelSandbox(
            id=sandbox_id,
            client=client,
            thread_id=thread_id,
            user_id=user_id,
            paths=get_paths(),
            max_sync_file_bytes=self._config["max_sync_file_bytes"],
        )
        sandbox.bootstrap()
        sandbox.sync_from_host()

        record.vercel_sandbox_id = sandbox.vercel_sandbox_id or record.vercel_sandbox_id
        record.status = "running"
        record.last_active_at = time.time()
        record.stopped_at = None

        with self._lock:
            self._sandboxes[sandbox_id] = sandbox
            self._records[sandbox_id] = record
            self._last_activity[sandbox_id] = time.time()
            if thread_id:
                self._thread_sandboxes[self._thread_key(thread_id, user_id)] = sandbox_id

        logger.info(
            "Acquired Vercel sandbox %s for DeerFlow sandbox %s thread %s",
            record.vercel_sandbox_id,
            sandbox_id,
            thread_id,
        )
        if persist:
            self._persist_record(record)
        return sandbox_id

    async def _register_sandbox_async(
        self,
        thread_id: str | None,
        sandbox_id: str,
        *,
        user_id: str,
        client: Any,
        record: VercelSandboxRecord,
    ) -> str:
        result = await asyncio.to_thread(
            self._register_sandbox,
            thread_id,
            sandbox_id,
            user_id=user_id,
            client=client,
            record=record,
            persist=False,
        )
        await self._persist_record_async(record)
        return result

    def _create_vercel_sandbox(self):
        VercelSDKSandbox, Resources = _load_vercel_sdk()
        resources = Resources(vcpus=self._config["vcpus"], memory=self._config["memory_mb"])
        return VercelSDKSandbox.create(
            ports=self._config["ports"],
            timeout=self._config["timeout"],
            resources=resources,
            runtime=self._config["runtime"],
            image=self._config["image"],
            token=self._config["token"],
            project_id=self._config["project_id"],
            team_id=self._config["team_id"],
            interactive=self._config["interactive"],
            env=self._config["environment"],
        )

    def _get_vercel_sandbox(self, vercel_sandbox_id: str):
        VercelSDKSandbox, _ = _load_vercel_sdk()
        return VercelSDKSandbox.get(
            sandbox_id=vercel_sandbox_id,
            token=self._config["token"],
            project_id=self._config["project_id"],
            team_id=self._config["team_id"],
        )

    def _release_thread_locked(
        self,
        thread_id: str,
        sandbox_id: str,
        *,
        user_id: str,
        reason: str | None,
    ) -> dict[str, Any]:
        with self._lock:
            active_sandbox = self._sandboxes.get(sandbox_id)

        if active_sandbox is not None:
            self.release(sandbox_id)
            return {
                "status": "released",
                "provider": "vercel",
                "thread_id": thread_id,
                "sandbox_id": sandbox_id,
                "message": "Active Vercel sandbox released.",
                "reason": reason,
            }

        record = self._load_record(thread_id, user_id, sandbox_id)
        if record is None:
            return {
                "status": "not_found",
                "provider": "vercel",
                "thread_id": thread_id,
                "sandbox_id": sandbox_id,
                "message": "No Vercel sandbox record exists for this thread.",
                "reason": reason,
            }

        if not record.vercel_sandbox_id:
            return {
                "status": "creating",
                "provider": "vercel",
                "thread_id": thread_id,
                "sandbox_id": sandbox_id,
                "message": "Vercel sandbox creation is still claimed by another worker.",
                "reason": reason,
            }

        if record.status == "stopped":
            return {
                "status": "already_stopped",
                "provider": "vercel",
                "thread_id": thread_id,
                "sandbox_id": sandbox_id,
                "vercel_sandbox_id": record.vercel_sandbox_id,
                "message": "Vercel sandbox is already stopped.",
                "reason": reason,
            }

        client = self._get_vercel_sandbox(record.vercel_sandbox_id)
        sandbox = VercelSandbox(
            id=sandbox_id,
            client=client,
            thread_id=thread_id,
            user_id=user_id,
            paths=get_paths(),
            max_sync_file_bytes=self._config["max_sync_file_bytes"],
        )
        try:
            try:
                sandbox.sync_to_host()
            except Exception as exc:
                logger.warning("Failed to sync Vercel sandbox %s during thread release: %s", sandbox_id, exc)

            sandbox.stop(blocking=False)
            record.status = "stopped"
            record.stopped_at = time.time()
            record.last_active_at = time.time()
            self._persist_record(record)
            return {
                "status": "released",
                "provider": "vercel",
                "thread_id": thread_id,
                "sandbox_id": sandbox_id,
                "vercel_sandbox_id": record.vercel_sandbox_id,
                "message": "Recorded Vercel sandbox stopped.",
                "reason": reason,
            }
        finally:
            sandbox.close()

    async def _release_thread_locked_async(
        self,
        thread_id: str,
        sandbox_id: str,
        *,
        user_id: str,
        reason: str | None,
    ) -> dict[str, Any]:
        with self._lock:
            active_sandbox = self._sandboxes.get(sandbox_id)

        if active_sandbox is not None:
            await self.release_async(sandbox_id)
            return {
                "status": "released",
                "provider": "vercel",
                "thread_id": thread_id,
                "sandbox_id": sandbox_id,
                "message": "Active Vercel sandbox released.",
                "reason": reason,
            }

        record = await self._load_record_async(thread_id, user_id, sandbox_id)
        if record is None:
            return {
                "status": "not_found",
                "provider": "vercel",
                "thread_id": thread_id,
                "sandbox_id": sandbox_id,
                "message": "No Vercel sandbox record exists for this thread.",
                "reason": reason,
            }

        if not record.vercel_sandbox_id:
            return {
                "status": "creating",
                "provider": "vercel",
                "thread_id": thread_id,
                "sandbox_id": sandbox_id,
                "message": "Vercel sandbox creation is still claimed by another worker.",
                "reason": reason,
            }

        if record.status == "stopped":
            return {
                "status": "already_stopped",
                "provider": "vercel",
                "thread_id": thread_id,
                "sandbox_id": sandbox_id,
                "vercel_sandbox_id": record.vercel_sandbox_id,
                "message": "Vercel sandbox is already stopped.",
                "reason": reason,
            }

        client = await asyncio.to_thread(self._get_vercel_sandbox, record.vercel_sandbox_id)
        sandbox = VercelSandbox(
            id=sandbox_id,
            client=client,
            thread_id=thread_id,
            user_id=user_id,
            paths=get_paths(),
            max_sync_file_bytes=self._config["max_sync_file_bytes"],
        )
        try:
            try:
                await asyncio.to_thread(sandbox.sync_to_host)
            except Exception as exc:
                logger.warning("Failed to sync Vercel sandbox %s during thread release: %s", sandbox_id, exc)

            await asyncio.to_thread(sandbox.stop, blocking=False)
            record.status = "stopped"
            record.stopped_at = time.time()
            record.last_active_at = time.time()
            await self._persist_record_async(record)
            return {
                "status": "released",
                "provider": "vercel",
                "thread_id": thread_id,
                "sandbox_id": sandbox_id,
                "vercel_sandbox_id": record.vercel_sandbox_id,
                "message": "Recorded Vercel sandbox stopped.",
                "reason": reason,
            }
        finally:
            await asyncio.to_thread(sandbox.close)

    @staticmethod
    def _remote_sandbox_id(client: Any) -> str:
        sandbox_id = getattr(client, "sandbox_id", None)
        if sandbox_id:
            return str(sandbox_id)

        sandbox = getattr(client, "sandbox", None)
        sandbox_id = getattr(sandbox, "id", None)
        if sandbox_id:
            return str(sandbox_id)

        raise RuntimeError("Vercel SDK did not return a sandbox id")

    def _record_path(self, thread_id: str, user_id: str | None, sandbox_id: str) -> Path:
        return get_paths().thread_dir(thread_id, user_id=user_id) / f"{sandbox_id}.vercel-sandbox.json"

    def _record_store(self) -> VercelSandboxRecordStore:
        mode = self._config["record_store"]
        if mode in {"auto", "database"}:
            session_factory = get_session_factory()
            if session_factory is not None:
                if self._db_record_store is None or self._db_record_store_session_factory is not session_factory:
                    self._db_record_store = DatabaseVercelSandboxRecordStore(session_factory)
                    self._db_record_store_session_factory = session_factory
                return self._db_record_store
            if mode == "database":
                raise RuntimeError("sandbox.vercel_record_store=database requires the DeerFlow persistence engine. Set database.backend to sqlite/postgres and initialize the Gateway before acquiring sandboxes.")
        return self._file_record_store

    def _load_record(self, thread_id: str, user_id: str, sandbox_id: str) -> VercelSandboxRecord | None:
        store = self._record_store()
        if isinstance(store, FileVercelSandboxRecordStore):
            data = store.load_for_thread(thread_id, user_id, sandbox_id)
        else:
            data = store.load(sandbox_id)
        if data is None:
            return None
        try:
            return VercelSandboxRecord.from_dict(data)
        except Exception as exc:
            logger.warning("Failed to parse Vercel sandbox record for %s: %s", sandbox_id, exc)
            return None

    async def _load_record_async(self, thread_id: str, user_id: str, sandbox_id: str) -> VercelSandboxRecord | None:
        store = self._record_store()
        if isinstance(store, FileVercelSandboxRecordStore):
            data = await asyncio.to_thread(store.load_for_thread, thread_id, user_id, sandbox_id)
        else:
            data = await store.aload(sandbox_id)
        if data is None:
            return None
        try:
            return VercelSandboxRecord.from_dict(data)
        except Exception as exc:
            logger.warning("Failed to parse Vercel sandbox record for %s: %s", sandbox_id, exc)
            return None

    def _persist_record(self, record: VercelSandboxRecord) -> None:
        if record.thread_id is None:
            return
        self._record_store().save(asdict(record))

    async def _persist_record_async(self, record: VercelSandboxRecord) -> None:
        if record.thread_id is None:
            return
        await self._record_store().asave(asdict(record))

    def _delete_record(self, record: VercelSandboxRecord) -> None:
        self._record_store().delete(asdict(record))

    async def _delete_record_async(self, record: VercelSandboxRecord) -> None:
        await self._record_store().adelete(asdict(record))

    def _try_claim_record(self, record: VercelSandboxRecord) -> bool:
        return self._record_store().try_claim_create(asdict(record))

    async def _try_claim_record_async(self, record: VercelSandboxRecord) -> bool:
        return await self._record_store().atry_claim_create(asdict(record))

    def _pending_record(
        self,
        thread_id: str,
        sandbox_id: str,
        *,
        user_id: str,
        previous: VercelSandboxRecord | None,
    ) -> VercelSandboxRecord:
        now = time.time()
        return VercelSandboxRecord(
            sandbox_id=sandbox_id,
            vercel_sandbox_id="",
            thread_id=thread_id,
            user_id=user_id,
            status="creating",
            created_at=previous.created_at if previous is not None else now,
            last_active_at=now,
            runtime=self._config["runtime"],
            vcpus=self._config["vcpus"],
            memory_mb=self._config["memory_mb"],
        )

    def _wait_for_ready_record(self, thread_id: str, user_id: str, sandbox_id: str) -> VercelSandboxRecord | None:
        deadline = time.monotonic() + self._config["record_claim_timeout_s"]
        while time.monotonic() < deadline:
            time.sleep(0.25)
            record = self._load_record(thread_id, user_id, sandbox_id)
            if record is not None and record.vercel_sandbox_id:
                return record
        return None

    async def _wait_for_ready_record_async(self, thread_id: str, user_id: str, sandbox_id: str) -> VercelSandboxRecord | None:
        deadline = time.monotonic() + self._config["record_claim_timeout_s"]
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            record = await self._load_record_async(thread_id, user_id, sandbox_id)
            if record is not None and record.vercel_sandbox_id:
                return record
        return None

    def _register_signal_handlers(self) -> None:
        self._original_sigterm = signal.getsignal(signal.SIGTERM)
        self._original_sigint = signal.getsignal(signal.SIGINT)
        self._original_sighup = signal.getsignal(signal.SIGHUP) if hasattr(signal, "SIGHUP") else None

        def signal_handler(signum, frame):
            self.shutdown()
            if signum == signal.SIGTERM:
                original = self._original_sigterm
            elif hasattr(signal, "SIGHUP") and signum == signal.SIGHUP:
                original = self._original_sighup
            else:
                original = self._original_sigint
            if callable(original):
                original(signum, frame)
            elif original == signal.SIG_DFL:
                signal.signal(signum, signal.SIG_DFL)
                signal.raise_signal(signum)

        try:
            signal.signal(signal.SIGTERM, signal_handler)
            signal.signal(signal.SIGINT, signal_handler)
            if hasattr(signal, "SIGHUP"):
                signal.signal(signal.SIGHUP, signal_handler)
        except ValueError:
            logger.debug("Could not register Vercel sandbox signal handlers outside the main thread")
