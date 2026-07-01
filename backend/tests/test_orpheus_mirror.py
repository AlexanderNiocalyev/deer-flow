import pytest

from deerflow.integrations.orpheus_mirror import build_orpheus_mirror_payload
from deerflow.runtime.runs.manager import RunManager, RunRecord
from deerflow.runtime.runs.schemas import DisconnectMode, RunStatus


def test_build_orpheus_mirror_payload_uses_embed_metadata(monkeypatch):
    monkeypatch.setenv("DEERFLOW_PUBLIC_BASE_URL", "https://deerflow.example")
    record = RunRecord(
        run_id="run-1",
        thread_id="thread-1",
        assistant_id="lead_agent",
        status=RunStatus.success,
        on_disconnect=DisconnectMode.cancel,
        metadata={
            "orpheus_session_id": "agws_1",
            "orpheus_workspace_id": "ws_1",
        },
        created_at="2026-07-01T10:00:00+00:00",
        updated_at="2026-07-01T10:01:00+00:00",
        model_name="gpt-test",
    )

    payload = build_orpheus_mirror_payload(record, artifacts=["/mnt/user-data/outputs/report.md"])

    assert payload is not None
    assert payload["workspace_id"] == "ws_1"
    assert payload["session_id"] == "agws_1"
    assert payload["thread_id"] == "thread-1"
    assert payload["run"] == {
        "id": "run-1",
        "status": "success",
        "started_at": "2026-07-01T10:00:00+00:00",
        "finished_at": "2026-07-01T10:01:00+00:00",
        "error": None,
        "metadata": {
            "assistant_id": "lead_agent",
            "model_name": "gpt-test",
        },
    }
    assert payload["events"][0]["event_type"] == "deerflow.run.success"
    assert payload["artifacts"] == [
        {
            "id": "run-1:/mnt/user-data/outputs/report.md",
            "run_id": "run-1",
            "path": "/mnt/user-data/outputs/report.md",
            "object_url": "https://deerflow.example/api/threads/thread-1/artifacts/mnt/user-data/outputs/report.md?download=true",
        }
    ]


def test_build_orpheus_mirror_payload_skips_non_embed_runs():
    record = RunRecord(
        run_id="run-1",
        thread_id="thread-1",
        assistant_id="lead_agent",
        status=RunStatus.running,
        on_disconnect=DisconnectMode.cancel,
    )

    assert build_orpheus_mirror_payload(record) is None


@pytest.mark.asyncio
async def test_run_manager_status_change_schedules_orpheus_mirror(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_schedule(record, *, artifacts=None):
        calls.append((record.run_id, record.status.value))

    monkeypatch.setattr(
        "deerflow.integrations.orpheus_mirror.schedule_orpheus_mirror",
        fake_schedule,
    )
    manager = RunManager()
    record = await manager.create(
        "thread-1",
        "lead_agent",
        metadata={
            "orpheus_session_id": "agws_1",
            "orpheus_workspace_id": "ws_1",
        },
    )

    await manager.set_status(record.run_id, RunStatus.running)

    assert calls == [(record.run_id, "running")]
