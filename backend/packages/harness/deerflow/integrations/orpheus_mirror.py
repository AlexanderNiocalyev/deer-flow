"""Best-effort Orpheus Agent Workspace mirror callbacks."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
from urllib.parse import quote

import httpx

from deerflow.runtime.runs.manager import RunRecord

logger = logging.getLogger(__name__)

CALLBACK_HEADER_NAME = "x-orpheus-agent-workspace-token"
CALLBACK_URL_ENV = "ORPHEUS_AGENT_WORKSPACE_CALLBACK_URL"
CALLBACK_TOKEN_ENV = "ORPHEUS_AGENT_WORKSPACE_CALLBACK_TOKEN"
CALLBACK_TIMEOUT_ENV = "ORPHEUS_AGENT_WORKSPACE_CALLBACK_TIMEOUT_SECONDS"
PUBLIC_BASE_URL_ENV = "DEERFLOW_PUBLIC_BASE_URL"


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _callback_url() -> str | None:
    return _clean(os.environ.get(CALLBACK_URL_ENV))


def _callback_token() -> str | None:
    return _clean(os.environ.get(CALLBACK_TOKEN_ENV))


def _timeout_seconds() -> float:
    try:
        value = float(os.environ.get(CALLBACK_TIMEOUT_ENV, "5"))
    except ValueError:
        return 5.0
    return max(1.0, min(value, 30.0))


def _orpheus_metadata(record: RunRecord) -> tuple[str, str] | None:
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    session_id = _clean(metadata.get("orpheus_session_id"))
    workspace_id = _clean(metadata.get("orpheus_workspace_id"))
    if not session_id or not workspace_id:
        return None
    return session_id, workspace_id


def _artifact_url(thread_id: str, path: str) -> str | None:
    base_url = _clean(os.environ.get(PUBLIC_BASE_URL_ENV))
    if not base_url:
        return None
    normalized_base = base_url.rstrip("/")
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{normalized_base}/api/threads/{quote(thread_id, safe='')}/artifacts{quote(normalized_path, safe='/')}?download=true"


def _artifact_payload(record: RunRecord, artifacts: list[str] | None) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for path in artifacts or []:
        if not isinstance(path, str) or not path.strip():
            continue
        clean_path = path.strip()
        payload.append(
            {
                "id": f"{record.run_id}:{clean_path}",
                "run_id": record.run_id,
                "path": clean_path,
                "object_url": _artifact_url(record.thread_id, clean_path),
            }
        )
    return payload


def build_orpheus_mirror_payload(
    record: RunRecord,
    *,
    artifacts: list[str] | None = None,
) -> dict[str, Any] | None:
    context = _orpheus_metadata(record)
    if context is None:
        return None
    session_id, workspace_id = context
    event_id = f"{record.run_id}:{record.status.value}:{record.updated_at}"
    return {
        "workspace_id": workspace_id,
        "session_id": session_id,
        "thread_id": record.thread_id,
        "run": {
            "id": record.run_id,
            "status": record.status.value,
            "started_at": record.created_at,
            "finished_at": record.updated_at,
            "error": record.error,
            "metadata": {
                "assistant_id": record.assistant_id,
                "model_name": record.model_name,
            },
        },
        "events": [
            {
                "id": event_id,
                "run_id": record.run_id,
                "event_type": f"deerflow.run.{record.status.value}",
                "message": f"DeerFlow run {record.status.value}.",
                "payload": {
                    "assistant_id": record.assistant_id,
                    "model_name": record.model_name,
                    "error": record.error,
                },
                "created_at": record.updated_at,
            }
        ],
        "artifacts": _artifact_payload(record, artifacts),
    }


async def post_orpheus_mirror(record: RunRecord, *, artifacts: list[str] | None = None) -> bool:
    url = _callback_url()
    token = _callback_token()
    payload = build_orpheus_mirror_payload(record, artifacts=artifacts)
    if not url or not token or payload is None:
        return False

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(_timeout_seconds())) as client:
            response = await client.post(
                url,
                headers={CALLBACK_HEADER_NAME: token},
                json=payload,
            )
            response.raise_for_status()
        return True
    except Exception:
        logger.warning("Failed to mirror DeerFlow run %s to Orpheus", record.run_id, exc_info=True)
        return False


def schedule_orpheus_mirror(record: RunRecord, *, artifacts: list[str] | None = None) -> None:
    if not _callback_url() or not _callback_token() or _orpheus_metadata(record) is None:
        return
    asyncio.create_task(post_orpheus_mirror(record, artifacts=artifacts))
