from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr_commander.cli import _dispatch, _resolve_cap, build_parser
from herdr_commander.formatters import OutputMode
from herdr_commander.models import CliError
from herdr_commander.orchestrate import (
    choose_split_target,
    execute_pane_close,
    execute_pane_spawn,
    execute_tab_close,
    execute_tab_send,
    execute_tab_spawn,
    plan_pane_spawn,
    plan_tab_spawn,
)
from herdr_commander.snapshot import (
    count_session_working_agents,
    count_workspace_non_primary_agents,
    parse_workspace_tabs,
    resolve_caller_context,
    resolve_pane_target,
    resolve_tab_target,
    strip_tab_decorations,
)

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


class HierarchySnapshotTests(unittest.TestCase):
    def test_strip_tab_decorations_and_target_resolution(self) -> None:
        self.assertEqual(strip_tab_decorations("2 ▶ review [t2]"), "review")
        self.assertEqual(strip_tab_decorations("1 ! lead [t4]"), "lead")
        self.assertEqual(strip_tab_decorations("build-worker"), "build-worker")

        raw = load_fixture()
        raw["tabs"][1]["label"] = "2 ▶ build-worker [t2]"
        tabs = parse_workspace_tabs(raw, "w1", path_exists=lambda _p: True)

        self.assertEqual(resolve_tab_target(tabs, "t2").tab_id, "w1:t2")
        self.assertEqual(resolve_tab_target(tabs, "w1:p4").tab_id, "w1:t4")
        self.assertEqual(resolve_tab_target(tabs, "build-worker").tab_id, "w1:t2")

        w2_tabs = parse_workspace_tabs(raw, "w2", path_exists=lambda _p: True)
        self.assertEqual(resolve_pane_target(w2_tabs[0].panes, "p5").pane_id, "w2:p5")
        self.assertEqual(resolve_pane_target(w2_tabs[0].panes, "2").pane_id, "w2:p5")

    def test_resolve_caller_context_roles_and_non_t1_lead(self) -> None:
        raw = load_fixture()

        # w2:t4/w2:p4 is the first tab even though its ID is t4.
        lead_ctx = resolve_caller_context(
            raw,
            {"HERDR_WORKSPACE_ID": "w2", "HERDR_TAB_ID": "w2:t4", "HERDR_PANE_ID": "w2:p4"},
        )
        self.assertEqual(lead_ctx.role, "space_leader")

        # Worker env override forces tab_leader even if index were 0.
        worker_tab_env = resolve_caller_context(
            raw,
            {
                "HERDR_WORKSPACE_ID": "w2",
                "HERDR_TAB_ID": "w2:t4",
                "HERDR_PANE_ID": "w2:p4",
                "HERDR_TAB_ROLE": "worker",
            },
        )
        self.assertEqual(worker_tab_env.role, "tab_leader")

        # Non-primary tab primary pane -> tab_leader.
        tab_lead_ctx = resolve_caller_context(
            raw,
            {"HERDR_WORKSPACE_ID": "w1", "HERDR_TAB_ID": "w1:t2", "HERDR_PANE_ID": "w1:p2"},
        )
        self.assertEqual(tab_lead_ctx.role, "tab_leader")

        # Second pane in a tab -> leaf_worker.
        leaf_ctx = resolve_caller_context(
            raw,
            {"HERDR_WORKSPACE_ID": "w2", "HERDR_TAB_ID": "w2:t4", "HERDR_PANE_ID": "w2:p5"},
        )
        self.assertEqual(leaf_ctx.role, "leaf_worker")

        # HERDR_PANE_ROLE=worker forces leaf_worker.
        leaf_env_ctx = resolve_caller_context(
            raw,
            {
                "HERDR_WORKSPACE_ID": "w1",
                "HERDR_TAB_ID": "w1:t2",
                "HERDR_PANE_ID": "w1:p2",
                "HERDR_PANE_ROLE": "worker",
            },
        )
        self.assertEqual(leaf_env_ctx.role, "leaf_worker")

    def test_workspace_and_session_agent_counts(self) -> None:
        raw = load_fixture()
        tabs = parse_workspace_tabs(raw, "w1", path_exists=lambda _p: True)
        # w1:p1 is primary; w1:p2 (agy) and w1:p4 (codex) are non-primary agents.
        self.assertEqual(count_workspace_non_primary_agents(tabs), 2)
        # w1:p2 and w4:p1 are working in fixture.
        self.assertEqual(count_session_working_agents(raw), 2)


