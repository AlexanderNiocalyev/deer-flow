"""Internal runtime lifecycle endpoints.

These endpoints are for trusted control-plane callers such as Orpheus. They do
not create or own sandboxes; they ask the active DeerFlow sandbox provider to
release DeerFlow-owned runtime resources for a thread.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.gateway.internal_auth import INTERNAL_SYSTEM_ROLE, get_trusted_internal_owner_user_id
from deerflow.sandbox.sandbox_provider import get_sandbox_provider

router = APIRouter(prefix="/api/internal/runtime", tags=["internal-runtime"])


class ThreadSandboxReleaseRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=120)


class ThreadSandboxReleaseResponse(BaseModel):
    status: Literal["released", "already_stopped", "not_found", "creating", "skipped"]
    provider: str
    thread_id: str
    sandbox_id: str | None = None
    vercel_sandbox_id: str | None = None
    message: str
    reason: str | None = None


def _require_internal_request(request: Request) -> str | None:
    user = getattr(getattr(request, "state", None), "user", None)
    if getattr(user, "system_role", None) != INTERNAL_SYSTEM_ROLE:
        raise HTTPException(status_code=403, detail="Internal runtime lifecycle API requires DeerFlow internal auth.")
    return get_trusted_internal_owner_user_id(request)


@router.post("/threads/{thread_id}/sandbox/release", response_model=ThreadSandboxReleaseResponse)
async def release_thread_sandbox(
    thread_id: str,
    body: ThreadSandboxReleaseRequest,
    request: Request,
) -> ThreadSandboxReleaseResponse:
    """Release the sandbox bound to a DeerFlow thread without creating one."""

    owner_user_id = _require_internal_request(request)
    provider = get_sandbox_provider()
    release_thread_async = getattr(provider, "release_thread_async", None)
    release_thread = getattr(provider, "release_thread", None)
    if not callable(release_thread_async) and not callable(release_thread):
        return ThreadSandboxReleaseResponse(
            status="skipped",
            provider=provider.__class__.__name__,
            thread_id=thread_id,
            message="Configured sandbox provider does not support thread-scoped release.",
            reason=body.reason,
        )

    if callable(release_thread_async):
        result = await release_thread_async(thread_id, user_id=owner_user_id, reason=body.reason)
    else:
        result = await asyncio.to_thread(
            release_thread,
            thread_id,
            user_id=owner_user_id,
            reason=body.reason,
        )
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="Sandbox provider returned an invalid lifecycle response.")

    payload: dict[str, Any] = {
        "status": result.get("status") or "skipped",
        "provider": result.get("provider") or provider.__class__.__name__,
        "thread_id": result.get("thread_id") or thread_id,
        "sandbox_id": result.get("sandbox_id"),
        "vercel_sandbox_id": result.get("vercel_sandbox_id"),
        "message": result.get("message") or "Sandbox lifecycle request completed.",
        "reason": result.get("reason") or body.reason,
    }
    return ThreadSandboxReleaseResponse.model_validate(payload)
