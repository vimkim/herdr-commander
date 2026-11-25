"""Unix socket RPC/event client and external CLI adapters for Herdr."""

from __future__ import annotations

import json
import os
import select
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from .models import EXIT_CONFIG, EXIT_DOMAIN, EXIT_TEMP, CliError

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
                "Verify the pane still exists with `herdr-spaces-status status`.",
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
            "Run `herdr-spaces-status status` to verify whether the workspace was created.",
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
            "Run `cubrid-worktree list --json` and `hss status` to check created state.",
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
            "Run `hss tab list` to inspect workspace tabs.",
        ) from exc


def tab_close(tab_id: str, *, timeout: float = 15.0) -> None:
    """Close a Herdr tab by ID."""
    _run_subprocess(
        ["herdr", "tab", "close", tab_id],
        timeout=timeout,
        error_code="TAB_CLOSE_FAILED",
        fix=f"Verify tab {tab_id} exists with `hss tab list`.",
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
            "Run `hss pane list` to inspect current tab panes.",
        ) from exc


def pane_close(pane_id: str, *, timeout: float = 15.0) -> None:
    """Close a Herdr pane by ID."""
    _run_subprocess(
        ["herdr", "pane", "close", pane_id],
        timeout=timeout,
        error_code="PANE_CLOSE_FAILED",
        fix=f"Verify pane {pane_id} exists with `hss pane list`.",
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
        fix=f"Inspect pane {pane_id} with `herdr-spaces-status peek` before retrying.",
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
        fix="Inspect the agent pane with `herdr-spaces-status peek` before resending.",
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
