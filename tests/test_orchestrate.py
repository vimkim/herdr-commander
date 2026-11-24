from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

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
    def __init__(self, snapshots: list[dict]) -> None:
        self._snapshots = snapshots
        self._idx = 0

    def snapshot(self) -> dict:
        snap = self._snapshots[min(self._idx, len(self._snapshots) - 1)]
        self._idx += 1
        return snap


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
            create_ticket=lambda _t, _b: {},
            create_pr=lambda _p: {},
            run_pane=lambda pid, cmd: ran.append((pid, cmd)),
            start_agent=lambda *_args: {},
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

        sent_calls: list[tuple[str, str]] = []
        res = execute_send(
            by_id["w2"],
            prompt="Check tests",
            wait=False,
            timeout=30.0,
            force=False,
            dry_run=False,
            prompt_fn=lambda pid, txt, _w, _t: sent_calls.append((pid, txt)) or {},
        )
        self.assertEqual(sent_calls, [("w2:p4", "Check tests")])
        self.assertEqual(res.pane_id, "w2:p4")
        self.assertEqual(res.prompt_chars, len("Check tests"))

    def test_sanitize_agent_name_avoids_collisions(self) -> None:
        name = sanitize_agent_name("demo-lead", {"demo-lead"})
        self.assertEqual(name, "demo-lead-2")
        numeric = sanitize_agent_name("1234", set())
        self.assertTrue(numeric[0].isalpha())


if __name__ == "__main__":
    unittest.main()
