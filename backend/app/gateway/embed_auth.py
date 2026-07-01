"""Signed embed-token authentication for Orpheus-hosted DeerFlow workspaces."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from fastapi import Request

EMBED_AUTH_HEADER_NAME = "X-DeerFlow-Embed-Token"
EMBED_AUTH_SECRET_ENV = "DEERFLOW_EMBED_TOKEN_SECRET"

_MAX_TOKEN_BYTES = 4096
_ALLOWED_CLOCK_SKEW_SECONDS = 30
_THREAD_PATH_RE = re.compile(r"^/api/(?:langgraph/)?threads/([^/?#]+)(?:/|$)")
_NON_THREAD_SEGMENTS = {"search"}


class EmbedTokenError(ValueError):
    """Raised when a signed embed token is missing, malformed, or invalid."""


@dataclass(frozen=True)
class EmbedTokenPayload:
    sub: str
    thread_id: str
    session_id: str | None
    workspace_id: str | None
    exp: int
    iat: int


def _base64_url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except Exception as exc:
        raise EmbedTokenError("Malformed embed token encoding") from exc


def _base64_url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _secret() -> str:
    return os.environ.get(EMBED_AUTH_SECRET_ENV, "").strip()


def _signature(secret: str, encoded_payload: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256).digest()
    return _base64_url_encode(digest)


def create_embed_token(payload: dict[str, Any], *, secret: str | None = None) -> str:
    """Create a signed embed token.

    Production tokens are minted by Orpheus. This helper exists for regression
    tests and local tooling so the verifier and signer stay byte-compatible.
    """
    actual_secret = (secret or _secret()).strip()
    if not actual_secret:
        raise EmbedTokenError(f"{EMBED_AUTH_SECRET_ENV} is not configured")
    encoded_payload = _base64_url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{encoded_payload}.{_signature(actual_secret, encoded_payload)}"


def verify_embed_token(raw_token: str | None, *, secret: str | None = None, now: int | None = None) -> EmbedTokenPayload:
    actual_secret = (secret or _secret()).strip()
    if not actual_secret:
        raise EmbedTokenError(f"{EMBED_AUTH_SECRET_ENV} is not configured")
    if not raw_token:
        raise EmbedTokenError("Embed token is missing")
    if len(raw_token.encode("utf-8")) > _MAX_TOKEN_BYTES:
        raise EmbedTokenError("Embed token is too large")

    encoded_payload, sep, encoded_signature = raw_token.partition(".")
    if not sep or not encoded_payload or not encoded_signature:
        raise EmbedTokenError("Malformed embed token")

    expected_signature = _signature(actual_secret, encoded_payload)
    if not hmac.compare_digest(expected_signature, encoded_signature):
        raise EmbedTokenError("Invalid embed token signature")

    try:
        data = json.loads(_base64_url_decode(encoded_payload).decode("utf-8"))
    except Exception as exc:
        raise EmbedTokenError("Malformed embed token payload") from exc
    if not isinstance(data, dict):
        raise EmbedTokenError("Embed token payload must be an object")

    if data.get("iss") != "orpheus" or data.get("aud") != "deerflow":
        raise EmbedTokenError("Embed token issuer or audience is invalid")

    current = int(time.time() if now is None else now)
    try:
        exp = int(data.get("exp") or 0)
        iat = int(data.get("iat") or 0)
    except (TypeError, ValueError) as exc:
        raise EmbedTokenError("Embed token timestamps are invalid") from exc
    if exp <= 0 or iat <= 0:
        raise EmbedTokenError("Embed token missing timestamps")
    if exp < current - _ALLOWED_CLOCK_SKEW_SECONDS:
        raise EmbedTokenError("Embed token has expired")
    if iat > current + _ALLOWED_CLOCK_SKEW_SECONDS:
        raise EmbedTokenError("Embed token was issued in the future")

    sub = str(data.get("sub") or "").strip()
    thread_id = str(data.get("thread_id") or "").strip()
    if not sub or not thread_id:
        raise EmbedTokenError("Embed token missing subject or thread_id")

    return EmbedTokenPayload(
        sub=sub,
        thread_id=thread_id,
        session_id=str(data.get("session_id") or "") or None,
        workspace_id=str(data.get("workspace_id") or "") or None,
        exp=exp,
        iat=iat,
    )


def _thread_id_from_path(path: str) -> str | None:
    match = _THREAD_PATH_RE.match(path)
    if not match:
        return None
    thread_id = match.group(1)
    if thread_id in _NON_THREAD_SEGMENTS:
        return None
    return thread_id


def verify_embed_request(request: Request) -> EmbedTokenPayload:
    payload = verify_embed_token(request.headers.get(EMBED_AUTH_HEADER_NAME))
    path_thread_id = _thread_id_from_path(request.url.path)
    if path_thread_id is not None and path_thread_id != payload.thread_id:
        raise EmbedTokenError("Embed token is not valid for this thread")
    return payload


def get_embed_user_from_request(request: Request):
    payload = verify_embed_request(request)
    safe_sub = re.sub(r"[^a-zA-Z0-9_.+-]+", "-", payload.sub).strip("-") or "user"
    return SimpleNamespace(
        id=payload.sub,
        email=f"orpheus+{safe_sub}@embed.local",
        password_hash=None,
        system_role="user",
        needs_setup=False,
        token_version=0,
        oauth_provider="orpheus_embed",
        embed_thread_id=payload.thread_id,
        embed_session_id=payload.session_id,
        embed_workspace_id=payload.workspace_id,
    )
