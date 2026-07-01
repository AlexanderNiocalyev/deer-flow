from __future__ import annotations

import base64
import errno
import json
import logging
import os
import posixpath
import re
import shlex
import threading
from pathlib import Path
from typing import Any

from deerflow.config.paths import VIRTUAL_PATH_PREFIX, Paths, get_paths
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.search import (
    DEFAULT_LINE_SUMMARY_LENGTH,
    DEFAULT_MAX_FILE_SIZE_BYTES,
    IGNORE_PATTERNS,
    GrepMatch,
    should_ignore_name,
    should_ignore_path,
    truncate_line,
)

logger = logging.getLogger(__name__)

_MAX_DOWNLOAD_SIZE = 100 * 1024 * 1024
_DEFAULT_CWD = f"{VIRTUAL_PATH_PREFIX}/workspace"


def _is_under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


def _normalize_sandbox_path(path: str) -> str:
    normalised = path.replace("\\", "/")
    if not normalised.startswith("/"):
        raise PermissionError(f"Sandbox path must be absolute: {path}")
    if any(segment == ".." for segment in normalised.split("/")):
        raise PermissionError(f"Access denied: path traversal detected in '{path}'")
    return posixpath.normpath(normalised)


def _require_user_data_path(path: str) -> str:
    normalised = _normalize_sandbox_path(path)
    if not _is_under(normalised, VIRTUAL_PATH_PREFIX):
        raise PermissionError(f"Access denied: path must be under '{VIRTUAL_PATH_PREFIX}': '{path}'")
    return normalised


