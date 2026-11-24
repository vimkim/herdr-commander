from __future__ import annotations

import copy
import json
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

from herdr_spaces_status.cli import _dispatch, build_parser
from herdr_spaces_status.formatters import OutputMode
from herdr_spaces_status.models import CliError
from herdr_spaces_status.orchestrate import (
    LAUNCHERS,
    build_agent_start_args,
    build_pane_run_command,
    execute_ensure,
    execute_send,
    plan_ensure,
    sanitize_agent_name,
    validate_send,
)
from herdr_spaces_status.snapshot import parse_snapshot

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "snapshot_sample.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class FakeClient:
    def __init__(self, snapshots: list[dict], *, detection_text: str = "") -> None:
        self._snapshots = snapshots
        self._idx = 0
        self._detection_text = detection_text

    def snapshot(self) -> dict:
        snap = self._snapshots[min(self._idx, len(self._snapshots) - 1)]
        self._idx += 1
        return snap

    def read_detection(self, _pane_id: str) -> str:
        return self._detection_text


class OrchestrateTests(unittest.TestCase):
    def test_launcher_commands_and_env_forwarding(self) -> None:
        cmd = build_pane_run_command(
            LAUNCHERS["ai-round"],
            {
                "CLAUDE_ACCOUNTS_DIR": "/tmp/accounts",
                "CLAUDE_CONFIG_DIR": "/tmp/claude cfg",
                "XDG_STATE_HOME": "/tmp/state",
            },
        )
        self.assertIn("CLAUDE_ACCOUNTS_DIR=/tmp/accounts", cmd)
        self.assertIn("CLAUDE_CONFIG_DIR='/tmp/claude cfg'", cmd)
        self.assertIn("XDG_STATE_HOME=/tmp/state", cmd)
        self.assertTrue(cmd.endswith(" ai-round"))

        codex_args = build_agent_start_args(LAUNCHERS["codex"], {})
        self.assertEqual(
            codex_args,
            ["--profile", "local", "--dangerously-bypass-approvals-and-sandbox"],
        )

        claude_args = build_agent_start_args(
            LAUNCHERS["claude"],
            {"CLAUDE_CONFIG_DIR": "/tmp/custom-claude"},
        )
        self.assertEqual(
            claude_args,
            [
                "--settings",
                "/tmp/custom-claude/local-settings.json",
                "--dangerously-skip-permissions",
            ],
        )

    def test_plan_ensure_reuses_and_guards_occupied_pane(self) -> None:
        raw = load_fixture()
        spaces = parse_snapshot(raw, self_workspace_id="w4", path_exists=lambda _p: True)

        # Reuse running ay (agy) on CBRD-10001.
        plan_reuse = plan_ensure(
            spaces,
            label=None,
            cwd=None,
            ticket="CBRD-10001",
            pr_number=None,
            launcher_name="ay",
        )
        self.assertEqual(plan_reuse.workspace_action, "reuse")
        self.assertEqual(plan_reuse.agent_action, "reuse")
        self.assertEqual(plan_reuse.existing_workspace_id, "w1")

        # Launch ay into existing empty shell workspace CBRD-10002.
        plan_launch = plan_ensure(
            spaces,
            label=None,
            cwd=None,
            ticket="cbrd-10002",
            pr_number=None,
            launcher_name="ay",
        )
        self.assertEqual(plan_launch.workspace_action, "reuse")
        self.assertEqual(plan_launch.agent_action, "launch")
        self.assertEqual(plan_launch.existing_pane_id, "w5:p1")

        # Refuse overwriting claude pane with codex.
        with self.assertRaises(CliError) as ctx:
            plan_ensure(
                spaces,
                label=None,
                cwd=None,
                ticket="CBRD-10003",
                pr_number=None,
                launcher_name="codex",
            )
        self.assertEqual(ctx.exception.code, "OCCUPIED_BY_OTHER_AGENT")

        # New ticket plan.
        plan_new = plan_ensure(
            spaces,
            label=None,
            cwd=None,
            ticket="CBRD-10004",
            pr_number=None,
            launcher_name="ai-best",
        )
        self.assertEqual(plan_new.workspace_action, "create_cubrid_ticket")
        self.assertEqual(plan_new.agent_action, "launch")
        self.assertEqual(plan_new.label, "CBRD-10004")

    def test_execute_ensure_launches_and_waits_until_ready(self) -> None:
        snap0 = load_fixture()
        snap1 = copy.deepcopy(snap0)
        for p in snap1["panes"]:
            if p["pane_id"] == "w5:p1":
                p["agent"] = "agy"
                p["agent_status"] = "idle"
        snap1["agents"].append(
            {
                "agent": "agy",
                "agent_status": "idle",
                "cwd": "/home/user/gh/cb/CBRD-10002",
                "interactive_ready": True,
                "name": "cb-10002",
                "pane_id": "w5:p1",
                "revision": 505,
                "tab_id": "w5:t1",
                "workspace_id": "w5",
            }
        )

        spaces0 = parse_snapshot(snap0, path_exists=lambda _p: True)
        plan = plan_ensure(
            spaces0,
            label=None,
            cwd=None,
            ticket="CBRD-10002",
            pr_number=None,
            launcher_name="ay",
        )
        ran: list[tuple[str, str]] = []
        result = execute_ensure(
            plan,
            client=FakeClient([snap0, snap1]),
            create_cwd=lambda _l, _c: {},
            create_ticket=lambda _t, *, base=None: {},
            create_pr=lambda _p: {},
            run_pane=lambda pid, cmd: ran.append((pid, cmd)),
            start_agent=lambda _name, _kind, _pane_id, _extra_args, *, timeout_ms=45000: {},
            env={},
            dry_run=False,
            ready_timeout=5.0,
            poll_interval=0.0,
            sleep_fn=lambda _s: None,
        )
        self.assertEqual(ran, [("w5:p1", "ay")])
        self.assertEqual(result.workspace_action, "reuse")
        self.assertEqual(result.agent_action, "launch")
        self.assertEqual(result.agent_kind, "agy")
        self.assertEqual(result.agent_status, "idle")

    def test_validate_and_execute_send_guards(self) -> None:
        raw = load_fixture()
        for p in raw["panes"]:
            if p["pane_id"] == "w6:p2":
                p["agent_status"] = "blocked"
        for a in raw["agents"]:
            if a["pane_id"] == "w6:p2":
                a["agent_status"] = "blocked"
        spaces = parse_snapshot(raw, self_workspace_id="w4", path_exists=lambda _p: True)
        by_id = {s.workspace_id: s for s in spaces}

        with self.assertRaises(CliError) as self_err:
            validate_send(by_id["w4"], prompt="hi", force=False)
        self.assertEqual(self_err.exception.code, "CANNOT_SEND_TO_SELF")

        with self.assertRaises(CliError) as shell_err:
            validate_send(by_id["w5"], prompt="hi", force=False)
        self.assertEqual(shell_err.exception.code, "NO_AGENT")

        with self.assertRaises(CliError) as blocked_err:
            validate_send(by_id["w6"], prompt="hi", force=True)
        self.assertEqual(blocked_err.exception.code, "AGENT_BLOCKED")

        sent_calls: list[tuple[str, str, bool, int | None]] = []

        def record_prompt(
            pid: str,
            txt: str,
            *,
            wait: bool = False,
            timeout_ms: int | None = None,
        ) -> dict[str, object]:
            sent_calls.append((pid, txt, wait, timeout_ms))
            return {}

        res = execute_send(
            by_id["w2"],
            prompt="Check tests",
            wait=False,
            timeout=30.0,
            force=False,
            dry_run=False,
            prompt_fn=record_prompt,
        )
        self.assertEqual(sent_calls, [("w2:p4", "Check tests", False, None)])
        self.assertEqual(res.pane_id, "w2:p4")
        self.assertEqual(res.prompt_chars, len("Check tests"))

    def test_sanitize_agent_name_avoids_collisions(self) -> None:
        name = sanitize_agent_name("demo-lead", {"demo-lead"})
        self.assertEqual(name, "demo-lead-2")
        numeric = sanitize_agent_name("1234", set())
        self.assertTrue(numeric[0].isalpha())


