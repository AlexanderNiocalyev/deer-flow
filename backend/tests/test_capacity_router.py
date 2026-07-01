from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.gateway.auth_middleware import AuthMiddleware
from app.gateway.internal_auth import create_internal_auth_headers
from app.gateway.routers import capacity
from deerflow.runtime import RunManager
from deerflow.runtime.runs.schemas import RunStatus


class _FakeRunManager:
    def __init__(
        self,
        *,
        active_runs: int = 0,
        pending_runs: int = 0,
        oldest_pending_age_seconds: float = 0.0,
    ) -> None:
        self._snapshot = {
            "active_runs": active_runs,
            "pending_runs": pending_runs,
            "oldest_pending_age_seconds": oldest_pending_age_seconds,
        }

    async def capacity_snapshot(self):
        return self._snapshot


def _make_app(run_manager: _FakeRunManager) -> FastAPI:
    app = FastAPI()
    app.state.run_manager = run_manager
    app.add_middleware(AuthMiddleware)
    app.include_router(capacity.router)
    return app


def _patch_system_metrics(monkeypatch, *, load=0.2, memory=0.4, disk=0.3) -> None:
    monkeypatch.setattr(capacity, "_load_1m_per_vcpu", lambda: load)
    monkeypatch.setattr(capacity, "_memory_used_ratio", lambda: memory)
    monkeypatch.setattr(capacity, "_disk_used_ratio", lambda: disk)


def test_capacity_requires_internal_auth(monkeypatch):
    _patch_system_metrics(monkeypatch)
    client = TestClient(_make_app(_FakeRunManager()))

    response = client.get("/internal/capacity")

    assert response.status_code == 401


def test_capacity_accepts_when_under_thresholds(monkeypatch):
    _patch_system_metrics(monkeypatch, load=0.2, memory=0.4, disk=0.3)
    client = TestClient(_make_app(_FakeRunManager(active_runs=1)))

    response = client.get("/internal/capacity", headers=create_internal_auth_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["healthy"] is True
    assert body["accept_new_runs"] is True
    assert body["mode"] == "accepting"
    assert body["reasons"] == []
    assert body["metrics"]["active_runs"] == 1
    assert body["metrics"]["max_active_runs"] == 2


def test_capacity_drains_when_run_slots_are_full(monkeypatch):
    _patch_system_metrics(monkeypatch, load=0.2, memory=0.4, disk=0.3)
    client = TestClient(_make_app(_FakeRunManager(active_runs=2)))

    response = client.get("/internal/capacity", headers=create_internal_auth_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["healthy"] is True
    assert body["accept_new_runs"] is False
    assert body["mode"] == "draining"
    assert "active_runs >= max_active_runs" in body["reasons"]


def test_capacity_marks_unhealthy_on_hard_memory_threshold(monkeypatch):
    _patch_system_metrics(monkeypatch, load=0.2, memory=0.95, disk=0.3)
    client = TestClient(_make_app(_FakeRunManager()))

    response = client.get("/internal/capacity", headers=create_internal_auth_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["healthy"] is False
    assert body["accept_new_runs"] is False
    assert body["mode"] == "unhealthy"
    assert "memory >= hard threshold" in body["reasons"]


@pytest.mark.asyncio
async def test_run_manager_capacity_snapshot_counts_local_inflight_runs():
    manager = RunManager()
    await manager.create("thread-pending")
    running = await manager.create("thread-running")
    finalizing = await manager.create("thread-finalizing")
    finished = await manager.create("thread-finished")
    await manager.set_status(running.run_id, RunStatus.running)
    await manager.set_status(finalizing.run_id, RunStatus.interrupted)
    await manager.set_status(finished.run_id, RunStatus.success)
    await manager.set_finalizing(finalizing.run_id, True)

    snapshot = await manager.capacity_snapshot()

    assert snapshot["pending_runs"] == 1
    assert snapshot["active_runs"] == 2
    assert snapshot["oldest_pending_age_seconds"] >= 0
