from __future__ import annotations

import json
import unittest
from pathlib import Path

from herdr_spaces_status.models import CliError
from herdr_spaces_status.snapshot import (
    classify_claude_detection,
    correlate_work_tracker,
    filter_spaces,
    parse_snapshot,
    resolve_target,
    verify_claude_busy,
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


if __name__ == "__main__":
    unittest.main()