def _append_workspace(
    snapshot: dict,
    *,
    workspace_id: str,
    label: str,
    number: int,
    tab_id: str,
    pane_id: str,
    cwd: str,
    agent_kind: str | None = None,
    agent_status: str = "unknown",
    agent_name: str | None = None,
    interactive_ready: bool = False,
) -> dict:
    snap = copy.deepcopy(snapshot)
    snap["workspaces"].append(
        {
            "active_tab_id": tab_id,
            "agent_status": agent_status,
            "focused": False,
            "label": label,
            "number": number,
            "pane_count": 1,
            "tab_count": 1,
            "workspace_id": workspace_id,
        }
    )
    snap["tabs"].append(
        {
            "agent_status": agent_status,
            "focused": False,
            "label": f"1 [{tab_id.split(':')[-1]}]",
            "number": 1,
            "pane_count": 1,
            "tab_id": tab_id,
            "workspace_id": workspace_id,
        }
    )
    snap["panes"].append(
        {
            "agent": agent_kind,
            "agent_status": agent_status,
            "cwd": cwd,
            "foreground_cwd": cwd,
            "focused": False,
            "pane_id": pane_id,
            "revision": number * 100,
            "tab_id": tab_id,
            "workspace_id": workspace_id,
        }
    )
    if agent_kind is not None:
        agent_entry: dict[str, object] = {
            "agent": agent_kind,
            "agent_status": agent_status,
            "cwd": cwd,
            "interactive_ready": interactive_ready,
            "pane_id": pane_id,
            "revision": number * 100,
            "tab_id": tab_id,
            "workspace_id": workspace_id,
        }
        if agent_name is not None:
            agent_entry["name"] = agent_name
        snap["agents"].append(agent_entry)
    return snap