class HierarchyOrchestrateTests(unittest.TestCase):
    def test_tab_spawn_caps_and_role_guards(self) -> None:
        raw = load_fixture()
        tabs = parse_workspace_tabs(raw, "w1", self_tab_id="w1:t1", self_pane_id="w1:p1")
        space_lead = resolve_caller_context(
            raw,
            {"HERDR_WORKSPACE_ID": "w1", "HERDR_TAB_ID": "w1:t1", "HERDR_PANE_ID": "w1:p1"},
        )
        tab_lead = resolve_caller_context(
            raw,
            {"HERDR_WORKSPACE_ID": "w1", "HERDR_TAB_ID": "w1:t2", "HERDR_PANE_ID": "w1:p2"},
        )

        # Tab Leader cannot spawn tabs.
        with self.assertRaises(CliError) as ctx:
            plan_tab_spawn(
                caller=tab_lead,
                tabs=tabs,
                label="review",
                cwd="/tmp",
                launcher_name="ai-agent",
                dir_exists=lambda _p: True,
            )
        self.assertEqual(ctx.exception.code, "NOT_SPACE_LEADER")

        # Sub-tab cap enforced (w1 already has 2 sub-tabs).
        with self.assertRaises(CliError) as ctx:
            plan_tab_spawn(
                caller=space_lead,
                tabs=tabs,
                label="review",
                cwd="/tmp",
                launcher_name="shell",
                max_sub_tabs=2,
                dir_exists=lambda _p: True,
            )
        self.assertEqual(ctx.exception.code, "TAB_LIMIT_REACHED")

        # Space AI agent cap enforced for AI launchers, bypassed for shell.
        with self.assertRaises(CliError) as ctx:
            plan_tab_spawn(
                caller=space_lead,
                tabs=tabs,
                label="review",
                cwd="/tmp",
                launcher_name="ai-agent",
                max_space_agents=2,
                dir_exists=lambda _p: True,
            )
        self.assertEqual(ctx.exception.code, "SPACE_AGENT_LIMIT_REACHED")

        shell_plan = plan_tab_spawn(
            caller=space_lead,
            tabs=tabs,
            label="tests",
            cwd="/tmp",
            launcher_name="shell",
            max_space_agents=2,
            max_session_agents=1,
            dir_exists=lambda _p: True,
        )
        self.assertEqual(shell_plan.launcher, "shell")

        # Session working agent cap enforced for AI launchers.
        with self.assertRaises(CliError) as ctx:
            plan_tab_spawn(
                caller=space_lead,
                tabs=tabs,
                label="review",
                cwd="/tmp",
                launcher_name="ay",
                max_space_agents=4,
                max_session_agents=2,
                session_working_agents=2,
                dir_exists=lambda _p: True,
            )
        self.assertEqual(ctx.exception.code, "SESSION_AGENT_LIMIT_REACHED")

    def test_execute_tab_spawn_tags_worker_and_prompts(self) -> None:
        raw_before = load_fixture()
        raw_after = copy.deepcopy(raw_before)
        raw_after["tabs"].append(
            {
                "agent_status": "idle",
                "focused": False,
                "label": "review",
                "number": 5,
                "pane_count": 1,
                "tab_id": "w1:t5",
                "workspace_id": "w1",
            }
        )
        raw_after["panes"].append(
            {
                "agent": "agy",
                "agent_status": "idle",
                "cwd": "/tmp/repo",
                "foreground_cwd": "/tmp/repo",
                "focused": False,
                "pane_id": "w1:p5",
                "revision": 999,
                "tab_id": "w1:t5",
                "workspace_id": "w1",
            }
        )
        raw_after["agents"].append(
            {
                "agent": "agy",
                "agent_status": "idle",
                "cwd": "/tmp/repo",
                "interactive_ready": True,
                "name": "review",
                "pane_id": "w1:p5",
                "revision": 999,
                "tab_id": "w1:t5",
                "workspace_id": "w1",
            }
        )

        caller = resolve_caller_context(
            raw_before,
            {"HERDR_WORKSPACE_ID": "w1", "HERDR_TAB_ID": "w1:t1", "HERDR_PANE_ID": "w1:p1"},
        )
        tabs = parse_workspace_tabs(raw_before, "w1", self_tab_id="w1:t1", self_pane_id="w1:p1")
        plan = plan_tab_spawn(
            caller=caller,
            tabs=tabs,
            label="review",
            cwd="/tmp/repo",
            launcher_name="ai-agent",
            prompt="Review current diff.",
            dir_exists=lambda _p: True,
        )

        calls: list[tuple[str, object]] = []

        def fake_create_tab(workspace_id: str, *, label: str, cwd: str, env_vars=None):
            calls.append(("tab_create", (workspace_id, label, cwd, dict(env_vars or {}))))
            return {"workspace_id": workspace_id, "tab_id": "w1:t5", "pane_id": "w1:p5"}

        def fake_run_pane(pane_id: str, command: str):
            calls.append(("pane_run", (pane_id, command)))

        def fake_prompt(pane_id: str, prompt: str, *, wait: bool = False, timeout_ms=None):
            calls.append(("agent_prompt", (pane_id, prompt, wait)))
            return {}

        res = execute_tab_spawn(
            plan,
            client=FakeClient([raw_after]),
            create_tab=fake_create_tab,
            run_pane=fake_run_pane,
            start_agent=lambda *_a, **_kw: {},
            prompt_fn=fake_prompt,
            env={},
            select_backend=lambda: "ay",
            sleep_fn=lambda _s: None,
        )
        self.assertEqual(res.tab_id, "w1:t5")
        self.assertEqual(res.pane_id, "w1:p5")
        self.assertEqual(res.agent_kind, "agy")
        self.assertTrue(res.prompt_sent)
        self.assertEqual(
            calls[0],
            ("tab_create", ("w1", "review", "/tmp/repo", {"HERDR_TAB_ROLE": "worker"})),
        )
        self.assertEqual(calls[1], ("pane_run", ("w1:p5", "ai-agent")))
        self.assertEqual(calls[2], ("agent_prompt", ("w1:p5", "Review current diff.", False)))

    def test_pane_2x2_auto_layout_and_worker_caps(self) -> None:
        raw = load_fixture()
        tabs = parse_workspace_tabs(raw, "w2", self_tab_id="w2:t4", self_pane_id="w2:p4")
        tab = tabs[0]
        # 1 pane -> right from p1
        self.assertEqual(choose_split_target(tab.panes[:1], "auto"), ("w2:p4", "right"))
        # 2 panes -> down from p2
        self.assertEqual(choose_split_target(tab.panes, "auto"), ("w2:p5", "down"))
        # 3 panes -> down from p1
        three_panes = (*tab.panes, tab.panes[1])
        self.assertEqual(choose_split_target(three_panes, "auto"), ("w2:p4", "down"))

        leaf = resolve_caller_context(
            raw,
            {"HERDR_WORKSPACE_ID": "w2", "HERDR_TAB_ID": "w2:t4", "HERDR_PANE_ID": "w2:p5"},
        )
        with self.assertRaises(CliError) as ctx:
            plan_pane_spawn(
                caller=leaf,
                tabs=tabs,
                cwd="/tmp",
                launcher_name="shell",
                dir_exists=lambda _p: True,
            )
        self.assertEqual(ctx.exception.code, "NOT_TAB_LEADER")

        tab_lead = resolve_caller_context(
            raw,
            {
                "HERDR_WORKSPACE_ID": "w2",
                "HERDR_TAB_ID": "w2:t4",
                "HERDR_PANE_ID": "w2:p4",
                "HERDR_TAB_ROLE": "worker",
            },
        )
        with self.assertRaises(CliError) as ctx:
            plan_pane_spawn(
                caller=tab_lead,
                tabs=tabs,
                cwd="/tmp",
                launcher_name="shell",
                max_worker_panes=1,
                dir_exists=lambda _p: True,
            )
        self.assertEqual(ctx.exception.code, "PANE_LIMIT_REACHED")

    def test_close_guards_for_primary_tab_and_primary_pane(self) -> None:
        raw = load_fixture()
        space_lead = resolve_caller_context(
            raw,
            {"HERDR_WORKSPACE_ID": "w1", "HERDR_TAB_ID": "w1:t1", "HERDR_PANE_ID": "w1:p1"},
        )
        tabs = parse_workspace_tabs(raw, "w1", self_tab_id="w1:t1", self_pane_id="w1:p1")

        with self.assertRaises(CliError) as ctx:
            execute_tab_close(
                caller=space_lead,
                tab=tabs[0],
                dry_run=False,
                close_tab=lambda _id: None,
            )
        self.assertEqual(ctx.exception.code, "CANNOT_CLOSE_PRIMARY_TAB")

        closed: list[str] = []
        res = execute_tab_close(
            caller=space_lead,
            tab=tabs[1],
            dry_run=False,
            close_tab=closed.append,
        )
        self.assertEqual(res.tab_id, "w1:t2")
        self.assertEqual(closed, ["w1:t2"])

        w2_lead = resolve_caller_context(
            raw,
            {"HERDR_WORKSPACE_ID": "w2", "HERDR_TAB_ID": "w2:t4", "HERDR_PANE_ID": "w2:p4"},
        )
        w2_tabs = parse_workspace_tabs(raw, "w2", self_tab_id="w2:t4", self_pane_id="w2:p4")
        with self.assertRaises(CliError) as ctx:
            execute_pane_close(
                caller=w2_lead,
                pane=w2_tabs[0].panes[0],
                dry_run=False,
                close_pane=lambda _id: None,
            )
        self.assertEqual(ctx.exception.code, "CANNOT_CLOSE_PRIMARY_PANE")

        with self.assertRaises(CliError) as ctx:
            execute_tab_send(
                caller=space_lead,
                tab=tabs[0],
                prompt="hi",
                wait=False,
                timeout=30.0,
                force=False,
                dry_run=True,
                prompt_fn=lambda *_a, **_kw: {},
            )
        self.assertEqual(ctx.exception.code, "CANNOT_SEND_TO_SELF")

    def test_cli_tab_and_pane_dry_run_dispatch(self) -> None:
        raw = load_fixture()
        parser = build_parser()
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "repo"
            repo_dir.mkdir()
            for item in (*raw["panes"], *raw["agents"]):
                if item["workspace_id"] == "w1":
                    item["cwd"] = str(repo_dir)
                    if "foreground_cwd" in item:
                        item["foreground_cwd"] = str(repo_dir)

            client = FakeClient([raw])
            mode = OutputMode("json", False, False, False)
            env = {"HERDR_WORKSPACE_ID": "w1", "HERDR_TAB_ID": "w1:t1", "HERDR_PANE_ID": "w1:p1"}

            ns_tab = parser.parse_args(
                ["tab", "spawn", "--label", "ci", "--launcher", "ai-agent", "--dry-run", "--json"]
            )
            with patch("herdr_commander.cli.ai_agent_select_backend", return_value="ai-round"):
                tab_out = json.loads(_dispatch(ns_tab, client, env, "w1", mode))
            self.assertTrue(tab_out["data"]["dry_run"])
            self.assertEqual(tab_out["data"]["agent_kind"], "claude")

            ns_pane = parser.parse_args(
                [
                    "pane",
                    "spawn",
                    "--launcher",
                    "shell",
                    "--cmd",
                    "just test",
                    "--dry-run",
                    "--json",
                ]
            )
            pane_out = json.loads(_dispatch(ns_pane, client, env, "w1", mode))
            self.assertTrue(pane_out["data"]["dry_run"])
            self.assertEqual(pane_out["data"]["direction"], "right")
            self.assertEqual(pane_out["data"]["shell_cmd"], "just test")

            # Execute pane spawn shell runs pane_split + pane_run.
            split_calls: list[tuple[object, ...]] = []
            run_calls: list[tuple[str, str]] = []
            tabs = parse_workspace_tabs(raw, "w1", self_tab_id="w1:t1", self_pane_id="w1:p1")
            caller = resolve_caller_context(raw, env)
            plan = plan_pane_spawn(
                caller=caller,
                tabs=tabs,
                cwd=str(repo_dir),
                launcher_name="shell",
                shell_cmd="just test",
            )
            pane_res = execute_pane_spawn(
                plan,
                client=client,
                split_pane=lambda pid, *, direction, cwd, env_vars=None: (
                    split_calls.append((pid, direction, cwd, dict(env_vars or {})))
                    or {"pane_id": "w1:p9"}
                ),
                run_pane=lambda pid, cmd: run_calls.append((pid, cmd)),
                start_agent=lambda *_a, **_kw: {},
                prompt_fn=lambda *_a, **_kw: {},
                env=env,
            )
            self.assertEqual(pane_res.pane_id, "w1:p9")
            self.assertEqual(split_calls[0][3], {"HERDR_PANE_ROLE": "worker"})
            self.assertEqual(run_calls, [("w1:p9", "just test")])

    def test_cap_env_var_precedence_and_legacy_fallback(self) -> None:
        vars_tuple = ("HCMD_MAX_SUB_TABS", "HSS_MAX_SUB_TABS")
        self.assertEqual(_resolve_cap(None, {}, vars_tuple, 8), 8)
        self.assertEqual(_resolve_cap(None, {"HSS_MAX_SUB_TABS": "5"}, vars_tuple, 8), 5)
        self.assertEqual(
            _resolve_cap(None, {"HCMD_MAX_SUB_TABS": "6", "HSS_MAX_SUB_TABS": "5"}, vars_tuple, 8),
            6,
        )
        self.assertEqual(_resolve_cap(3, {"HCMD_MAX_SUB_TABS": "6"}, vars_tuple, 8), 3)


if __name__ == "__main__":
    unittest.main()