class VercelSandbox(Sandbox):
    """Sandbox implementation backed by Vercel Sandbox.

    Vercel does not mount DeerFlow's host-side thread directories, so this class
    mirrors `/mnt/user-data/*` writes back to the host thread data directory.
    That keeps existing artifact delivery and `present_files` behavior working
    with remote Vercel execution.
    """

    def __init__(
        self,
        id: str,
        client: Any,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
        paths: Paths | None = None,
        cwd: str = _DEFAULT_CWD,
        max_download_bytes: int = _MAX_DOWNLOAD_SIZE,
        max_sync_file_bytes: int = _MAX_DOWNLOAD_SIZE,
    ):
        super().__init__(id)
        self._client = client
        self._thread_id = thread_id
        self._user_id = user_id
        self._paths = paths or get_paths()
        self._cwd = cwd
        self._max_download_bytes = max_download_bytes
        self._max_sync_file_bytes = max_sync_file_bytes
        self._lock = threading.RLock()
        self._closed = False

    @property
    def vercel_sandbox_id(self) -> str:
        sandbox_id = getattr(self._client, "sandbox_id", None)
        if sandbox_id:
            return str(sandbox_id)

        sandbox = getattr(self._client, "sandbox", None)
        sandbox_id = getattr(sandbox, "id", None)
        if sandbox_id:
            return str(sandbox_id)

        return ""

    def close(self) -> None:
        """Close the host-side SDK client, if the SDK exposes a close hook."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            client = self._client
            self._client = None

        sdk_client = getattr(client, "client", None)
        close = getattr(sdk_client, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                logger.warning("Failed to close Vercel sandbox client for %s: %s", self.id, exc)

    def stop(self, *, blocking: bool = False) -> None:
        """Stop the remote Vercel sandbox.

        Persistent Vercel sandboxes snapshot on stop and can be resumed later by
        retrieving the same Vercel sandbox id.
        """
        with self._lock:
            self._sdk().stop(blocking=blocking)

    def bootstrap(self) -> None:
        """Create the standard DeerFlow directories inside the Vercel sandbox."""
        quoted_prefix = shlex.quote(VIRTUAL_PATH_PREFIX)
        output = self._run_bash(
            f"mkdir -p {quoted_prefix}/workspace {quoted_prefix}/uploads {quoted_prefix}/outputs /mnt/acp-workspace",
            cwd="/",
        )
        if output.startswith("Error:"):
            raise RuntimeError(output)

    def execute_command(self, command: str) -> str:
        """Execute a bash command in the Vercel sandbox."""
        return self._run_bash(command, cwd=self._cwd)

    def _run_bash(self, command: str, *, cwd: str | None) -> str:
        with self._lock:
            try:
                result = self._sdk().run_command("bash", ["-lc", command], cwd=cwd)
                output = result.output("both") if hasattr(result, "output") else ""
                if output:
                    return output
                exit_code = getattr(result, "exit_code", 0)
                if exit_code:
                    return f"Error: command exited with status {exit_code}"
                return "(no output)"
            except Exception as exc:
                logger.error("Failed to execute command in Vercel sandbox %s: %s", self.id, exc)
                return f"Error: {exc}"

    def read_file(self, path: str) -> str:
        """Read UTF-8 text content from `/mnt/user-data` inside the sandbox."""
        path = _require_user_data_path(path)
        with self._lock:
            data = self._read_file_bytes_locked(path)
        return data.decode("utf-8")

    def download_file(self, path: str) -> bytes:
        """Download file bytes from `/mnt/user-data` inside the sandbox."""
        path = _require_user_data_path(path)
        with self._lock:
            chunks: list[bytes] = []
            total = 0
            try:
                for chunk in self._sdk().iter_file(path, chunk_size=65536):
                    total += len(chunk)
                    if total > self._max_download_bytes:
                        raise OSError(
                            errno.EFBIG,
                            f"File exceeds maximum download size of {self._max_download_bytes} bytes",
                            path,
                        )
                    chunks.append(chunk)
            except OSError:
                raise
            except Exception as exc:
                logger.error("Failed to download file %s from Vercel sandbox %s: %s", path, self.id, exc)
                raise OSError(f"Failed to download file '{path}' from Vercel sandbox: {exc}") from exc
            return b"".join(chunks)

    def list_dir(self, path: str, max_depth: int = 2) -> list[str]:
        """List files and directories under a `/mnt/user-data` path."""
        path = _require_user_data_path(path)
        max_depth = max(0, int(max_depth))
        command = (
            f"find {shlex.quote(path)} -maxdepth {max_depth} "
            r"\( -type f -o -type d \) -print 2>/dev/null | head -500"
        )
        output = self.execute_command(command)
        if output.startswith("Error:"):
            return []
        return [line.strip() for line in output.splitlines() if line.strip()]

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        """Write text content to a `/mnt/user-data` path."""
        self._write_bytes(path, content.encode("utf-8"), append=append)

    def update_file(self, path: str, content: bytes) -> None:
        """Write binary content to a `/mnt/user-data` path."""
        self._write_bytes(path, content, append=False)

    def glob(
        self,
        path: str,
        pattern: str,
        *,
        include_dirs: bool = False,
        max_results: int = 200,
    ) -> tuple[list[str], bool]:
        path = _require_user_data_path(path)
        payload = {
            "root": path,
            "pattern": pattern,
            "include_dirs": include_dirs,
            "max_results": max_results,
            "ignore_patterns": IGNORE_PATTERNS,
        }
        data = self._run_python_json(_GLOB_SCRIPT, payload)
        matches = data.get("matches", []) if isinstance(data, dict) else []
        filtered = [match for match in matches if isinstance(match, str) and not should_ignore_path(match)]
        truncated = bool(data.get("truncated")) if isinstance(data, dict) else False
        if len(filtered) > max_results:
            truncated = True
        return filtered[:max_results], truncated

    def grep(
        self,
        path: str,
        pattern: str,
        *,
        glob: str | None = None,
        literal: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> tuple[list[GrepMatch], bool]:
        path = _require_user_data_path(path)
        regex_source = re.escape(pattern) if literal else pattern
        re.compile(regex_source, 0 if case_sensitive else re.IGNORECASE)
        payload = {
            "root": path,
            "regex": regex_source,
            "glob": glob,
            "case_sensitive": case_sensitive,
            "max_results": max_results,
            "max_file_size": DEFAULT_MAX_FILE_SIZE_BYTES,
            "line_summary_length": DEFAULT_LINE_SUMMARY_LENGTH,
            "ignore_patterns": IGNORE_PATTERNS,
        }
        data = self._run_python_json(_GREP_SCRIPT, payload)
        raw_matches = data.get("matches", []) if isinstance(data, dict) else []
        matches: list[GrepMatch] = []
        for item in raw_matches:
            if not isinstance(item, dict):
                continue
            file_path = item.get("path")
            if not isinstance(file_path, str) or should_ignore_path(file_path):
                continue
            matches.append(
                GrepMatch(
                    path=file_path,
                    line_number=item.get("line_number") if isinstance(item.get("line_number"), int) else 0,
                    line=truncate_line(str(item.get("line", ""))),
                )
            )
            if len(matches) >= max_results:
                return matches, True
        truncated = bool(data.get("truncated")) if isinstance(data, dict) else False
        return matches, truncated

    def sync_from_host(self, *, include_outputs: bool = False) -> None:
        """Copy existing thread data from the host into the Vercel sandbox."""
        if self._thread_id is None:
            return

        roots: list[tuple[Path, str]] = [
            (
                self._paths.sandbox_work_dir(self._thread_id, user_id=self._user_id),
                f"{VIRTUAL_PATH_PREFIX}/workspace",
            ),
            (
                self._paths.sandbox_uploads_dir(self._thread_id, user_id=self._user_id),
                f"{VIRTUAL_PATH_PREFIX}/uploads",
            ),
        ]
        if include_outputs:
            roots.append(
                (
                    self._paths.sandbox_outputs_dir(self._thread_id, user_id=self._user_id),
                    f"{VIRTUAL_PATH_PREFIX}/outputs",
                )
            )

        for root, virtual_root in roots:
            if not root.exists():
                continue
            for current_root, dirs, files in os.walk(root):
                dirs[:] = [name for name in dirs if not should_ignore_name(name)]
                for name in files:
                    if should_ignore_name(name):
                        continue
                    host_path = Path(current_root) / name
                    if host_path.is_symlink():
                        continue
                    try:
                        if host_path.stat().st_size > self._max_sync_file_bytes:
                            continue
                        relative = host_path.relative_to(root).as_posix()
                        self.update_file(f"{virtual_root}/{relative}", host_path.read_bytes())
                    except OSError as exc:
                        logger.warning("Failed to sync host file %s to Vercel sandbox %s: %s", host_path, self.id, exc)

    def sync_to_host(self, *, subdirs: tuple[str, ...] = ("workspace", "outputs")) -> None:
        """Copy selected `/mnt/user-data` files from Vercel back to the host."""
        if self._thread_id is None:
            return

        roots = " ".join(shlex.quote(f"{VIRTUAL_PATH_PREFIX}/{subdir}") for subdir in subdirs)
        output = self.execute_command(f"find {roots} -type f -print 2>/dev/null")
        if output.startswith("Error:"):
            logger.warning("Failed to list Vercel sandbox files for sync: %s", output)
            return

        for line in output.splitlines():
            path = line.strip()
            if not path or should_ignore_path(path):
                continue
            try:
                data = self.download_file(path)
                self._mirror_user_data_path(path, data)
            except OSError as exc:
                logger.warning("Failed to sync Vercel sandbox file %s to host: %s", path, exc)

    def _sdk(self) -> Any:
        if self._client is None or self._closed:
            raise RuntimeError("Vercel sandbox client is closed")
        return self._client

    def _read_file_bytes_locked(self, path: str) -> bytes:
        data = self._sdk().read_file(path)
        if data is None:
            raise FileNotFoundError(path)
        return data

    def _write_bytes(self, path: str, content: bytes, *, append: bool = False) -> None:
        path = _require_user_data_path(path)
        with self._lock:
            data = content
            if append:
                try:
                    data = self._read_file_bytes_locked(path) + content
                except FileNotFoundError:
                    data = content
            parent = posixpath.dirname(path)
            if parent:
                self._sdk().mk_dir(parent)
            self._sdk().write_files([{"path": path, "content": data, "mode": 0o644}])
            self._mirror_user_data_path(path, data)

    def _mirror_user_data_path(self, path: str, content: bytes) -> None:
        if self._thread_id is None:
            return
        try:
            host_path = self._paths.resolve_virtual_path(self._thread_id, path, user_id=self._user_id)
        except ValueError:
            logger.warning("Skipping host mirror for non-user-data path %s", path)
            return

        host_path.parent.mkdir(parents=True, exist_ok=True)
        host_path.write_bytes(content)
        try:
            host_path.chmod(0o666)
        except OSError:
            logger.debug("Could not chmod mirrored Vercel sandbox file %s", host_path)

    def _run_python_json(self, script: str, payload: dict[str, Any]) -> dict[str, Any]:
        encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
        command = f"python3 - <<'PY'\nimport base64, json\nparams = json.loads(base64.b64decode({encoded!r}))\n{script}\nPY"
        output = self.execute_command(command)
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            logger.warning("Failed to parse Vercel sandbox JSON helper output: %s", output[:500])
            return {}
        return data if isinstance(data, dict) else {}


_GLOB_SCRIPT = r"""
import fnmatch
import os
from pathlib import PurePosixPath

