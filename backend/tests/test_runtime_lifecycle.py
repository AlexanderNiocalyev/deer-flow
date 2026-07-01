from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.auth_middleware import AuthMiddleware
from app.gateway.internal_auth import create_internal_auth_headers
from app.gateway.routers import runtime_lifecycle


def _make_app(provider, monkeypatch):
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(runtime_lifecycle.router)
    monkeypatch.setattr(runtime_lifecycle, "get_sandbox_provider", lambda: provider)
    return app


def test_runtime_lifecycle_requires_internal_auth(monkeypatch):
    class FakeProvider:
        pass

    monkeypatch.setenv("DEER_FLOW_AUTH_DISABLED", "1")
    with TestClient(_make_app(FakeProvider(), monkeypatch)) as client:
        response = client.post(
            "/api/internal/runtime/threads/thread-1/sandbox/release",
            json={"reason": "idle_reaper"},
        )

    assert response.status_code == 403


def test_runtime_lifecycle_releases_thread_with_owner_header(monkeypatch):
    calls: list[dict] = []

    class FakeProvider:
        def release_thread(self, thread_id: str, *, user_id: str | None = None, reason: str | None = None):
            calls.append({"thread_id": thread_id, "user_id": user_id, "reason": reason})
            return {
                "status": "released",
                "provider": "vercel",
                "thread_id": thread_id,
                "sandbox_id": "vercel-abc",
                "vercel_sandbox_id": "sbx_abc",
                "message": "released",
                "reason": reason,
            }

    monkeypatch.delenv("DEER_FLOW_AUTH_DISABLED", raising=False)
    with TestClient(_make_app(FakeProvider(), monkeypatch)) as client:
        response = client.post(
            "/api/internal/runtime/threads/thread-1/sandbox/release",
            headers=create_internal_auth_headers(owner_user_id="user-1"),
            json={"reason": "idle_reaper"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "released",
        "provider": "vercel",
        "thread_id": "thread-1",
        "sandbox_id": "vercel-abc",
        "vercel_sandbox_id": "sbx_abc",
        "message": "released",
        "reason": "idle_reaper",
    }
    assert calls == [{"thread_id": "thread-1", "user_id": "user-1", "reason": "idle_reaper"}]


def test_runtime_lifecycle_skips_provider_without_thread_release(monkeypatch):
    class FakeProvider:
        pass

    monkeypatch.delenv("DEER_FLOW_AUTH_DISABLED", raising=False)
    with TestClient(_make_app(FakeProvider(), monkeypatch)) as client:
        response = client.post(
            "/api/internal/runtime/threads/thread-1/sandbox/release",
            headers=create_internal_auth_headers(owner_user_id="user-1"),
            json={"reason": "idle_reaper"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "skipped"
