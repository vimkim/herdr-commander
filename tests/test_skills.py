from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOT = REPO_ROOT / "skills"
JUSTFILE_PATH = REPO_ROOT / "justfile"

EXPECTED_SKILLS = (
    "herdr-commander",
    "herdr-commander-init",
    "hcmd-init",
    "hcmd-check",
    "hcmd-auto",
)


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, flags=re.DOTALL)
    assert match is not None, "Missing YAML frontmatter delimiters"
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip()
    return meta, match.group(2)


class SkillPackagingTests(unittest.TestCase):
    def test_all_expected_skills_have_valid_frontmatter(self) -> None:
        discovered = sorted(p.name for p in SKILL_ROOT.iterdir() if (p / "SKILL.md").is_file())
        self.assertEqual(discovered, sorted(EXPECTED_SKILLS))
        for name in EXPECTED_SKILLS:
            text = (SKILL_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
            meta, body = _split_frontmatter(text)
            self.assertEqual(meta.get("name"), name)
            self.assertTrue(meta.get("description"))
            self.assertTrue(body.strip())

    def test_hcmd_init_matches_herdr_commander_init(self) -> None:
        canonical = (SKILL_ROOT / "herdr-commander-init" / "SKILL.md").read_text(encoding="utf-8")
        alias = (SKILL_ROOT / "hcmd-init" / "SKILL.md").read_text(encoding="utf-8")
        meta_c, body_c = _split_frontmatter(canonical)
        meta_a, body_a = _split_frontmatter(alias)
        self.assertEqual(meta_c.get("description"), meta_a.get("description"))
        self.assertEqual(body_c, body_a)

    def test_justfile_install_skill_iterates_over_skill_glob(self) -> None:
        just_text = JUSTFILE_PATH.read_text(encoding="utf-8")
        self.assertIn('for src in "$skill_root"/*', just_text)
        self.assertIn('[ -d "$src" ] && [ -f "$src/SKILL.md" ]', just_text)

    def test_hcmd_auto_documents_standing_allowlist_guardrails(self) -> None:
        text = (SKILL_ROOT / "hcmd-auto" / "SKILL.md").read_text(encoding="utf-8")
        required_markers = (
            "#### Conditional Standing Allowlist (CUBRID PR Sync & CI)",
            "vk/*",
            "tc/pr-<N>",
            "/run all",
            "/run rerun <run-id>",
            "cubrid-ci-watch register",
            "git merge origin/develop",
            "--force-with-lease",
            "conflict",
            "just build",
            "cubrid-pr-tc-base-check --pr <N>",
            "cubrid-tcr blockers",
            "READY",
            "max 2",
            "auto-audit.jsonl",
            "hcmd rotate --auto-config",
            "[AUTO-POLICY: standing-approved]",
        )
        for marker in required_markers:
            self.assertIn(marker, text)


if __name__ == "__main__":
    unittest.main()