class CliWiringRegressionTests(unittest.TestCase):
    _JSON_MODE = OutputMode(fmt="json", color=False, is_tty_stdout=False, is_tty_stderr=False)

    def test_ensure_pr_codex_exercises_real_agent_start_signature(self) -> None:
        snap0 = load_fixture()
        snap1 = _append_workspace(
            snap0,
            workspace_id="w7",
            label="PR-8539",
            number=7,
            tab_id="w7:t1",
            pane_id="w7:p1",
            cwd="/tmp/pr-8539",
        )
        snap2 = _append_workspace(
            snap0,
            workspace_id="w7",
            label="PR-8539",
            number=7,
            tab_id="w7:t1",
            pane_id="w7:p1",
            cwd="/tmp/pr-8539",
            agent_kind="codex",
            agent_status="idle",
            agent_name="pr-8539",
            interactive_ready=True,
        )

        calls: list[list[str]] = []

        def fake_run(args: Sequence[str], **_kwargs: object) -> str:
            argv = list(args)
            calls.append(argv)
            if argv[0] == "cubrid-worktree":
                return json.dumps(
                    {
                        "data": {
                            "herdr": {"workspace_id": "w7", "tab_id": "w7:t1", "pane_id": "w7:p1"},
                            "path": "/tmp/pr-8539",
                        }
                    }
                )
            if argv[:3] == ["herdr", "agent", "start"]:
                return json.dumps({"result": {"agent": "codex"}})
            raise AssertionError(f"Unexpected subprocess call: {argv}")

        ns = build_parser().parse_args(["ensure", "--pr", "8539", "--launcher", "codex", "--json"])
        with patch("herdr_spaces_status.herdr_client._run_subprocess", side_effect=fake_run):
            raw_out = _dispatch(ns, FakeClient([snap0, snap1, snap2]), {}, "w4", self._JSON_MODE)

        self.assertEqual(
            calls,
            [
                ["cubrid-worktree", "review", "8539", "--no-focus", "--json"],
                [
                    "herdr",
                    "agent",
                    "start",
                    "pr-8539",
                    "--kind",
                    "codex",
                    "--pane",
                    "w7:p1",
                    "--timeout",
                    "45000",
                    "--",
                    "--profile",
                    "local",
                    "--dangerously-bypass-approvals-and-sandbox",
                ],
            ],
        )
        payload = json.loads(raw_out)
        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertEqual(data["workspace_action"], "create_cubrid_pr")
        self.assertEqual(data["agent_action"], "launch")
        self.assertEqual(data["workspace_id"], "w7")
        self.assertEqual(data["agent_kind"], "codex")
        self.assertEqual(data["agent_name"], "pr-8539")

    def test_ensure_ticket_claude_exercises_real_ticket_and_agent_start(self) -> None:
        snap0 = load_fixture()
        snap1 = _append_workspace(
            snap0,
            workspace_id="w8",
            label="CBRD-10004",
            number=8,
            tab_id="w8:t1",
            pane_id="w8:p1",
            cwd="/tmp/cb/CBRD-10004",
        )
        snap2 = _append_workspace(
            snap0,
            workspace_id="w8",
            label="CBRD-10004",
            number=8,
            tab_id="w8:t1",
            pane_id="w8:p1",
            cwd="/tmp/cb/CBRD-10004",
            agent_kind="claude",
            agent_status="idle",
            agent_name="cbrd-10004",
            interactive_ready=True,
        )

        calls: list[list[str]] = []

        def fake_run(args: Sequence[str], **_kwargs: object) -> str:
            argv = list(args)
            calls.append(argv)
            if argv[0] == "cubrid-worktree":
                return json.dumps(
                    {
                        "data": {
                            "herdr": {"workspace_id": "w8", "tab_id": "w8:t1", "pane_id": "w8:p1"},
                            "path": "/tmp/cb/CBRD-10004",
                            "ticket": "CBRD-10004",
                        }
                    }
                )
            if argv[:3] == ["herdr", "agent", "start"]:
                return json.dumps({"result": {"agent": "claude"}})
            raise AssertionError(f"Unexpected subprocess call: {argv}")

        with tempfile.TemporaryDirectory() as tmpdir:
            claude_dir = Path(tmpdir) / "custom-claude"
            expected_settings = str(claude_dir.resolve() / "local-settings.json")
            ns = build_parser().parse_args(
                [
                    "ensure",
                    "--ticket",
                    "CBRD-10004",
                    "--base",
                    "develop",
                    "--launcher",
                    "claude",
                    "--ready-timeout",
                    "30",
                    "--json",
                ]
            )
            with patch("herdr_spaces_status.herdr_client._run_subprocess", side_effect=fake_run):
                raw_out = _dispatch(
                    ns,
                    FakeClient([snap0, snap1, snap2]),
                    {"CLAUDE_CONFIG_DIR": str(claude_dir)},
                    "w4",
                    self._JSON_MODE,
                )

        self.assertEqual(
            calls,
            [
                [
                    "cubrid-worktree",
                    "create",
                    "CBRD-10004",
                    "--reuse",
                    "--no-focus",
                    "--json",
                    "--base",
                    "develop",
                ],
                [
                    "herdr",
                    "agent",
                    "start",
                    "cbrd-10004",
                    "--kind",
                    "claude",
                    "--pane",
                    "w8:p1",
                    "--timeout",
                    "30000",
                    "--",
                    "--settings",
                    expected_settings,
                    "--dangerously-skip-permissions",
                ],
            ],
        )
        payload = json.loads(raw_out)
        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertEqual(data["workspace_action"], "create_cubrid_ticket")
        self.assertEqual(data["agent_action"], "launch")
        self.assertEqual(data["agent_kind"], "claude")

    def test_ensure_cwd_ay_exercises_real_create_cwd_and_pane_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            resolved_dir = str(Path(tmpdir).resolve())
            label = Path(resolved_dir).name
            snap0 = load_fixture()
            snap1 = _append_workspace(
                snap0,
                workspace_id="w9",
                label=label,
                number=9,
                tab_id="w9:t1",
                pane_id="w9:p1",
                cwd=resolved_dir,
            )
            snap2 = _append_workspace(
                snap0,
                workspace_id="w9",
                label=label,
                number=9,
                tab_id="w9:t1",
                pane_id="w9:p1",
                cwd=resolved_dir,
                agent_kind="agy",
                agent_status="idle",
                agent_name=label.lower(),
                interactive_ready=True,
            )

            calls: list[list[str]] = []

            def fake_run(args: Sequence[str], **_kwargs: object) -> str:
                argv = list(args)
                calls.append(argv)
                if argv[:3] == ["herdr", "workspace", "create"]:
                    return json.dumps(
                        {
                            "result": {
                                "workspace": {"workspace_id": "w9", "label": label},
                                "tab": {"tab_id": "w9:t1"},
                                "root_pane": {"pane_id": "w9:p1"},
                            }
                        }
                    )
                if argv[:3] == ["herdr", "pane", "run"]:
                    return ""
                raise AssertionError(f"Unexpected subprocess call: {argv}")

            ns = build_parser().parse_args(
                ["ensure", "--cwd", resolved_dir, "--launcher", "ay", "--json"]
            )
            with (
                patch("herdr_spaces_status.herdr_client.shutil.which", return_value=None),
                patch("herdr_spaces_status.herdr_client._run_subprocess", side_effect=fake_run),
            ):
                raw_out = _dispatch(
                    ns,
                    FakeClient([snap0, snap1, snap2]),
                    {},
                    "w4",
                    self._JSON_MODE,
                )

        self.assertEqual(
            calls,
            [
                [
                    "herdr",
                    "workspace",
                    "create",
                    "--cwd",
                    resolved_dir,
                    "--label",
                    label,
                    "--no-focus",
                ],
                ["herdr", "pane", "run", "w9:p1", "ay"],
            ],
        )
        payload = json.loads(raw_out)
        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertEqual(data["workspace_action"], "create_cwd")
        self.assertEqual(data["agent_action"], "launch")
        self.assertEqual(data["agent_kind"], "agy")

    def test_send_exercises_real_agent_prompt_signature(self) -> None:
        snap = load_fixture()
        eligible_detection = "────────\n❯ \n────────\n⏵⏵ bypass permissions on"
        calls: list[list[str]] = []

        def fake_run(args: Sequence[str], **_kwargs: object) -> str:
            argv = list(args)
            calls.append(argv)
            if "--wait" in argv:
                return json.dumps({"result": {"agent_status": "done"}})
            return json.dumps({"result": {"status": "sent"}})

        ns_nowait = build_parser().parse_args(["send", "w2", "--prompt", "Run checks", "--json"])
        ns_wait = build_parser().parse_args(
            ["send", "w2", "--prompt", "Run checks", "--wait", "--timeout", "30", "--json"]
        )
        with patch("herdr_spaces_status.herdr_client._run_subprocess", side_effect=fake_run):
            out_nowait = _dispatch(
                ns_nowait,
                FakeClient([snap], detection_text=eligible_detection),
                {},
                "w4",
                self._JSON_MODE,
            )
            out_wait = _dispatch(
                ns_wait,
                FakeClient([snap], detection_text=eligible_detection),
                {},
                "w4",
                self._JSON_MODE,
            )

        self.assertEqual(
            calls,
            [
                ["herdr", "agent", "prompt", "w2:p4", "Run checks"],
                [
                    "herdr",
                    "agent",
                    "prompt",
                    "w2:p4",
                    "Run checks",
                    "--wait",
                    "--timeout",
                    "30000",
                ],
            ],
        )
        data_nowait = json.loads(out_nowait)["data"]
        self.assertFalse(data_nowait["waited"])
        data_wait = json.loads(out_wait)["data"]
        self.assertTrue(data_wait["waited"])
        self.assertEqual(data_wait["settled_status"], "done")


if __name__ == "__main__":
    unittest.main()
