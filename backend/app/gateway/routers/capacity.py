"""Internal Gateway capacity endpoint for overflow routing decisions."""

from __future__ import annotations

import os
import shutil
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.gateway.deps import get_run_manager
from app.gateway.internal_auth import INTERNAL_SYSTEM_ROLE

router = APIRouter(prefix="/internal", tags=["internal-capacity"])


class CapacityMetrics(BaseModel):
    cpu: float | None = Field(default=None, ge=0.0, description="Best-effort CPU saturation estimate, 0-1.")
    load_1m_per_vcpu: float | None = Field(default=None, ge=0.0)
    memory: float | None = Field(default=None, ge=0.0, le=1.0)
    disk: float | None = Field(default=None, ge=0.0, le=1.0)
    active_runs: int = Field(ge=0)
    max_active_runs: int = Field(ge=1)
    pending_runs: int = Field(ge=0)
    oldest_pending_age_seconds: float = Field(ge=0.0)
    p95_run_start_delay_seconds: float | None = Field(default=None, ge=0.0)
    error_rate_5m: float | None = Field(default=None, ge=0.0)


class CapacityResponse(BaseModel):
    healthy: bool
    accept_new_runs: bool
    mode: Literal["accepting", "draining", "unhealthy"]
    score: float = Field(ge=0.0)
    reasons: list[str]
    metrics: CapacityMetrics


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _load_1m_per_vcpu() -> float | None:
    if not hasattr(os, "getloadavg"):
        return None
    try:
        load_1m = os.getloadavg()[0]
    except OSError:
        return None
    vcpus = os.cpu_count() or 1
    return max(0.0, load_1m / vcpus)


def _memory_used_ratio() -> float | None:
    meminfo: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                value = rest.strip().split()[0]
                meminfo[key] = int(value)
    except (FileNotFoundError, OSError, ValueError, IndexError):
        return None

    total = meminfo.get("MemTotal")
    available = meminfo.get("MemAvailable")
    if not total or available is None:
        return None
    return min(1.0, max(0.0, 1.0 - (available / total)))


def _disk_used_ratio(path: str = "/") -> float | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    if usage.total <= 0:
        return None
    return min(1.0, max(0.0, usage.used / usage.total))


async def _run_snapshot(request: Request) -> dict[str, Any]:
    run_manager = get_run_manager(request)
    snapshot = getattr(run_manager, "capacity_snapshot", None)
    if not callable(snapshot):
        return {
            "active_runs": 0,
            "pending_runs": 0,
            "oldest_pending_age_seconds": 0.0,
        }
    return await snapshot()


def _require_internal_request(request: Request) -> None:
    user = getattr(getattr(request, "state", None), "user", None)
    if getattr(user, "system_role", None) != INTERNAL_SYSTEM_ROLE:
        raise HTTPException(status_code=403, detail="Capacity API requires DeerFlow internal auth.")


def _score(*values: float | None) -> float:
    return max((value for value in values if value is not None), default=0.0)


@router.get("/capacity", response_model=CapacityResponse)
async def get_capacity(request: Request) -> CapacityResponse:
    """Return whether this gateway should accept new DeerFlow runs.

    Routers should use ``accept_new_runs`` as the routing contract. Individual
    metrics are diagnostic and intentionally conservative until staging load
    tests calibrate machine-specific thresholds.
    """

    _require_internal_request(request)

    max_active_runs = _int_env("DEERFLOW_CAPACITY_MAX_ACTIVE_RUNS", 2)
    memory_soft = _float_env("DEERFLOW_CAPACITY_MEMORY_SOFT", 0.75)
    memory_hard = _float_env("DEERFLOW_CAPACITY_MEMORY_HARD", 0.90)
    load_soft = _float_env("DEERFLOW_CAPACITY_LOAD_1M_PER_VCPU_SOFT", 0.80)
    disk_hard = _float_env("DEERFLOW_CAPACITY_DISK_HARD", 0.90)
    pending_age_soft = _float_env("DEERFLOW_CAPACITY_OLDEST_PENDING_AGE_SECONDS_SOFT", 20.0)

    runs = await _run_snapshot(request)
    active_runs = int(runs.get("active_runs") or 0)
    pending_runs = int(runs.get("pending_runs") or 0)
    oldest_pending_age_seconds = float(runs.get("oldest_pending_age_seconds") or 0.0)

    load_1m_per_vcpu = _load_1m_per_vcpu()
    memory = _memory_used_ratio()
    disk = _disk_used_ratio()
    # Without psutil, sustained load per vCPU is the least misleading cheap
    # CPU proxy. Clamp it for the 0-1 saturation-shaped field.
    cpu = min(load_1m_per_vcpu, 1.0) if load_1m_per_vcpu is not None else None

    reasons: list[str] = []
    healthy = True

    if memory is not None and memory >= memory_hard:
        healthy = False
        reasons.append("memory >= hard threshold")
    if disk is not None and disk >= disk_hard:
        healthy = False
        reasons.append("disk >= hard threshold")

    if active_runs >= max_active_runs:
        reasons.append("active_runs >= max_active_runs")
    if pending_runs > 0:
        reasons.append("pending_runs > 0")
    if pending_runs > 0 and oldest_pending_age_seconds >= pending_age_soft:
        reasons.append("oldest_pending_age_seconds >= threshold")
    if memory is not None and memory >= memory_soft:
        reasons.append("memory >= soft threshold")
    if load_1m_per_vcpu is not None and load_1m_per_vcpu >= load_soft:
        reasons.append("load_1m_per_vcpu >= threshold")

    accept_new_runs = healthy and not reasons
    mode: Literal["accepting", "draining", "unhealthy"]
    if not healthy:
        mode = "unhealthy"
    elif accept_new_runs:
        mode = "accepting"
    else:
        mode = "draining"

    capacity_score = _score(
        (active_runs + pending_runs) / max_active_runs,
        (oldest_pending_age_seconds / pending_age_soft) if pending_age_soft > 0 and pending_runs > 0 else None,
        (memory / memory_soft) if memory is not None and memory_soft > 0 else None,
        (load_1m_per_vcpu / load_soft) if load_1m_per_vcpu is not None and load_soft > 0 else None,
        (disk / disk_hard) if disk is not None and disk_hard > 0 else None,
    )

    return CapacityResponse(
        healthy=healthy,
        accept_new_runs=accept_new_runs,
        mode=mode,
        score=round(capacity_score, 4),
        reasons=reasons,
        metrics=CapacityMetrics(
            cpu=cpu,
            load_1m_per_vcpu=load_1m_per_vcpu,
            memory=memory,
            disk=disk,
            active_runs=active_runs,
            max_active_runs=max_active_runs,
            pending_runs=pending_runs,
            oldest_pending_age_seconds=oldest_pending_age_seconds,
            p95_run_start_delay_seconds=None,
            error_rate_5m=None,
        ),
    )
