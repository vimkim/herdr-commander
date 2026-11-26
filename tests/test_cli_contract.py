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
        [sys.executable, "-m", "herdr_commander", *args],
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

    def test_tab_and_pane_list_via_socket(self) -> None:
        snapshot = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "herdr.sock"
            with MockHerdrServer(sock_path, snapshot):
                tab_proc = run_cli(
                    ["--socket", str(sock_path), "tab", "list", "--json"],
                    env={
                        "HERDR_WORKSPACE_ID": "w1",
                        "HERDR_TAB_ID": "w1:t1",
                        "HERDR_PANE_ID": "w1:p1",
                    },
                )
                self.assertEqual(tab_proc.returncode, 0, tab_proc.stderr)
                tab_data = json.loads(tab_proc.stdout)["data"]
                self.assertEqual(tab_data["caller"]["role"], "space_lead")
                self.assertEqual(tab_data["sub_tab_count"], 2)

                pane_proc = run_cli(
                    ["--socket", str(sock_path), "pane", "list", "--json"],
                    env={
                        "HERDR_WORKSPACE_ID": "w2",
                        "HERDR_TAB_ID": "w2:t4",
                        "HERDR_PANE_ID": "w2:p4",
                        "HERDR_TAB_ROLE": "worker",
                    },
                )
                self.assertEqual(pane_proc.returncode, 0, pane_proc.stderr)
                pane_data = json.loads(pane_proc.stdout)["data"]
                self.assertEqual(pane_data["caller"]["role"], "tab_lead")
                self.assertEqual(pane_data["worker_pane_count"], 1)

    def test_find_json_plain_human_and_errors(self) -> None:
        snapshot = copy.deepcopy(json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))
        with tempfile.TemporaryDirectory() as tmpdir:
            live_dir = Path(tmpdir) / "demo-skills"
            live_dir.mkdir()
            for item in (*snapshot["panes"], *snapshot["agents"]):
                if item["workspace_id"] == "w2":
                    item["cwd"] = str(live_dir)
                    if "foreground_cwd" in item:
                        item["foreground_cwd"] = str(live_dir)
                elif item["workspace_id"] != "w3":
                    other_dir = Path(tmpdir) / item["workspace_id"]
                    other_dir.mkdir(exist_ok=True)
                    item["cwd"] = str(other_dir)
                    if "foreground_cwd" in item:
                        item["foreground_cwd"] = str(other_dir)

            sock_path = Path(tmpdir) / "herdr.sock"
            with MockHerdrServer(sock_path, snapshot):
                found_proc = run_cli(
                    [
                        "--socket",
                        str(sock_path),
                        "find",
                        "--label",
                        "demo-skills",
                        "--pwd",
                        str(live_dir),
                        "--json",
                    ],
                    env={"HERDR_WORKSPACE_ID": "w4"},
                )
                self.assertEqual(found_proc.returncode, 0, found_proc.stderr)
                self.assertEqual(found_proc.stderr, "")
                found_data = json.loads(found_proc.stdout)["data"]
                self.assertTrue(found_data["found"])
                self.assertEqual(found_data["count"], 1)
                self.assertEqual(found_data["recommendation"], "ask_reuse")
                match = found_data["matches"][0]
                self.assertEqual(match["workspace"]["workspace_id"], "w2")
                self.assertIn("label_exact", match["match_reasons"])
                self.assertIn("cwd_exact", match["match_reasons"])

                busy_proc = run_cli(
                    [
                        "--socket",
                        str(sock_path),
                        "find",
                        "--ticket",
                        "CBRD-10003",
                        "--verify-busy",
                        "--json",
                    ],
                    env={"HERDR_WORKSPACE_ID": "w4"},
                )
                self.assertEqual(busy_proc.returncode, 0, busy_proc.stderr)
                busy_data = json.loads(busy_proc.stdout)["data"]
                self.assertEqual(busy_data["recommendation"], "warn_busy")
                self.assertEqual(
                    busy_data["matches"][0]["workspace"]["primary_pane"]["effective_status"],
                    "busy_bg",
                )

                shell_proc = run_cli(
                    [
                        "--socket",
                        str(sock_path),
                        "find",
                        "--ticket",
                        "CBRD-10002",
                        "--json",
                    ],
                    env={"HERDR_WORKSPACE_ID": "w4"},
                )
                self.assertEqual(shell_proc.returncode, 0, shell_proc.stderr)
                shell_data = json.loads(shell_proc.stdout)["data"]
                self.assertEqual(shell_data["recommendation"], "reuse_empty_shell")

                miss_proc = run_cli(
                    [
                        "--socket",
                        str(sock_path),
                        "find",
                        "--label",
                        "does-not-exist",
                        "--json",
                    ],
                    env={"HERDR_WORKSPACE_ID": "w4"},
                )
                self.assertEqual(miss_proc.returncode, 0, miss_proc.stderr)
                miss_data = json.loads(miss_proc.stdout)["data"]
                self.assertFalse(miss_data["found"])
                self.assertEqual(miss_data["count"], 0)
                self.assertEqual(miss_data["recommendation"], "create_new")
                self.assertEqual(miss_data["matches"], [])

                plain_proc = run_cli(
                    [
                        "--socket",
                        str(sock_path),
                        "find",
                        "--label",
                        "demo-skills",
                        "--plain",
                    ],
                    env={"HERDR_WORKSPACE_ID": "w4"},
                )
                self.assertEqual(plain_proc.returncode, 0, plain_proc.stderr)
                self.assertNotIn("\033", plain_proc.stdout)
                self.assertIn("w2\texact\tlabel_exact\tidle\tclaude", plain_proc.stdout)

                human_proc = run_cli(
                    [
                        "--socket",
                        str(sock_path),
                        "find",
                        "--label",
                        "demo-skills",
                    ],
                    env={"HERDR_WORKSPACE_ID": "w4"},
                )
                self.assertEqual(human_proc.returncode, 0, human_proc.stderr)
                self.assertNotIn("\033", human_proc.stdout)
                self.assertIn("recommendation: ask_reuse", human_proc.stdout)
                self.assertIn("Ask the user whether to reuse", human_proc.stdout)

                self_excluded = run_cli(
                    [
                        "--socket",
                        str(sock_path),
                        "find",
                        "--label",
                        "herdr-spaces-status",
                        "--json",
                    ],
                    env={"HERDR_WORKSPACE_ID": "w4"},
                )
                self.assertEqual(self_excluded.returncode, 0, self_excluded.stderr)
                self.assertFalse(json.loads(self_excluded.stdout)["data"]["found"])

                self_included = run_cli(
                    [
                        "--socket",
                        str(sock_path),
                        "find",
                        "--label",
                        "herdr-spaces-status",
                        "--include-self",
                        "--json",
                    ],
                    env={"HERDR_WORKSPACE_ID": "w4"},
                )
                self.assertEqual(self_included.returncode, 0, self_included.stderr)
                self_data = json.loads(self_included.stdout)["data"]
                self.assertTrue(self_data["found"])
                self.assertEqual(self_data["matches"][0]["workspace"]["workspace_id"], "w4")

                err_proc = run_cli(["--socket", str(sock_path), "find", "--json"])
                self.assertEqual(err_proc.returncode, 2)
                self.assertEqual(err_proc.stdout, "")
                err_payload = json.loads(err_proc.stderr.strip().splitlines()[-1])
                self.assertEqual(err_payload["error"]["code"], "MISSING_QUERY")

    def test_ensure_on_existing_fail_and_reuse_via_socket(self) -> None:
        snapshot = copy.deepcopy(json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))
        with tempfile.TemporaryDirectory() as tmpdir:
            live_dir = Path(tmpdir) / "demo-skills"
            live_dir.mkdir()
            for item in (*snapshot["panes"], *snapshot["agents"]):
                if item["workspace_id"] == "w2":
                    item["cwd"] = str(live_dir)
                    if "foreground_cwd" in item:
                        item["foreground_cwd"] = str(live_dir)
                elif item["workspace_id"] != "w3":
                    other_dir = Path(tmpdir) / item["workspace_id"]
                    other_dir.mkdir(exist_ok=True)
                    item["cwd"] = str(other_dir)
                    if "foreground_cwd" in item:
                        item["foreground_cwd"] = str(other_dir)

            sock_path = Path(tmpdir) / "herdr.sock"
            with MockHerdrServer(sock_path, snapshot):
                fail_busy = run_cli(
                    [
                        "--socket",
                        str(sock_path),
                        "ensure",
                        "--ticket",
                        "CBRD-10003",
                        "--on-existing",
                        "fail",
                        "--json",
                    ],
                    env={"HERDR_WORKSPACE_ID": "w4"},
                )
                self.assertEqual(fail_busy.returncode, 1)
                self.assertEqual(fail_busy.stdout, "")
                busy_err = json.loads(fail_busy.stderr.strip().splitlines()[-1])["error"]
                self.assertEqual(busy_err["code"], "EXISTING_WORKSPACE_FOUND")
                self.assertEqual(busy_err["details"]["recommendation"], "warn_busy")
                self.assertEqual(
                    busy_err["details"]["matches"][0]["workspace"]["primary_pane"][
                        "effective_status"
                    ],
                    "busy_bg",
                )

                fail_shell = run_cli(
                    [
                        "--socket",
                        str(sock_path),
                        "ensure",
                        "--ticket",
                        "CBRD-10002",
                        "--on-existing",
                        "fail",
                        "--json",
                    ],
                    env={"HERDR_WORKSPACE_ID": "w4"},
                )
                self.assertEqual(fail_shell.returncode, 1)
                shell_err = json.loads(fail_shell.stderr.strip().splitlines()[-1])["error"]
                self.assertEqual(shell_err["details"]["recommendation"], "reuse_empty_shell")

                reuse_proc = run_cli(
                    [
                        "--socket",
                        str(sock_path),
                        "ensure",
                        "--reuse",
                        "w2",
                        "--launcher",
                        "ai-agent",
                        "--dry-run",
                        "--json",
                    ],
                    env={"HERDR_WORKSPACE_ID": "w4"},
                )
                self.assertEqual(reuse_proc.returncode, 0, reuse_proc.stderr)
                reuse_data = json.loads(reuse_proc.stdout)["data"]
                self.assertEqual(reuse_data["workspace_action"], "reuse")
                self.assertEqual(reuse_data["agent_action"], "reuse")
                self.assertEqual(reuse_data["workspace_id"], "w2")

                new_proc = run_cli(
                    [
                        "--socket",
                        str(sock_path),
                        "ensure",
                        "--pwd",
                        str(live_dir),
                        "--label",
                        "demo-skills-2",
                        "--on-existing",
                        "new",
                        "--launcher",
                        "shell",
                        "--dry-run",
                        "--json",
                    ],
                    env={"HERDR_WORKSPACE_ID": "w4"},
                )
                self.assertEqual(new_proc.returncode, 0, new_proc.stderr)
                new_data = json.loads(new_proc.stdout)["data"]
                self.assertEqual(new_data["workspace_action"], "create_cwd")
                self.assertEqual(new_data["label"], "demo-skills-2")


if __name__ == "__main__":
    unittest.main()
