"""Unix socket RPC/event client and external CLI adapters for Herdr."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import socket
import subprocess
import tempfile
import time
import tomllib
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import EXIT_CONFIG, EXIT_DOMAIN, EXIT_TEMP, CliError, DigestCursor

_EVENT_TYPES = (
    "workspace.created",
    "workspace.closed",
    "tab.created",
    "tab.closed",
    "tab.moved",
    "tab.renamed",
    "pane.created",
    "pane.closed",
    "pane.moved",
    "pane.exited",
    "pane.agent_detected",
    "pane.updated",
)


def resolve_socket_path(explicit_socket: str | None, env: Mapping[str, str]) -> str:
    """Resolve and validate the target Herdr Unix socket path."""
    if explicit_socket:
        return explicit_socket
    if env.get("HERDR_ENV") != "1":
        raise CliError(
            "NOT_IN_HERDR",
            "HERDR_ENV is not 1 and --socket was not supplied.",
            "Run this command inside a Herdr pane, or pass --socket <path>.",
            exit_code=EXIT_CONFIG,
        )
    sock = env.get("HERDR_SOCKET_PATH")
    if sock:
        return sock
    config_home = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return str(Path(config_home) / "herdr" / "herdr.sock")


class HerdrSocketClient:
    """Client for the Herdr Unix domain socket JSON-lines protocol."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path

    def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 5.0,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> dict[str, Any]:
        """Send one JSON-line request and return its result dictionary."""
        req_id = uuid.uuid4().hex
        wire = json.dumps({"id": req_id, "method": method, "params": params}).encode() + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(timeout)
                client.connect(self.socket_path)
                client.sendall(wire)
                line = _readline_bounded(client, max_bytes)
        except TimeoutError as exc:
            raise CliError(
                "HERDR_SOCKET_TIMEOUT",
                f"Timed out talking to Herdr socket ({method}).",
                "Verify the Herdr server is responsive with `herdr status` and retry.",
                exit_code=EXIT_TEMP,
                transient=True,
            ) from exc
        except OSError as exc:
            raise CliError(
                "HERDR_SOCKET_ERROR",
                f"Could not communicate with Herdr socket {self.socket_path}: {exc}",
                "Verify Herdr is running (`herdr status`) or pass --socket <path>.",
                exit_code=EXIT_TEMP,
                transient=True,
            ) from exc

        try:
            response = json.loads(line)
        except ValueError as exc:
            raise CliError(
                "HERDR_INVALID_RESPONSE",
                f"Herdr returned malformed JSON for {method}.",
                "Inspect `herdr status` and retry.",
                exit_code=EXIT_DOMAIN,
            ) from exc

        if not isinstance(response, dict) or response.get("id") != req_id:
            raise CliError(
                "HERDR_INVALID_RESPONSE",
                f"Unexpected response envelope from Herdr for {method}.",
                "Check `herdr status` for client/server compatibility.",
                exit_code=EXIT_DOMAIN,
            )
        if "error" in response:
            err = response["error"]
            err_code = err.get("code", "HERDR_API_ERROR") if isinstance(err, dict) else "ERROR"
            err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise CliError(
                "HERDR_API_ERROR",
                f"Herdr rejected {method} ({err_code}): {err_msg}",
                "Inspect the target ID or run `herdr status`.",
                exit_code=EXIT_DOMAIN,
                details={"herdr_code": err_code},
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise CliError(
                "HERDR_INVALID_RESPONSE",
                f"Herdr result for {method} is not a JSON object.",
                "Check `herdr status`.",
                exit_code=EXIT_DOMAIN,
            )
        return result

    def snapshot(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """Fetch the live session snapshot."""
        result = self.request("session.snapshot", {}, timeout=timeout)
        snap = result.get("snapshot")
        if not isinstance(snap, dict):
            raise CliError(
                "HERDR_INVALID_RESPONSE",
                "session.snapshot response is missing snapshot payload.",
                "Check `herdr api snapshot`.",
            )
        return snap

    def read_detection(self, pane_id: str, *, lines: int = 60, timeout: float = 3.0) -> str:
        """Read the detection bottom-buffer text for an agent pane."""
        read = self.read_pane(
            pane_id,
            has_agent=True,
            source="detection",
            lines=lines,
            timeout=timeout,
        )
        return str(read["text"])

    def read_pane(
        self,
        pane_id: str,
        *,
        has_agent: bool,
        source: str = "recent-unwrapped",
        lines: int = 40,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Read terminal output via agent.read or pane.read."""
        api_source = source.replace("-", "_")
        if has_agent:
            result = self.request(
                "agent.read",
                {"target": pane_id, "source": api_source, "lines": lines},
                timeout=timeout,
            )
        else:
            result = self.request(
                "pane.read",
                {"pane_id": pane_id, "source": api_source, "lines": lines},
                timeout=timeout,
            )
        read = result.get("read")
        if not isinstance(read, dict) or not isinstance(read.get("text"), str):
            raise CliError(
                "HERDR_INVALID_RESPONSE",
                f"Malformed read response for pane {pane_id}.",
                "Verify the pane still exists with `hcmd status`.",
            )
        if read.get("pane_id") != pane_id:
            raise CliError(
                "HERDR_INVALID_RESPONSE",
                f"Read response returned pane {read.get('pane_id')!r} instead of {pane_id!r}.",
                "Retry the peek command.",
            )
        return read

    def subscribe_events(
        self,
        pane_ids: Sequence[str],
        *,
        deadline: float,
        max_bytes: int = 4 * 1024 * 1024,
    ) -> Iterator[dict[str, Any]]:
        """Subscribe to Herdr workspace/tab/pane events until deadline."""
        subscriptions: list[dict[str, str]] = [{"type": kind} for kind in _EVENT_TYPES]
        for pid in sorted(set(pane_ids)):
            subscriptions.append({"type": "pane.agent_status_changed", "pane_id": pid})

        req_id = uuid.uuid4().hex
        wire = (
            json.dumps(
                {
                    "id": req_id,
                    "method": "events.subscribe",
                    "params": {"subscriptions": subscriptions},
                }
            ).encode()
            + b"\n"
        )
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        buf = bytearray()
        try:
            remaining = max(0.5, min(5.0, deadline - time.monotonic()))
            sock.settimeout(remaining)
            sock.connect(self.socket_path)
            sock.sendall(wire)

            ack_line, buf = _read_next_line(sock, buf, deadline=deadline, max_bytes=max_bytes)
            if ack_line is None:
                return
            ack = json.loads(ack_line)
            if (
                not isinstance(ack, dict)
                or ack.get("id") != req_id
                or ack.get("result", {}).get("type") != "subscription_started"
            ):
                raise CliError(
                    "HERDR_SUBSCRIBE_FAILED",
                    "Herdr did not acknowledge events.subscribe.",
                    "Check `herdr status` for socket API compatibility.",
                )

            sock.setblocking(False)
            while True:
                if time.monotonic() >= deadline:
                    return
                line, buf = _read_next_line(sock, buf, deadline=deadline, max_bytes=max_bytes)
                if line is None:
                    return
                msg = json.loads(line)
                if not isinstance(msg, dict):
                    continue
                if "error" in msg:
                    raise CliError(
                        "HERDR_EVENT_ERROR",
                        f"Herdr event stream error: {msg['error']}",
                        "Retry `wait`.",
                        exit_code=EXIT_TEMP,
                        transient=True,
                    )
                yield msg
        except OSError as exc:
            raise CliError(
                "HERDR_SOCKET_ERROR",
                f"Event stream failed on {self.socket_path}: {exc}",
                "Verify Herdr is running (`herdr status`) and retry.",
                exit_code=EXIT_TEMP,
                transient=True,
            ) from exc
        finally:
            try:
                sock.close()
            except OSError:
                pass


def _readline_bounded(sock: socket.socket, max_bytes: int) -> bytes:
    buf = bytearray()
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf.extend(chunk)
        newline_idx = buf.find(b"\n")
        if newline_idx != -1:
            return bytes(buf[:newline_idx])
        if len(buf) > max_bytes:
            raise CliError(
                "HERDR_OVERSIZED_RESPONSE",
                "Herdr socket response exceeded size limit.",
                "Inspect `herdr status`.",
            )
    if not buf:
        raise CliError(
            "HERDR_EMPTY_RESPONSE",
            "Herdr socket closed without sending a response.",
            "Verify Herdr is running (`herdr status`).",
            exit_code=EXIT_TEMP,
            transient=True,
        )
    return bytes(buf)


def _read_next_line(
    sock: socket.socket,
    buf: bytearray,
    *,
    deadline: float,
    max_bytes: int,
) -> tuple[bytes | None, bytearray]:
    while True:
        newline_idx = buf.find(b"\n")
        if newline_idx != -1:
            line = bytes(buf[:newline_idx])
            del buf[: newline_idx + 1]
            return line, buf

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, buf

        ready, _, _ = select.select([sock], [], [], min(1.0, remaining))
        if not ready:
            continue
        chunk = sock.recv(65536)
        if not chunk:
            raise CliError(
                "HERDR_DISCONNECTED",
                "Herdr event stream disconnected.",
                "Retry `wait`.",
                exit_code=EXIT_TEMP,
                transient=True,
            )
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise CliError(
                "HERDR_OVERSIZED_RESPONSE",
                "Herdr event payload exceeded size limit.",
                "Retry `wait`.",
            )


def _run_subprocess(
    args: Sequence[str],
    *,
    timeout: float,
    error_code: str,
    fix: str,
) -> str:
    try:
        proc = subprocess.run(
            list(args),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CliError(
            "DEPENDENCY_MISSING",
            f"Required executable {args[0]!r} was not found on PATH.",
            fix,
            exit_code=EXIT_CONFIG,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CliError(
            error_code,
            f"Command timed out after {timeout:.0f}s: {' '.join(args)}",
            fix,
            exit_code=EXIT_DOMAIN,
        ) from exc
    except OSError as exc:
        raise CliError(
            error_code,
            f"Failed to execute {args[0]}: {exc}",
            fix,
            exit_code=EXIT_DOMAIN,
        ) from exc

    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
        raise CliError(
            error_code,
            f"{args[0]} failed: {detail}",
            fix,
            exit_code=EXIT_DOMAIN,
        )
    return proc.stdout


def create_workspace_cwd(label: str, cwd: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """Create a Herdr workspace for a directory, preferring adjacent placement when valid."""
    if shutil.which("herdr-next-to-current"):
        proc = subprocess.run(
            [
                "herdr-next-to-current",
                "workspace",
                "--label",
                label,
                "--cwd",
                cwd,
                "--no-focus",
                "--json",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode == 0:
            try:
                payload = json.loads(proc.stdout)
                wid = payload.get("new_id")
                if isinstance(wid, str) and wid:
                    return {"workspace_id": wid, "label": label, "cwd": cwd}
            except ValueError:
                pass

    raw = _run_subprocess(
        ["herdr", "workspace", "create", "--cwd", cwd, "--label", label, "--no-focus"],
        timeout=timeout,
        error_code="WORKSPACE_CREATE_FAILED",
        fix="Verify the Herdr session is active and the directory exists.",
    )
    try:
        data = json.loads(raw)["result"]
        ws = data["workspace"]
        tab = data["tab"]
        pane = data["root_pane"]
        return {
            "workspace_id": ws["workspace_id"],
            "tab_id": tab["tab_id"],
            "pane_id": pane["pane_id"],
            "label": ws.get("label", label),
            "cwd": cwd,
        }
    except (ValueError, KeyError, TypeError) as exc:
        raise CliError(
            "WORKSPACE_CREATE_FAILED",
            "Unexpected JSON response from `herdr workspace create`.",
            "Run `hcmd status` to verify whether the workspace was created.",
        ) from exc


def create_workspace_cubrid_ticket(
    ticket: str,
    *,
    base: str | None = None,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """Create or reuse a CUBRID ticket worktree and its Herdr workspace."""
    args = ["cubrid-worktree", "create", ticket, "--reuse", "--no-focus", "--json"]
    if base:
        args.extend(["--base", base])
    raw = _run_subprocess(
        args,
        timeout=timeout,
        error_code="CUBRID_WORKTREE_FAILED",
        fix="Verify `cubrid-worktree` is installed and `~/gh/cb/develop` is healthy.",
    )
    return _parse_cubrid_worktree_result(raw)


def create_workspace_cubrid_pr(pr_number: str, *, timeout: float = 180.0) -> dict[str, Any]:
    """Create or reuse a CUBRID PR review worktree and its Herdr workspace."""
    raw = _run_subprocess(
        ["cubrid-worktree", "review", pr_number, "--no-focus", "--json"],
        timeout=timeout,
        error_code="CUBRID_WORKTREE_FAILED",
        fix="Verify `cubrid-worktree` and `gh` authentication, then retry.",
    )
    return _parse_cubrid_worktree_result(raw)


def _parse_cubrid_worktree_result(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
        data = parsed.get("data", parsed)
        herdr_info = data.get("herdr") or {}
        return {
            "workspace_id": herdr_info.get("workspace_id"),
            "tab_id": herdr_info.get("tab_id"),
            "pane_id": herdr_info.get("pane_id"),
            "cwd": data.get("path") or data.get("worktree_path"),
            "ticket": data.get("ticket"),
        }
    except (ValueError, AttributeError) as exc:
        raise CliError(
            "CUBRID_WORKTREE_FAILED",
            "Could not parse JSON output from `cubrid-worktree`.",
            "Run `cubrid-worktree list --json` and `hcmd status` to check created state.",
        ) from exc


def ai_agent_select_backend(*, timeout: float = 30.0) -> str:
    """Return the backend (`ay` or `ai-round`) `ai-agent` would launch, without launching."""
    args = ["ai-agent", "--dry-run", "--json"]
    fix = "Run `ai-agent --dry-run` to inspect backend quota, or pass --launcher ay / ai-round."
    try:
        proc = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CliError(
            "DEPENDENCY_MISSING",
            "Required executable 'ai-agent' was not found on PATH.",
            "Install ai-agent on PATH, or pass --launcher ay / ai-round.",
            exit_code=EXIT_CONFIG,
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise CliError("AI_AGENT_FAILED", f"Failed to run ai-agent: {exc}", fix) from exc

    if proc.returncode != 0:
        error: dict[str, Any] = {}
        try:
            parsed = json.loads(proc.stderr.strip().splitlines()[-1])
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
                error = parsed["error"]
        except (ValueError, IndexError):
            pass
        message = error.get("message") or proc.stderr.strip() or f"exit code {proc.returncode}"
        code = error.get("code")
        raise CliError(
            f"AI_AGENT_{code}" if isinstance(code, str) else "AI_AGENT_FAILED",
            f"ai-agent cannot select a backend: {message}",
            str(error.get("fix") or fix),
            exit_code=EXIT_CONFIG if proc.returncode == EXIT_CONFIG else EXIT_DOMAIN,
        )

    try:
        backend = json.loads(proc.stdout)["data"]["selected"]["name"]
    except (ValueError, KeyError, TypeError) as exc:
        raise CliError(
            "AI_AGENT_FAILED", "Could not parse `ai-agent --dry-run --json` output.", fix
        ) from exc
    if not isinstance(backend, str):
        raise CliError("AI_AGENT_FAILED", "ai-agent reported no selected backend.", fix)
    return backend


def pane_run(pane_id: str, command: str, *, timeout: float = 15.0) -> None:
    """Run a shell command inside an available Herdr pane."""
    _run_subprocess(
        ["herdr", "pane", "run", pane_id, command],
        timeout=timeout,
        error_code="PANE_RUN_FAILED",
        fix=f"Inspect pane {pane_id} with `herdr pane get {pane_id}`.",
    )


def tab_create(
    workspace_id: str,
    *,
    label: str,
    cwd: str,
    env_vars: Mapping[str, str] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Create a non-focused sub-tab inside a Herdr workspace."""
    args = [
        "herdr",
        "tab",
        "create",
        "--workspace",
        workspace_id,
        "--cwd",
        cwd,
        "--label",
        label,
        "--no-focus",
    ]
    for key, value in sorted((env_vars or {}).items()):
        args.extend(["--env", f"{key}={value}"])
    raw = _run_subprocess(
        args,
        timeout=timeout,
        error_code="TAB_CREATE_FAILED",
        fix=f"Verify workspace {workspace_id} and directory {cwd} exist.",
    )
    try:
        data = json.loads(raw)["result"]
        tab = data["tab"]
        pane = data["root_pane"]
        return {
            "workspace_id": tab.get("workspace_id", workspace_id),
            "tab_id": tab["tab_id"],
            "pane_id": pane["pane_id"],
            "label": tab.get("label", label),
            "cwd": cwd,
        }
    except (ValueError, KeyError, TypeError) as exc:
        raise CliError(
            "TAB_CREATE_FAILED",
            "Unexpected JSON response from `herdr tab create`.",
            "Run `hcmd tab list` to inspect workspace tabs.",
        ) from exc


def tab_close(tab_id: str, *, timeout: float = 15.0) -> None:
    """Close a Herdr tab by ID."""
    _run_subprocess(
        ["herdr", "tab", "close", tab_id],
        timeout=timeout,
        error_code="TAB_CLOSE_FAILED",
        fix=f"Verify tab {tab_id} exists with `hcmd tab list`.",
    )


def pane_split(
    target_pane_id: str,
    *,
    direction: str,
    cwd: str,
    env_vars: Mapping[str, str] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Split an existing pane without shifting UI focus."""
    args = [
        "herdr",
        "pane",
        "split",
        "--pane",
        target_pane_id,
        "--direction",
        direction,
        "--cwd",
        cwd,
        "--no-focus",
    ]
    for key, value in sorted((env_vars or {}).items()):
        args.extend(["--env", f"{key}={value}"])
    raw = _run_subprocess(
        args,
        timeout=timeout,
        error_code="PANE_SPLIT_FAILED",
        fix=f"Verify pane {target_pane_id} and directory {cwd} exist.",
    )
    try:
        pane = json.loads(raw)["result"]["pane"]
        return {
            "pane_id": pane["pane_id"],
            "tab_id": pane.get("tab_id"),
            "workspace_id": pane.get("workspace_id"),
            "cwd": cwd,
        }
    except (ValueError, KeyError, TypeError) as exc:
        raise CliError(
            "PANE_SPLIT_FAILED",
            "Unexpected JSON response from `herdr pane split`.",
            "Run `hcmd pane list` to inspect current tab panes.",
        ) from exc


def pane_close(pane_id: str, *, timeout: float = 15.0) -> None:
    """Close a Herdr pane by ID."""
    _run_subprocess(
        ["herdr", "pane", "close", pane_id],
        timeout=timeout,
        error_code="PANE_CLOSE_FAILED",
        fix=f"Verify pane {pane_id} exists with `hcmd pane list`.",
    )


def agent_start(
    name: str,
    kind: str,
    pane_id: str,
    extra_args: Sequence[str],
    *,
    timeout_ms: int = 45000,
) -> dict[str, Any]:
    """Start a supported agent in an idle shell pane via `herdr agent start`."""
    args = [
        "herdr",
        "agent",
        "start",
        name,
        "--kind",
        kind,
        "--pane",
        pane_id,
        "--timeout",
        str(timeout_ms),
    ]
    if extra_args:
        args.append("--")
        args.extend(extra_args)
    raw = _run_subprocess(
        args,
        timeout=max(10.0, timeout_ms / 1000.0 + 10.0),
        error_code="AGENT_START_FAILED",
        fix=f"Inspect pane {pane_id} with `hcmd peek` before retrying.",
    )
    try:
        return json.loads(raw).get("result", {})
    except ValueError:
        return {}


def agent_prompt(
    pane_id: str,
    prompt: str,
    *,
    wait: bool = False,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    """Send a prompt to an agent pane via `herdr agent prompt`."""
    args = ["herdr", "agent", "prompt", pane_id, prompt]
    if wait:
        args.append("--wait")
    if timeout_ms is not None:
        args.extend(["--timeout", str(timeout_ms)])
    proc_timeout = max(15.0, (timeout_ms or 30000) / 1000.0 + 10.0)
    raw = _run_subprocess(
        args,
        timeout=proc_timeout,
        error_code="AGENT_PROMPT_FAILED",
        fix="Inspect the agent pane with `hcmd peek` before resending.",
    )
    try:
        return json.loads(raw).get("result", {})
    except ValueError:
        return {}


def agent_send_keys(
    pane_id: str,
    keys: Sequence[str],
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Send logical keys (e.g. `esc`, `ctrl+c`) to an agent pane via `herdr agent send-keys`."""
    if not keys:
        return {}
    args = ["herdr", "agent", "send-keys", pane_id, *keys]
    raw = _run_subprocess(
        args,
        timeout=timeout,
        error_code="AGENT_SEND_KEYS_FAILED",
        fix=f"Inspect pane {pane_id} with `hcmd peek`.",
    )
    try:
        return json.loads(raw).get("result", {})
    except ValueError:
        return {}


def fetch_work_tracker_items(timeout: float = 5.0) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch open work-tracker items, degrading gracefully with a warning if unavailable."""
    if not shutil.which("work-tracker"):
        return [], "work-tracker executable not found on PATH"
    try:
        proc = subprocess.run(
            ["work-tracker", "list", "--compact", "--json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"work-tracker query failed: {exc}"
    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"exit {proc.returncode}"
        return [], f"work-tracker returned error: {detail}"
    try:
        parsed = json.loads(proc.stdout)
    except ValueError as exc:
        return [], f"work-tracker returned invalid JSON: {exc}"
    if isinstance(parsed, list):
        return parsed, None
    if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
        return parsed["items"], None
    return [], "work-tracker output had unexpected shape"


def workspace_env_overrides() -> dict[str, str]:
    """Return current process environment dictionary."""
    return dict(os.environ)


def _canonical_trust_paths(cwd: str) -> list[str]:
    norm = os.path.normpath(os.path.abspath(os.path.expanduser(cwd)))
    real = os.path.normpath(os.path.realpath(norm))
    return [norm] if real == norm else [norm, real]


@contextmanager
def _config_file_lock(config_path: Path) -> Iterator[None]:
    digest = hashlib.sha256(str(config_path).encode("utf-8")).hexdigest()[:16]
    lock_path = Path(tempfile.gettempdir()) / f"hcmd-trust-{digest}.lock"
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _write_text_atomic(path: Path, text: str, *, default_mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = (path.stat().st_mode & 0o777) if path.exists() else default_mode
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _load_json_object(path: Path, *, label: str) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return {}, None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"Could not read {label} ({path}): {exc}"
    if not raw.strip():
        return {}, None
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        return None, f"Skipping auto-trust in malformed {label} ({path}): {exc}"
    if not isinstance(parsed, dict):
        return None, f"Skipping auto-trust in {label} ({path}): expected top-level JSON object"
    return parsed, None


def _ensure_claude_trust(
    paths: Sequence[str],
    env: Mapping[str, str],
    home: Path,
) -> str | None:
    raw_dir = env.get("CLAUDE_CONFIG_DIR", "").strip()
    config_path = (
        (Path(raw_dir).expanduser().resolve() / ".claude.json")
        if raw_dir
        else (home / ".claude.json")
    )
    with _config_file_lock(config_path):
        data, warn = _load_json_object(config_path, label="Claude config")
        if warn is not None or data is None:
            return warn
        projects = data.get("projects")
        if projects is None:
            projects = {}
            data["projects"] = projects
        elif not isinstance(projects, dict):
            return f"Skipping Claude auto-trust in {config_path}: `projects` is not an object"

        changed = False
        for target_path in paths:
            entry = projects.get(target_path)
            if entry is None:
                projects[target_path] = {"hasTrustDialogAccepted": True}
                changed = True
            elif isinstance(entry, dict):
                if entry.get("hasTrustDialogAccepted") is not True:
                    entry["hasTrustDialogAccepted"] = True
                    changed = True
            else:
                return (
                    f"Skipping Claude auto-trust for {target_path} in {config_path}: "
                    "project entry is not an object"
                )
        if not changed:
            return None
        serialized = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        json.loads(serialized)
        _write_text_atomic(config_path, serialized, default_mode=0o600)
    return None


def _ensure_agy_trust(paths: Sequence[str], home: Path) -> str | None:
    config_path = home / ".gemini" / "antigravity-cli" / "settings.json"
    with _config_file_lock(config_path):
        data, warn = _load_json_object(config_path, label="Antigravity settings")
        if warn is not None or data is None:
            return warn
        trusted = data.get("trustedWorkspaces")
        if trusted is None:
            trusted = []
            data["trustedWorkspaces"] = trusted
        elif not isinstance(trusted, list):
            return (
                f"Skipping Antigravity auto-trust in {config_path}: "
                "`trustedWorkspaces` is not a list"
            )

        changed = False
        for target_path in paths:
            if target_path not in trusted:
                trusted.append(target_path)
                changed = True
        if not changed:
            return None
        serialized = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        json.loads(serialized)
        _write_text_atomic(config_path, serialized, default_mode=0o644)
    return None


def _parse_toml_table_header(line: str) -> dict[str, Any] | None:
    stripped = line.lstrip()
    if not stripped.startswith("[") or stripped.startswith("[["):
        return None
    try:
        parsed = tomllib.loads(stripped + "\n")
    except tomllib.TOMLDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _set_codex_project_trust(raw_text: str, target_path: str) -> str:
    lines = raw_text.splitlines()
    header_idx: int | None = None
    for idx, line in enumerate(lines):
        parsed_header = _parse_toml_table_header(line)
        if parsed_header is None:
            continue
        projects = parsed_header.get("projects")
        if isinstance(projects, dict) and set(projects.keys()) == {target_path}:
            header_idx = idx
            break

    if header_idx is None:
        prefix = raw_text
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix and not prefix.endswith("\n\n"):
            prefix += "\n"
        return f'{prefix}[projects.{json.dumps(target_path)}]\ntrust_level = "trusted"\n'

    end_idx = len(lines)
    for idx in range(header_idx + 1, len(lines)):
        if _parse_toml_table_header(lines[idx]) is not None:
            end_idx = idx
            break

    replaced = False
    for idx in range(header_idx + 1, end_idx):
        match = re.match(r"^(\s*)trust_level\s*=", lines[idx])
        if match:
            lines[idx] = f'{match.group(1)}trust_level = "trusted"'
            replaced = True
            break
    if not replaced:
        lines.insert(header_idx + 1, 'trust_level = "trusted"')
    return "\n".join(lines) + "\n"


def _ensure_codex_trust(
    paths: Sequence[str],
    env: Mapping[str, str],
    home: Path,
) -> str | None:
    raw_home = env.get("CODEX_HOME", "").strip()
    codex_dir = Path(raw_home).expanduser().resolve() if raw_home else (home / ".codex")
    config_path = codex_dir / "local.config.toml"
    with _config_file_lock(config_path):
        try:
            raw_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        except OSError as exc:
            return f"Could not read Codex config ({config_path}): {exc}"
        try:
            parsed = tomllib.loads(raw_text) if raw_text.strip() else {}
        except tomllib.TOMLDecodeError as exc:
            return f"Skipping Codex auto-trust in malformed TOML ({config_path}): {exc}"

        projects = parsed.get("projects")
        if projects is not None and not isinstance(projects, dict):
            return f"Skipping Codex auto-trust in {config_path}: `projects` is not a table"

        updated_text = raw_text
        changed = False
        for target_path in paths:
            existing = projects.get(target_path) if isinstance(projects, dict) else None
            if isinstance(existing, dict) and existing.get("trust_level") == "trusted":
                continue
            updated_text = _set_codex_project_trust(updated_text, target_path)
            changed = True
            try:
                parsed = tomllib.loads(updated_text)
            except tomllib.TOMLDecodeError as exc:
                return f"Skipping Codex auto-trust ({config_path}): generated invalid TOML ({exc})"
            projects = parsed.get("projects")

        if not changed:
            return None
        try:
            final_parsed = tomllib.loads(updated_text)
        except tomllib.TOMLDecodeError as exc:
            return f"Skipping Codex auto-trust ({config_path}): validation failed ({exc})"
        final_projects = final_parsed.get("projects") or {}
        for target_path in paths:
            entry = final_projects.get(target_path)
            if not isinstance(entry, dict) or entry.get("trust_level") != "trusted":
                return f"Skipping Codex auto-trust ({config_path}): trust verification failed"
        _write_text_atomic(config_path, updated_text, default_mode=0o644)
    return None


def ensure_trusted_workspace(
    cwd: str,
    agent_kinds: Sequence[str],
    env: Mapping[str, str],
    *,
    home_dir: Path | None = None,
) -> list[str]:
    """Pre-trust `cwd` in Claude, Antigravity (`agy`), and/or Codex local configs.

    Returns non-fatal warning strings; malformed user configs are never overwritten.
    """
    if not cwd.strip() or not agent_kinds:
        return []
    home = home_dir or Path.home()
    paths = _canonical_trust_paths(cwd)
    kinds = set(agent_kinds)
    warnings: list[str] = []

    if "claude" in kinds:
        try:
            warn = _ensure_claude_trust(paths, env, home)
        except OSError as exc:
            warn = f"Failed to persist Claude workspace trust: {exc}"
        if warn:
            warnings.append(warn)
    if "agy" in kinds:
        try:
            warn = _ensure_agy_trust(paths, home)
        except OSError as exc:
            warn = f"Failed to persist Antigravity workspace trust: {exc}"
        if warn:
            warnings.append(warn)
    if "codex" in kinds:
        try:
            warn = _ensure_codex_trust(paths, env, home)
        except OSError as exc:
            warn = f"Failed to persist Codex workspace trust: {exc}"
        if warn:
            warnings.append(warn)
    return warnings


def resolve_state_dir(
    env: Mapping[str, str],
    *,
    home_dir: Path | None = None,
) -> Path:
    """Resolve persistent state directory under XDG_STATE_HOME/herdr-commander."""
    home = home_dir or Path.home()
    raw_state = env.get("XDG_STATE_HOME", "").strip()
    base = Path(raw_state).expanduser() if raw_state else (home / ".local" / "state")
    return base / "herdr-commander"


def resolve_cursor_scope(
    socket_path: str,
    self_workspace_id: str | None,
    cursor_name: str | None,
) -> str:
    """Resolve safe filename-compatible cursor scope for `digest --since-last` / `wait`."""
    if cursor_name is not None:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", cursor_name.strip()).strip("-.")
        if not cleaned:
            raise CliError(
                "INVALID_CURSOR",
                "Cursor name cannot be empty or punctuation-only.",
                "Pass an alphanumeric cursor name such as `--cursor commander`.",
                exit_code=2,
            )
        return f"named-{cleaned[:64]}"
    sock_hash = hashlib.sha256(socket_path.encode("utf-8")).hexdigest()[:10]
    ws_part = re.sub(r"[^A-Za-z0-9._-]+", "-", (self_workspace_id or "global")).strip("-")
    return f"auto-{sock_hash}-{ws_part or 'global'}"


def resolve_cursor_path(state_dir: Path, scope: str) -> Path:
    """Return path to a digest cursor JSON file."""
    return state_dir / "cursors" / f"{scope}.json"


def load_digest_cursor(path: Path) -> tuple[DigestCursor | None, str | None]:
    """Load persisted DigestCursor, degrading gracefully with a warning if corrupt."""
    if not path.exists():
        return None, None
    with _config_file_lock(path):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            return None, f"Could not read digest cursor ({path}): {exc}"
        if not raw.strip():
            return None, None
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            return None, f"Ignoring corrupt digest cursor ({path}): {exc}"
        if not isinstance(parsed, dict):
            return None, f"Ignoring malformed digest cursor ({path}): expected JSON object"
        return DigestCursor.from_dict(parsed), None


def save_digest_cursor(path: Path, cursor: DigestCursor) -> None:
    """Persist DigestCursor atomically under an advisory lock."""
    serialized = json.dumps(cursor.to_dict(), indent=2, ensure_ascii=False) + "\n"
    with _config_file_lock(path):
        _write_text_atomic(path, serialized, default_mode=0o600)


def delete_digest_cursor(path: Path) -> bool:
    """Delete an existing cursor file safely under lock; return True if removed."""
    with _config_file_lock(path):
        if not path.exists():
            return False
        path.unlink(missing_ok=True)
        return True


def resolve_handoff_paths(state_dir: Path, workspace_id: str) -> tuple[Path, Path]:
    """Return `(json_path, md_path)` for a workspace's rotation handoff record."""
    safe_ws = re.sub(r"[^A-Za-z0-9._-]+", "-", workspace_id.strip()).strip("-") or "default"
    handoff_dir = state_dir / "handoff"
    return handoff_dir / f"{safe_ws}.json", handoff_dir / f"{safe_ws}.md"


def load_handoff_state(json_path: Path) -> dict[str, Any] | None:
    """Load persisted rotation handoff JSON payload if present and valid."""
    if not json_path.exists():
        return None
    with _config_file_lock(json_path):
        try:
            parsed = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None


def save_handoff_state(
    json_path: Path,
    md_path: Path,
    payload: Mapping[str, Any],
    markdown: str,
) -> None:
    """Persist rotation handoff JSON and Markdown atomically under lock."""
    serialized = json.dumps(dict(payload), indent=2, ensure_ascii=False) + "\n"
    with _config_file_lock(json_path):
        _write_text_atomic(json_path, serialized, default_mode=0o600)
        _write_text_atomic(md_path, markdown, default_mode=0o600)


def read_recent_audit_lines(state_dir: Path, limit: int = 10) -> list[str]:
    """Read the most recent lines from `auto-audit.jsonl` if present."""
    if limit <= 0:
        return []
    audit_path = state_dir / "auto-audit.jsonl"
    if not audit_path.exists():
        return []
    try:
        raw_lines = audit_path.read_text(encoding="utf-8").splitlines()
        lines = [line.strip() for line in raw_lines if line.strip()]
    except OSError:
        return []
    return lines[-limit:]
