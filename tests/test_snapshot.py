from __future__ import annotations

import json
import unittest
from pathlib import Path

from herdr_commander.models import CliError
from herdr_commander.snapshot import (
    classify_claude_detection,
    collect_workspace_panes,
    correlate_work_tracker,
    filter_spaces,
    find_matching_spaces,
    has_active_work,
    parse_self_context_statusline,
    parse_snapshot,
    resolve_peek_target,
    resolve_target,
    verify_claude_busy,
    verify_subtabs_claude_busy,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "snapshot_sample.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class SnapshotTests(unittest.TestCase):
    def test_first_tab_and_sub_tab_rollup(self) -> None:
        existing_paths = {
            "/home/user/gh/cb/CBRD-10001-demo",
            "/home/user/gh/demo-skills",
            "/home/user/gh/herdr-spaces-status",
            "/home/user/gh/cb/CBRD-10002",
            "/home/user/gh/cb/CBRD-10003",
        }
        spaces = parse_snapshot(
            load_fixture(),
            self_workspace_id="w4",
            path_exists=lambda p: p in existing_paths,
        )
        by_id = {s.workspace_id: s for s in spaces}

        # Non-t1 first tab (number=4, tab_id=w2:t4) with split pane p5 counted in rollup.
        w2 = by_id["w2"]
        self.assertEqual(w2.primary_tab_id, "w2:t4")
        self.assertIsNotNone(w2.primary_pane)
        assert w2.primary_pane is not None
        self.assertEqual(w2.primary_pane.pane_id, "w2:p4")
        self.assertEqual(w2.primary_pane.agent_kind, "claude")
        self.assertEqual(w2.sub_tabs.extra_tab_count, 0)
        self.assertEqual(w2.sub_tabs.extra_pane_count, 1)
        self.assertEqual(w2.sub_tabs.status_counts, {"shell": 1})

        # Multi-tab workspace: primary tab is done, sub-tabs have working + blocked.
        w1 = by_id["w1"]
        self.assertEqual(w1.attention, "done")
        self.assertEqual(w1.ticket_key, "CBRD-10001")
        self.assertEqual(w1.sub_tabs.extra_tab_count, 2)
        self.assertEqual(w1.sub_tabs.extra_pane_count, 2)
        self.assertTrue(w1.sub_tabs.any_working)
        self.assertTrue(w1.sub_tabs.any_blocked)
        self.assertEqual(w1.sub_tabs.status_counts, {"working": 1, "blocked": 1})
        self.assertEqual(
            w1.sub_tabs.blocked_panes,
            (
                {
                    "tab_id": "w1:t4",
                    "tab_label": "3 tc-pr-1234",
                    "pane_id": "w1:p4",
                    "revision": 102,
                },
            ),
        )

        # Deleted workspace becomes unknown attention.
        w3 = by_id["w3"]
        self.assertTrue(w3.deleted_hint)
        self.assertEqual(w3.attention, "unknown")

        # Empty shell pane has effective_status=shell and attention=shell.
        w5 = by_id["w5"]
        assert w5.primary_pane is not None
        self.assertIsNone(w5.primary_pane.agent_kind)
        self.assertEqual(w5.primary_pane.effective_status, "shell")
        self.assertEqual(w5.attention, "shell")

        # Self marker.
        self.assertTrue(by_id["w4"].is_self)
        self.assertFalse(w1.is_self)

    def test_claude_detection_classifier(self) -> None:
        busy_footer = "\n".join(
            [
                "────────────────────────",
                "❯ ",
                "────────────────────────",
                "⏵⏵ bypass permissions on · 2 background agents",
            ]
        )
        self.assertEqual(classify_claude_detection(busy_footer), "claude_busy")

        busy_live = "\n".join(
            [
                "✻ Waiting for 1 background agent to finish...",
                "────────────────────────",
                "❯ ",
                "────────────────────────",
                "⏵⏵ bypass permissions on",
            ]
        )
        self.assertEqual(classify_claude_detection(busy_live), "claude_busy")

        idle_clean = "\n".join(
            [
                "Done.",
                "────────────────────────",
                "❯ ",
                "────────────────────────",
                "⏵⏵ bypass permissions on",
            ]
        )
        self.assertEqual(classify_claude_detection(idle_clean), "eligible")
        self.assertEqual(classify_claude_detection("plain shell output"), "unverified")

    def test_verify_claude_busy_promotes_only_busy_claude_panes(self) -> None:
        spaces = parse_snapshot(load_fixture(), path_exists=lambda _p: True)

        def fake_read(pane_id: str) -> str:
            if pane_id == "w6:p2":
                return "────────\n❯ \n────────\n⏵⏵ bypass on · 1 background task"
            return "────────\n❯ \n────────\n⏵⏵ bypass on"

        verified = verify_claude_busy(spaces, fake_read)
        by_id = {s.workspace_id: s for s in verified}

        assert by_id["w6"].primary_pane is not None
        self.assertEqual(by_id["w6"].primary_pane.effective_status, "busy_bg")
        self.assertEqual(by_id["w6"].attention, "working")

        assert by_id["w2"].primary_pane is not None
        self.assertEqual(by_id["w2"].primary_pane.effective_status, "idle")
        self.assertEqual(by_id["w2"].attention, "idle")

    def test_correlate_work_tracker(self) -> None:
        spaces = parse_snapshot(load_fixture(), path_exists=lambda _p: True)
        items = [
            {
                "id": 101,
                "title": "Fix issue in CBRD-10001",
                "workdir": "/home/user/gh/cb/CBRD-10001-demo",
            },
            {
                "id": 202,
                "title": "Unrelated home cleanup",
                "workdir": "/home/user",
            },
            {
                "id": 303,
                "title": "CBRD-10003 investigation",
                "workdir": "/home/user",
            },
        ]
        correlated = correlate_work_tracker(spaces, items, home_dir="/home/user")
        by_id = {s.workspace_id: s for s in correlated}
        self.assertEqual(by_id["w1"].work_item_ids, (101,))
        self.assertEqual(by_id["w6"].work_item_ids, (303,))
        self.assertEqual(by_id["w2"].work_item_ids, ())

    def test_resolve_target_and_filters(self) -> None:
        spaces = parse_snapshot(
            load_fixture(),
            self_workspace_id="w4",
            path_exists=lambda p: "old-experiment" not in p,
        )
        self.assertEqual(resolve_target(spaces, "w2").workspace_id, "w2")
        self.assertEqual(resolve_target(spaces, "DEMO-SKILLS").workspace_id, "w2")
        self.assertEqual(resolve_target(spaces, "cbrd-10001").workspace_id, "w1")

        with self.assertRaises(CliError) as ctx:
            resolve_target(spaces, "does-not-exist")
        self.assertEqual(ctx.exception.code, "SPACE_NOT_FOUND")

        workers = filter_spaces(spaces, workers_only=True, sort="attention")
        self.assertNotIn("w4", [s.workspace_id for s in workers])
        # w1 has sub_tabs.any_blocked=True, so it sorts before shell/idle/unknown.
        self.assertEqual(workers[0].workspace_id, "w1")

        attn = filter_spaces(spaces, workers_only=True, attention_only=True)
        self.assertNotIn("w3", [s.workspace_id for s in attn])
        self.assertIn("w5", [s.workspace_id for s in attn])

    def test_find_matching_spaces_by_label_name_cwd_ticket_and_pr(self) -> None:
        raw = load_fixture()
        raw["workspaces"].append(
            {
                "active_tab_id": "w7:t1",
                "agent_status": "idle",
                "focused": False,
                "label": "review-pr-1234",
                "number": 7,
                "pane_count": 1,
                "tab_count": 1,
                "workspace_id": "w7",
            }
        )
        raw["tabs"].append(
            {
                "agent_status": "idle",
                "focused": False,
                "label": "1 [t1]",
                "number": 1,
                "pane_count": 1,
                "tab_id": "w7:t1",
                "workspace_id": "w7",
            }
        )
        raw["panes"].append(
            {
                "agent": "claude",
                "agent_status": "idle",
                "cwd": "/home/user/gh/cb/PR-12345",
                "foreground_cwd": "/home/user/gh/cb/PR-12345/build",
                "focused": False,
                "pane_id": "w7:p1",
                "revision": 700,
                "tab_id": "w7:t1",
                "workspace_id": "w7",
            }
        )
        raw["agents"].append(
            {
                "agent": "claude",
                "agent_status": "idle",
                "cwd": "/home/user/gh/cb/PR-12345",
                "interactive_ready": True,
                "name": "pr-1234-review",
                "pane_id": "w7:p1",
                "revision": 700,
                "tab_id": "w7:t1",
                "workspace_id": "w7",
            }
        )
        # Set foreground_cwd different from cwd on w2:p4 to verify foreground_cwd matching.
        for pane in raw["panes"]:
            if pane["pane_id"] == "w2:p4":
                pane["foreground_cwd"] = "/home/user/gh/demo-skills/subdir"

        spaces = parse_snapshot(
            raw,
            self_workspace_id="w4",
            path_exists=lambda p: "old-experiment" not in p,
        )
        panes_by_ws = collect_workspace_panes(raw, path_exists=lambda p: "old-experiment" not in p)

        # Label exact + case-insensitive + partial ordering & deduplication.
        res_label = find_matching_spaces(
            spaces,
            label="DEMO-SKILLS",
            cwd="/home/user/gh/demo-skills",
            panes_by_workspace=panes_by_ws,
            home_dir="/home/user",
        )
        self.assertTrue(res_label.found)
        self.assertEqual(res_label.recommendation, "ask_reuse")
        self.assertEqual(len(res_label.matches), 1)
        self.assertEqual(res_label.matches[0].space.workspace_id, "w2")
        self.assertEqual(res_label.matches[0].match_strength, "exact")
        self.assertIn("label_ci_exact", res_label.matches[0].match_reasons)
        self.assertIn("cwd_exact", res_label.matches[0].match_reasons)

        # Primary agent name vs sub-pane agent name.
        res_primary_agent = find_matching_spaces(
            spaces, name="demo-lead", panes_by_workspace=panes_by_ws
        )
        self.assertEqual([m.space.workspace_id for m in res_primary_agent.matches], ["w1"])
        self.assertIn("agent_name_exact", res_primary_agent.matches[0].match_reasons)

        res_sub_agent = find_matching_spaces(
            spaces, name="build-worker", panes_by_workspace=panes_by_ws
        )
        self.assertEqual([m.space.workspace_id for m in res_sub_agent.matches], ["w1"])
        self.assertEqual(res_sub_agent.matches[0].match_reasons, ("sub_pane_agent_name_exact",))
        self.assertEqual(res_sub_agent.matches[0].matched_panes, ("w1:p2",))

        # Foreground cwd exact and symlink realpath matching.
        res_fg = find_matching_spaces(
            spaces,
            cwd="/home/user/gh/demo-skills/subdir",
            panes_by_workspace=panes_by_ws,
            home_dir="/home/user",
        )
        self.assertEqual([m.space.workspace_id for m in res_fg.matches], ["w2"])
        self.assertIn("foreground_cwd_exact", res_fg.matches[0].match_reasons)

        res_symlink = find_matching_spaces(
            spaces,
            cwd="/var/link/demo-skills",
            panes_by_workspace=panes_by_ws,
            home_dir="/home/user",
            realpath_fn=lambda p: (
                "/home/user/gh/demo-skills" if p == "/var/link/demo-skills" else p
            ),
        )
        self.assertEqual([m.space.workspace_id for m in res_symlink.matches], ["w2"])
        self.assertIn("cwd_exact", res_symlink.matches[0].match_reasons)

        # Subdir matching works for repo parent, but $HOME never causes false subdir matches.
        res_parent = find_matching_spaces(
            spaces,
            cwd="/home/user/gh/cb",
            panes_by_workspace=panes_by_ws,
            home_dir="/home/user",
        )
        self.assertEqual(
            {m.space.workspace_id for m in res_parent.matches}, {"w1", "w5", "w6", "w7"}
        )
        for m in res_parent.matches:
            self.assertIn("cwd_subdir", m.match_reasons)
            self.assertEqual(m.match_strength, "partial")

        res_home = find_matching_spaces(
            spaces,
            cwd="/home/user",
            panes_by_workspace=panes_by_ws,
            home_dir="/home/user",
        )
        self.assertFalse(res_home.found)
        self.assertEqual(res_home.recommendation, "create_new")

        # Ticket and PR boundary matching (1234 must not match cwd PR-12345).
        res_ticket = find_matching_spaces(spaces, ticket="cbrd-10001")
        self.assertEqual([m.space.workspace_id for m in res_ticket.matches], ["w1"])
        self.assertEqual(res_ticket.matches[0].match_reasons, ("ticket_exact",))
        self.assertEqual(res_ticket.recommendation, "warn_busy")

        res_pr = find_matching_spaces(spaces, pr_number="#1234")
        self.assertEqual([m.space.workspace_id for m in res_pr.matches], ["w7"])
        self.assertIn("pr_label_match", res_pr.matches[0].match_reasons)
        self.assertIn("pr_agent_name_match", res_pr.matches[0].match_reasons)
        self.assertNotIn("pr_cwd_match", res_pr.matches[0].match_reasons)
        self.assertEqual(res_pr.recommendation, "ask_reuse")

        # Positional query matching path-like string and empty shell recommendation.
        res_path_query = find_matching_spaces(
            spaces,
            query="/home/user/gh/cb/CBRD-10002",
            home_dir="/home/user",
        )
        self.assertEqual([m.space.workspace_id for m in res_path_query.matches], ["w5"])
        self.assertIn("cwd_exact", res_path_query.matches[0].match_reasons)
        self.assertEqual(res_path_query.recommendation, "reuse_empty_shell")

        # Partial match on an empty shell recommends ask_reuse rather than auto reuse_empty_shell.
        res_partial_shell = find_matching_spaces(spaces, label="10002")
        self.assertEqual([m.space.workspace_id for m in res_partial_shell.matches], ["w5"])
        self.assertEqual(res_partial_shell.matches[0].match_strength, "partial")
        self.assertEqual(res_partial_shell.recommendation, "ask_reuse")

        # busy_bg or working primary pane triggers warn_busy.
        verified_spaces = verify_claude_busy(
            spaces,
            lambda pid: (
                "────────\n❯ \n────────\n⏵⏵ bypass on · 1 background agent"
                if pid == "w6:p2"
                else "────────\n❯ \n────────\n⏵⏵ bypass on"
            ),
        )
        res_busy_bg = find_matching_spaces(verified_spaces, ticket="CBRD-10003")
        self.assertEqual([m.space.workspace_id for m in res_busy_bg.matches], ["w6"])
        self.assertEqual(res_busy_bg.recommendation, "warn_busy")

        # Default exclusions of self (w4) and deleted (w3), and opt-in flags.
        self.assertFalse(find_matching_spaces(spaces, label="herdr-spaces-status").found)
        self.assertTrue(
            find_matching_spaces(spaces, label="herdr-spaces-status", include_self=True).found
        )
        self.assertFalse(find_matching_spaces(spaces, label="old-experiment").found)
        self.assertTrue(
            find_matching_spaces(spaces, label="old-experiment", include_deleted=True).found
        )

        # Error handling.
        with self.assertRaises(CliError) as missing_err:
            find_matching_spaces(spaces, query="   ", label="")
        self.assertEqual(missing_err.exception.code, "MISSING_QUERY")

        with self.assertRaises(CliError) as bad_pr_err:
            find_matching_spaces(spaces, pr_number="abc")
        self.assertEqual(bad_pr_err.exception.code, "INVALID_PR")

    def test_resolve_peek_target_primary_tab_and_pane(self) -> None:
        raw = load_fixture()
        spaces = parse_snapshot(raw, self_workspace_id="w4", path_exists=lambda _p: True)

        # Default resolves primary tab & primary pane.
        space, tid, pane = resolve_peek_target(raw, spaces, "w1")
        self.assertEqual((space.workspace_id, tid, pane.pane_id), ("w1", "w1:t1", "w1:p1"))

        # --tab resolves by label or tab ID.
        _, tid_tab, pane_tab = resolve_peek_target(raw, spaces, "w1", tab_selector="3 tc-pr-1234")
        self.assertEqual((tid_tab, pane_tab.pane_id), ("w1:t4", "w1:p4"))

        # --tab by 1-based position.
        _, tid_pos, pane_pos = resolve_peek_target(raw, spaces, "w1", tab_selector="2")
        self.assertEqual((tid_pos, pane_pos.pane_id), ("w1:t2", "w1:p2"))

        # --pane across workspace tabs.
        _, tid_p, pane_p = resolve_peek_target(raw, spaces, "w1", pane_selector="p4")
        self.assertEqual((tid_p, pane_p.pane_id), ("w1:t4", "w1:p4"))

        # --tab + --pane in multi-pane tab.
        _, tid_sub, pane_sub = resolve_peek_target(
            raw, spaces, "w2", tab_selector="t4", pane_selector="2"
        )
        self.assertEqual((tid_sub, pane_sub.pane_id), ("w2:t4", "w2:p5"))

        with self.assertRaises(CliError) as missing_tab:
            resolve_peek_target(raw, spaces, "w1", tab_selector="nope")
        self.assertEqual(missing_tab.exception.code, "TAB_NOT_FOUND")

        with self.assertRaises(CliError) as missing_pane:
            resolve_peek_target(raw, spaces, "w1", pane_selector="p99")
        self.assertEqual(missing_pane.exception.code, "PANE_NOT_FOUND")

    def test_parse_self_context_statusline_variants(self) -> None:
        ay_line = (
            "some output\n"
            "\x1b[1mOrchestrator (Neon) [bypass]\x1b[0m │ ~/gh/herdr-commander (main) │ "
            "\x1b[32mctx ██░░░░░░░░ 17%\x1b[0m \x1b[2m181k/1.0M\x1b[0m │ "
            "gemini-5h 81% ·3h1m │ gemini-7d 72% ·4d23h\n"
        )
        ay_res = parse_self_context_statusline(ay_line)
        self.assertTrue(ay_res["parsed"])
        self.assertEqual(ay_res["model_display"], "Orchestrator (Neon) [bypass]")
        self.assertEqual(ay_res["ctx_percent"], 17)
        self.assertEqual(ay_res["tokens_used"], 181_000)
        self.assertEqual(ay_res["tokens_limit"], 1_000_000)
        self.assertEqual(ay_res["tokens_display"], "181k/1.0M")
        self.assertEqual(
            ay_res["rate_limits"],
            {
                "5h": {"remaining_percent": 81, "resets_in": "3h1m"},
                "7d": {"remaining_percent": 72, "resets_in": "4d23h"},
            },
        )

        claude_line = (
            "Opus 4.1 │ ~/gh/herdr-commander (feat) │ "
            "ctx ███░░░░░░░ 32% 64k/200k │ 5h 74% ·2h15m │ 7d 88% ·4d10h"
        )
        claude_res = parse_self_context_statusline(claude_line)
        self.assertTrue(claude_res["parsed"])
        self.assertEqual(claude_res["model_display"], "Opus 4.1")
        self.assertEqual(claude_res["ctx_percent"], 32)
        self.assertEqual(claude_res["tokens_used"], 64_000)
        self.assertEqual(claude_res["tokens_limit"], 200_000)

        pct_only = parse_self_context_statusline("Opus │ ~/repo │ ctx ███░░░░░░░ 45%")
        self.assertTrue(pct_only["parsed"])
        self.assertEqual(pct_only["ctx_percent"], 45)
        self.assertIsNone(pct_only["tokens_used"])

        fallback_ratio = parse_self_context_statusline("model │ ~/repo │ 250k/1.0M")
        self.assertTrue(fallback_ratio["parsed"])
        self.assertEqual(fallback_ratio["ctx_percent"], 25)

        unparsed = parse_self_context_statusline("plain shell prompt\n$ ls -la\n")
        self.assertFalse(unparsed["parsed"])
        self.assertIsNone(unparsed["ctx_percent"])

    def test_has_active_work_and_subtab_busy_verification(self) -> None:
        raw = load_fixture()
        spaces = parse_snapshot(raw, self_workspace_id="w4", path_exists=lambda _p: True)
        workers = [s for s in spaces if not s.is_self]
        # w1 has sub_tabs.any_working == True.
        self.assertTrue(has_active_work(workers))

        # Keep only w6 (idle claude) and w5 (shell) -> idle until verified busy_bg.
        idle_only = [s for s in workers if s.workspace_id in {"w5", "w6"}]
        self.assertFalse(has_active_work(idle_only))
        verified = verify_claude_busy(
            idle_only,
            lambda _pid: "────────\n❯ \n────────\n⏵⏵ bypass on · 1 background agent",
        )
        self.assertTrue(has_active_work(verified))

        # Sub-tab claude background busy promotion.
        raw_sub = load_fixture()
        for t in raw_sub["tabs"]:
            if t["tab_id"] == "w1:t2":
                t["agent_status"] = "idle"
        for p in raw_sub["panes"]:
            if p["pane_id"] == "w1:p2":
                p["agent"] = "claude"
                p["agent_status"] = "idle"
        for a in raw_sub["agents"]:
            if a["pane_id"] == "w1:p2":
                a["agent"] = "claude"
                a["agent_status"] = "idle"
        sub_spaces = [
            s
            for s in parse_snapshot(raw_sub, self_workspace_id="w4", path_exists=lambda _p: True)
            if s.workspace_id == "w1"
        ]
        self.assertFalse(sub_spaces[0].sub_tabs.any_working)
        promoted = verify_subtabs_claude_busy(
            raw_sub,
            sub_spaces,
            lambda pid: (
                "────────\n❯ \n────────\n⏵⏵ bypass on · 1 background agent"
                if pid == "w1:p2"
                else ""
            ),
        )
        self.assertTrue(promoted[0].sub_tabs.any_working)
        self.assertEqual(promoted[0].sub_tabs.status_counts["working"], 1)


if __name__ == "__main__":
    unittest.main()
