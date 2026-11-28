---
name: herdr-commander
description: Coordinate all tasks asynchronously through Herdr workspaces and Herdr tabs using hcmd only. Compatibility wrapper delegating bootstrap/dispatch to herdr-commander-init (hcmd-init), delta check-ins to hcmd-check, and autonomous event-driven monitoring to hcmd-auto.
---

# Herdr Commander

Backward-compatible umbrella skill for Commander coordination via `hcmd` (`hss` alias preserved). Keep all core Commander invariants intact and delegate each cadence to its specialized skill:

## Core Invariants (Always Enforced)

- **Async-first execution**: Dispatch all task work into Herdr Spaces (`hcmd ensure` + `hcmd send`) and return control immediately. Blocking waits (`hcmd wait` / `hcmd send --wait`) are strictly opt-in or managed by `hcmd-auto`.
- **`hcmd`-only inspection**: Never call raw `herdr pane read` / `herdr agent read` / `herdr api snapshot`, never inspect worker worktrees directly (`git -C`, `cat`, `rg`), and never bypass Herdr with in-process subagents or swarms.
- **4-tier hierarchy & hard caps**: Commander > Space Lead (`tabs[0]`, `panes[0]`, max 8 sub-tabs via `hcmd tab`) > Tab Lead (`tabs[1..8]`, `panes[0]`, max 3 worker panes via `hcmd pane`) > Pane Worker (`panes[1..]`, execute only).
- **Safe lifecycle**: Pre-launch check (`--on-existing fail` / `hcmd find`) before creating Spaces; never `--force` past `AGENT_BLOCKED`; never close Herdr workspaces or delete worktrees without explicit user approval.

## Delegated Workflows

- **Session bootstrap, dispatch, pre-launch dedup, & rotation handoff resume**: Read and follow `herdr-commander-init` (identical alias: `hcmd-init`).
- **On-demand delta check-in, harvesting, & `cubrid-tcr` wakeup join**: Read and follow `hcmd-check` (`hcmd digest --since-last --compact --json`).
- **Opt-in autonomous event-driven watch & context rotation**: Read and follow `hcmd-auto` (`hcmd wait --since-last ...`, `hcmd self-context`, `hcmd rotate`).

## Sibling Skills

- Daily priorities and control sheets: `daily-schedule`
- Persistent item status and notes: `track-work` (`work-tracker`)
- CUBRID ticket worktrees: `cubrid-worktree`
- Low-level pane and tab primitives: `herdr`
