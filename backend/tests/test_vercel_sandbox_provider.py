from __future__ import annotations

import json
import shlex
from types import SimpleNamespace
from typing import Any

from deerflow.community.vercel_sandbox.vercel_sandbox import VercelSandbox
from deerflow.config.paths import Paths


class FakeCommandFinished:
    def __init__(self, output: str = "", exit_code: int = 0):
        self._output = output
        self.exit_code = exit_code

    def output(self, stream: str = "both") -> str:
        return self._output


class FakeVercelClient:
    def __init__(self, sandbox_id: str):
        self.sandbox_id = sandbox_id
        self.files: dict[str, bytes] = {}
        self.commands: list[tuple[str, list[str] | None, str | None]] = []
        self.dirs: set[str] = set()
        self.stopped = False
        self.closed = False
        self.client = SimpleNamespace(close=self._close)

    def _close(self) -> None:
        self.closed = True

    def run_command(
        self,
        cmd: str,
        args: list[str] | None = None,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        sudo: bool = False,
    ) -> FakeCommandFinished:
        self.commands.append((cmd, args, cwd))
        command = args[1] if cmd == "bash" and args and len(args) >= 2 and args[0] == "-lc" else cmd
        if command.startswith("mkdir -p "):
            for path in shlex.split(command)[2:]:
                self.dirs.add(path)
            return FakeCommandFinished()
        if command.startswith("find ") and " -type f -print" in command:
            return FakeCommandFinished("\n".join(sorted(self.files)))
        if command.startswith("find ") and " -maxdepth " in command:
            return FakeCommandFinished("\n".join(sorted(self.files)))
        return FakeCommandFinished()

    def mk_dir(self, path: str, *, cwd: str | None = None) -> None:
        self.dirs.add(path)

    def write_files(self, files: list[dict[str, Any]]) -> None:
        for file in files:
            self.files[file["path"]] = file["content"]

    def read_file(self, path: str, *, cwd: str | None = None) -> bytes | None:
        return self.files.get(path)

    def iter_file(self, path: str, *, cwd: str | None = None, chunk_size: int = 65536):
        data = self.files[path]
        for offset in range(0, len(data), chunk_size):
            yield data[offset : offset + chunk_size]

    def stop(self, *, blocking: bool = False) -> None:
        self.stopped = True


def test_vercel_sandbox_mirrors_user_data_writes_to_host(tmp_path):
    paths = Paths(base_dir=tmp_path)
    paths.ensure_thread_dirs("thread-1", user_id="user-1")
    client = FakeVercelClient("sbx_1")
    sandbox = VercelSandbox(
        id="vercel-thread",
        client=client,
        thread_id="thread-1",
        user_id="user-1",
        paths=paths,
    )

    sandbox.write_file("/mnt/user-data/outputs/report.md", "hello")
    sandbox.write_file("/mnt/user-data/outputs/report.md", " world", append=True)

    host_file = paths.sandbox_outputs_dir("thread-1", user_id="user-1") / "report.md"
    assert client.files["/mnt/user-data/outputs/report.md"] == b"hello world"
    assert host_file.read_text(encoding="utf-8") == "hello world"


def test_vercel_provider_persists_mapping_stops_and_resumes(tmp_path, monkeypatch):
    import deerflow.community.vercel_sandbox.vercel_sandbox as sandbox_mod
    import deerflow.community.vercel_sandbox.vercel_sandbox_provider as provider_mod

    paths = Paths(base_dir=tmp_path)
    paths.ensure_thread_dirs("thread-2", user_id="user-2")
    (paths.sandbox_uploads_dir("thread-2", user_id="user-2") / "input.txt").write_text(
        "uploaded",
        encoding="utf-8",
    )

    sandbox_cfg = SimpleNamespace(
        environment={"API_KEY": "$TEST_API_KEY"},
        vercel_environment={"RUNTIME_ENV": "test"},
        vercel_vcpus=2,
        vercel_memory_mb=4096,
        vercel_runtime="python3.13",
        vercel_stop_on_release=True,
    )
    monkeypatch.setenv("TEST_API_KEY", "secret")
    monkeypatch.setattr(provider_mod, "get_app_config", lambda: SimpleNamespace(sandbox=sandbox_cfg))
    monkeypatch.setattr(provider_mod, "get_paths", lambda: paths)
    monkeypatch.setattr(sandbox_mod, "get_paths", lambda: paths)
    monkeypatch.setattr(provider_mod.VercelSandboxProvider, "_register_signal_handlers", lambda self: None)

    remote_clients: dict[str, FakeVercelClient] = {}
    created: list[str] = []
    resumed: list[str] = []

    def fake_create(self):
        remote_id = f"sbx_{len(created) + 1}"
        created.append(remote_id)
        client = FakeVercelClient(remote_id)
        remote_clients[remote_id] = client
        return client

    def fake_get(self, vercel_sandbox_id: str):
        resumed.append(vercel_sandbox_id)
        return remote_clients[vercel_sandbox_id]

    monkeypatch.setattr(provider_mod.VercelSandboxProvider, "_create_vercel_sandbox", fake_create)
    monkeypatch.setattr(provider_mod.VercelSandboxProvider, "_get_vercel_sandbox", fake_get)

    provider = provider_mod.VercelSandboxProvider()

    sandbox_id = provider.acquire("thread-2", user_id="user-2")
    sandbox = provider.get(sandbox_id)
    assert isinstance(sandbox, VercelSandbox)

    client = remote_clients["sbx_1"]
    assert client.files["/mnt/user-data/uploads/input.txt"] == b"uploaded"

    record_path = paths.thread_dir("thread-2", user_id="user-2") / f"{sandbox_id}.vercel-sandbox.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["sandbox_id"] == sandbox_id
    assert record["vercel_sandbox_id"] == "sbx_1"

    client.files["/mnt/user-data/outputs/result.txt"] = b"done"
    provider.release(sandbox_id)

    assert client.stopped is True
    assert client.closed is True
    assert provider.get(sandbox_id) is None
    assert (paths.sandbox_outputs_dir("thread-2", user_id="user-2") / "result.txt").read_text(encoding="utf-8") == "done"

    reacquired_id = provider.acquire("thread-2", user_id="user-2")

    assert reacquired_id == sandbox_id
    assert created == ["sbx_1"]
    assert resumed == ["sbx_1"]
