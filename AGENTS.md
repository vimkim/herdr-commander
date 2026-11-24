# AGENTS.md

Repository guidance for `herdr-spaces-status` (`hss`).

## Layout

- `src/herdr_spaces_status/models.py`: Domain dataclasses, attention order, exit codes, and `CliError`.
- `src/herdr_spaces_status/herdr_client.py`: Unix socket RPC/event client and subprocess adapters for `herdr`, `cubrid-worktree`, `herdr-next-to-current`, and `work-tracker`.
- `src/herdr_spaces_status/snapshot.py`: Pure snapshot parsing, first-tab resolution, sub-tab rollup, Claude busy classification, tracker correlation, and filtering.
- `src/herdr_spaces_status/orchestrate.py`: Pure planning and execution for `ensure`, `send`, and `wait`.
- `src/herdr_spaces_status/formatters.py`: Output mode detection and `human`, `plain`, and `json` renderers.
- `src/herdr_spaces_status/cli.py`: CLI entry point; only module permitted to write to stdout/stderr or exit.
- `skill/herdr-commander/SKILL.md`: Commander orchestration skill linked by `just install-skill`.

## Rules

- Keep runtime dependencies empty (Python >=3.11 standard library only).
- Keep business logic pure in `snapshot.py`, `orchestrate.py`, and `formatters.py`; isolate socket and subprocess I/O in `herdr_client.py` and stream I/O in `cli.py`.
- Never assume the first tab of a workspace is `t1` or `number == 1`; always select `tabs[0]` per workspace.
- Verify changes with `just check` and install from `main` with `just sync`.
