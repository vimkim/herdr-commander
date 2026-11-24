from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "snapshot_sample.json"
REPO_ROOT = Path(__file__).resolve().parent.parent


class MockHerdrServer:
    """Minimal AF_UNIX JSON-lines Herdr server for CLI contract testing."""

    def __init__(self, socket_path: Path, snapshot: dict, *, emit_done_event: bool = False) -> None:
        self.socket_path = socket_path
        self.snapshot = snapshot
        self.emit_done_event = emit_done_event
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(socket_path))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> MockHerdrServer:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            data = conn.recv(65536)
            if not data:
                return
            req = json.loads(data.decode().splitlines()[0])
            req_id = req["id"]
            method = req["method"]
            if method == "session.snapshot":
                resp = {"id": req_id, "result": {"snapshot": self.snapshot, "type": "snapshot"}}
                conn.sendall((json.dumps(resp) + "\n").encode())
            elif method == "agent.read":
                target = req["params"]["target"]
                source = req["params"]["source"]
                if source not in {"visible", "recent", "recent_unwrapped", "detection"}:
                    resp = {
                        "id": req_id,
                        "error": {
                            "code": "invalid_request",
                            "message": f"unknown variant {source}",
                        },
                    }
                    conn.sendall((json.dumps(resp) + "\n").encode())
                    return
                text = (
                    "────────\n❯ \n────────\n⏵⏵ bypass on · 1 background agent"
                    if source == "detection" and target == "w6:p2"
                    else "Mock agent output line"
                )
                resp = {
                    "id": req_id,
                    "result": {"read": {"pane_id": target, "text": text}, "type": "agent_read"},
                }
                conn.sendall((json.dumps(resp) + "\n").encode())
            elif method == "events.subscribe":
                ack = {"id": req_id, "result": {"type": "subscription_started"}}
                conn.sendall((json.dumps(ack) + "\n").encode())
                if self.emit_done_event:
                    time.sleep(0.05)
                    for p in self.snapshot["panes"]:
                        if p["pane_id"] == "w6:p2":
                            p["agent_status"] = "done"
                    for a in self.snapshot["agents"]:
                        if a["pane_id"] == "w6:p2":
                            a["agent_status"] = "done"
                    ev = {"event": "pane.agent_status_changed", "data": {"pane_id": "w6:p2"}}
                    conn.sendall((json.dumps(ev) + "\n").encode())
                while not self._stop.is_set():
                    time.sleep(0.05)


def run_cli(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = dict(os.environ)
    merged_env["PYTHONPATH"] = str(REPO_ROOT / "src")
    if env:
        merged_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "herdr_spaces_status", *args],
        cwd=str(REPO_ROOT),
        env=merged_env,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


class CliContractTests(unittest.TestCase):
    def test_not_in_herdr_returns_exit_78_and_json_error_on_stderr(self) -> None:
        proc = run_cli(["status", "--json"], env={"HERDR_ENV": "0", "HERDR_SOCKET_PATH": ""})
        self.assertEqual(proc.returncode, 78)
        self.assertEqual(proc.stdout, "")
        err = json.loads(proc.stderr.strip().splitlines()[-1])
        self.assertFalse(err["ok"])
        self.assertEqual(err["error"]["code"], "NOT_IN_HERDR")

    def test_status_json_plain_and_invalid_field(self) -> None:
        snapshot = copy.deepcopy(json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))
        with tempfile.TemporaryDirectory() as tmpdir:
            live_dir = Path(tmpdir) / "live-repo"
            live_dir.mkdir()
            for item in (*snapshot["panes"], *snapshot["agents"]):
                if item["workspace_id"] != "w3":
                    item["cwd"] = str(live_dir)
                    if "foreground_cwd" in item:
                        item["foreground_cwd"] = str(live_dir)
            sock_path = Path(tmpdir) / "herdr.sock"
            with MockHerdrServer(sock_path, snapshot):
                proc = run_cli(
                    [
                        "--socket",
                        str(sock_path),
                        "status",
                        "--workers-only",
                        "--verify-busy",
                        "--json",
                    ],
                    env={"HERDR_WORKSPACE_ID": "w4"},
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")
                payload = json.loads(proc.stdout)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["schema_version"], 1)
                wids = [w["workspace_id"] for w in payload["data"]["workspaces"]]
                self.assertNotIn("w4", wids)
                w6 = next(w for w in payload["data"]["workspaces"] if w["workspace_id"] == "w6")
                self.assertEqual(w6["primary_pane"]["effective_status"], "busy_bg")

                plain = run_cli(
                    [
                        "--socket",
                        str(sock_path),
                        "status",
                        "--plain",
                        "--fields",
                        "workspace_id,attention,agent_kind",
                    ]
                )
                self.assertEqual(plain.returncode, 0, plain.stderr)
                self.assertNotIn("\033", plain.stdout)
                self.assertIn("w1\tdone\tagy", plain.stdout)

                bad_field = run_cli(
                    ["--socket", str(sock_path), "status", "--plain", "--fields", "bogus"]
                )
                self.assertEqual(bad_field.returncode, 2)
                self.assertEqual(bad_field.stdout, "")

    def test_peek_and_wait_via_events(self) -> None:
        snapshot = copy.deepcopy(json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))
        for p in snapshot["panes"]:
            if p["pane_id"] == "w6:p2":
                p["agent_status"] = "working"
        for a in snapshot["agents"]:
            if a["pane_id"] == "w6:p2":
                a["agent_status"] = "working"

        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "herdr.sock"
            with MockHerdrServer(sock_path, snapshot, emit_done_event=True):
                peek_proc = run_cli(
                    ["--socket", str(sock_path), "peek", "w6", "--lines", "10", "--json"]
                )
                self.assertEqual(peek_proc.returncode, 0, peek_proc.stderr)
                peek_data = json.loads(peek_proc.stdout)["data"]
                self.assertEqual(peek_data["pane_id"], "w6:p2")
                self.assertIn("Mock agent output line", peek_data["text"])

                wait_proc = run_cli(
                    [
                        "--socket",
                        str(sock_path),
                        "wait",
                        "--workspace",
                        "w6",
                        "--until",
                        "done",
                        "--timeout",
                        "3",
                        "--json",
                    ]
                )
                self.assertEqual(wait_proc.returncode, 0, wait_proc.stderr)
                wait_data = json.loads(wait_proc.stdout)["data"]
                self.assertFalse(wait_data["from_initial_snapshot"])
                self.assertEqual(wait_data["matched"][0]["workspace_id"], "w6")

    def test_send_stdin_dry_run(self) -> None:
        snapshot = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "herdr.sock"
            with MockHerdrServer(sock_path, snapshot):
                proc = run_cli(
                    [
                        "--socket",
                        str(sock_path),
                        "send",
                        "w2",
                        "--prompt",
                        "-",
                        "--dry-run",
                        "--json",
                    ],
                    stdin_text="Line 1\nLine 2\n",
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                data = json.loads(proc.stdout)["data"]
                self.assertTrue(data["dry_run"])
                self.assertEqual(data["prompt_chars"], len("Line 1\nLine 2\n"))


if __name__ == "__main__":
    unittest.main()