ignore_patterns = params["ignore_patterns"]
exact_ignores = {pattern for pattern in ignore_patterns if not any(ch in pattern for ch in "*?[")}
glob_ignores = [pattern for pattern in ignore_patterns if any(ch in pattern for ch in "*?[")]


def should_ignore(name):
    return name in exact_ignores or any(fnmatch.fnmatch(name, pattern) for pattern in glob_ignores)


def path_matches(pattern, rel_path):
    path = PurePosixPath(rel_path)
    return path.match(pattern) or (pattern.startswith("**/") and path.match(pattern[3:]))


root = params["root"]
pattern = params["pattern"]
include_dirs = bool(params["include_dirs"])
max_results = int(params["max_results"])
matches = []
truncated = False

if os.path.isdir(root):
    for current_root, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if not should_ignore(name)]
        rel_dir = os.path.relpath(current_root, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")

        if include_dirs:
            for name in dirs:
                rel_path = f"{rel_dir}/{name}" if rel_dir else name
                if path_matches(pattern, rel_path):
                    matches.append(os.path.join(current_root, name).replace(os.sep, "/"))
                    if len(matches) >= max_results:
                        truncated = True
                        break
        if truncated:
            break

        for name in files:
            if should_ignore(name):
                continue
            rel_path = f"{rel_dir}/{name}" if rel_dir else name
            if path_matches(pattern, rel_path):
                matches.append(os.path.join(current_root, name).replace(os.sep, "/"))
                if len(matches) >= max_results:
                    truncated = True
                    break
        if truncated:
            break

print(json.dumps({"matches": matches, "truncated": truncated}))
"""


_GREP_SCRIPT = r"""
import fnmatch
import os
import re
from pathlib import PurePosixPath

ignore_patterns = params["ignore_patterns"]
exact_ignores = {pattern for pattern in ignore_patterns if not any(ch in pattern for ch in "*?[")}
glob_ignores = [pattern for pattern in ignore_patterns if any(ch in pattern for ch in "*?[")]


def should_ignore(name):
    return name in exact_ignores or any(fnmatch.fnmatch(name, pattern) for pattern in glob_ignores)


def path_matches(pattern, rel_path):
    path = PurePosixPath(rel_path)
    return path.match(pattern) or (pattern.startswith("**/") and path.match(pattern[3:]))


root = params["root"]
glob_pattern = params["glob"]
flags = 0 if params["case_sensitive"] else re.IGNORECASE
regex = re.compile(params["regex"], flags)
max_results = int(params["max_results"])
max_file_size = int(params["max_file_size"])
line_summary_length = int(params["line_summary_length"])
max_line_chars = line_summary_length * 10
matches = []
truncated = False

if os.path.isdir(root):
    for current_root, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if not should_ignore(name)]
        rel_dir = os.path.relpath(current_root, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")

        for name in files:
            if should_ignore(name):
                continue
            candidate = os.path.join(current_root, name)
            rel_path = f"{rel_dir}/{name}" if rel_dir else name
            if glob_pattern is not None and not path_matches(glob_pattern, rel_path):
                continue
            try:
                if os.path.islink(candidate) or os.path.getsize(candidate) > max_file_size:
                    continue
                with open(candidate, "rb") as sample:
                    if b"\0" in sample.read(8192):
                        continue
                with open(candidate, encoding="utf-8", errors="replace") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        if len(line) > max_line_chars:
                            continue
                        if regex.search(line):
                            clean_line = line.rstrip("\n\r")
                            if len(clean_line) > line_summary_length:
                                clean_line = clean_line[: line_summary_length - 3] + "..."
                            matches.append(
                                {
                                    "path": candidate.replace(os.sep, "/"),
                                    "line_number": line_number,
                                    "line": clean_line,
                                }
                            )
                            if len(matches) >= max_results:
                                truncated = True
                                break
                if truncated:
                    break
            except OSError:
                continue
        if truncated:
            break

print(json.dumps({"matches": matches, "truncated": truncated}))
"""
