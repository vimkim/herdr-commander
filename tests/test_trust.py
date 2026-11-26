from __future__ import annotations

import json
import os
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path

from herdr_commander.herdr_client import ensure_trusted_workspace


class WorkspaceTrustTests(unittest.TestCase):
    def test_claude_trust_preserves_existing_keys_and_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            repo = Path(tmpdir) / "repo-a"
            repo.mkdir()
            claude_cfg = home / ".claude.json"
            claude_cfg.write_text(
                json.dumps(
                    {
                        "numStartups": 5,
                        "projects": {
                            str(repo): {"hasTrustDialogAccepted": False, "allowedTools": ["Bash"]},
                        },
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(claude_cfg, 0o600)

            warnings = ensure_trusted_workspace(str(repo), ("claude",), {}, home_dir=home)
            self.assertEqual(warnings, [])
            parsed = json.loads(claude_cfg.read_text(encoding="utf-8"))
            self.assertEqual(parsed["numStartups"], 5)
            self.assertTrue(parsed["projects"][str(repo)]["hasTrustDialogAccepted"])
            self.assertEqual(parsed["projects"][str(repo)]["allowedTools"], ["Bash"])
            self.assertEqual(stat.S_IMODE(claude_cfg.stat().st_mode), 0o600)

            # Idempotent re-run produces no warnings and keeps data intact.
            self.assertEqual(
                ensure_trusted_workspace(str(repo), ("claude",), {}, home_dir=home), []
            )

    def test_claude_config_dir_and_symlink_handling(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            cfg_dir = Path(tmpdir) / "custom-claude"
            cfg_dir.mkdir()
            real_repo = Path(tmpdir) / "real-repo"
            real_repo.mkdir()
            link_repo = Path(tmpdir) / "link-repo"
            link_repo.symlink_to(real_repo)

            warnings = ensure_trusted_workspace(
                str(link_repo),
                ("claude",),
                {"CLAUDE_CONFIG_DIR": str(cfg_dir)},
                home_dir=home,
            )
            self.assertEqual(warnings, [])
            cfg_path = cfg_dir / ".claude.json"
            self.assertTrue(cfg_path.exists())
            self.assertEqual(stat.S_IMODE(cfg_path.stat().st_mode), 0o600)
            parsed = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertTrue(parsed["projects"][str(link_repo)]["hasTrustDialogAccepted"])
            self.assertTrue(parsed["projects"][str(real_repo)]["hasTrustDialogAccepted"])

    def test_agy_trust_appends_without_duplicating_or_clobbering(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            agy_dir = home / ".gemini" / "antigravity-cli"
            agy_dir.mkdir(parents=True)
            settings = agy_dir / "settings.json"
            repo = Path(tmpdir) / "repo-agy"
            repo.mkdir()
            settings.write_text(
                json.dumps({"model": "gemini-4-neon-high", "trustedWorkspaces": ["/existing"]}),
                encoding="utf-8",
            )

            warnings = ensure_trusted_workspace(str(repo), ("agy",), {}, home_dir=home)
            self.assertEqual(warnings, [])
            parsed = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(parsed["model"], "gemini-4-neon-high")
            self.assertEqual(parsed["trustedWorkspaces"], ["/existing", str(repo)])

            ensure_trusted_workspace(str(repo), ("agy",), {}, home_dir=home)
            parsed_again = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(parsed_again["trustedWorkspaces"], ["/existing", str(repo)])

    def test_codex_trust_appends_and_updates_existing_untrusted_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            codex_home = Path(tmpdir) / "codex-home"
            codex_home.mkdir()
            local_toml = codex_home / "local.config.toml"
            repo_new = Path(tmpdir) / "repo-new"
            repo_existing = Path(tmpdir) / "repo-existing"
            repo_new.mkdir()
            repo_existing.mkdir()

            local_toml.write_text(
                "\n".join(
                    [
                        "# Machine-local Codex state",
                        'model = "gpt-6.2-sol"',
                        "",
                        f'[projects."{repo_existing}"]',
                        'trust_level = "untrusted"',
                        "",
                        "[tui]",
                        "screen_reader_detection_done = true",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            env = {"CODEX_HOME": str(codex_home)}
            self.assertEqual(
                ensure_trusted_workspace(str(repo_existing), ("codex",), env, home_dir=home), []
            )
            self.assertEqual(
                ensure_trusted_workspace(str(repo_new), ("codex",), env, home_dir=home), []
            )

            content = local_toml.read_text(encoding="utf-8")
            self.assertIn("# Machine-local Codex state", content)
            parsed = tomllib.loads(content)
            self.assertEqual(parsed["model"], "gpt-6.2-sol")
            self.assertTrue(parsed["tui"]["screen_reader_detection_done"])
            self.assertEqual(parsed["projects"][str(repo_existing)]["trust_level"], "trusted")
            self.assertEqual(parsed["projects"][str(repo_new)]["trust_level"], "trusted")

    def test_malformed_configs_warn_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            (home / ".gemini" / "antigravity-cli").mkdir(parents=True)
            (home / ".codex").mkdir(parents=True)
            (home / ".claude.json").write_text("{broken json", encoding="utf-8")
            (home / ".gemini" / "antigravity-cli" / "settings.json").write_text(
                "[]", encoding="utf-8"
            )
            (home / ".codex" / "local.config.toml").write_text(
                "[projects\nbroken", encoding="utf-8"
            )

            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            warnings = ensure_trusted_workspace(
                str(repo), ("claude", "agy", "codex"), {}, home_dir=home
            )
            self.assertEqual(len(warnings), 3)
            self.assertEqual((home / ".claude.json").read_text(encoding="utf-8"), "{broken json")
            self.assertEqual(
                (home / ".gemini" / "antigravity-cli" / "settings.json").read_text(
                    encoding="utf-8"
                ),
                "[]",
            )
            self.assertEqual(
                (home / ".codex" / "local.config.toml").read_text(encoding="utf-8"),
                "[projects\nbroken",
            )


if __name__ == "__main__":
    unittest.main()
