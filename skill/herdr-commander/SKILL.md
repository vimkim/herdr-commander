---
name: herdr-commander
description: Coordinate daily tasks across Herdr workspaces by managing each workspace's first-tab agent. Use when planning or dispatching parallel work into Herdr workspaces, launching ai-round, ai-best, ay, or codex in matching worktrees or directories, monitoring first-tab progress, or harvesting completed workspace results.
---

# Herdr Commander

Act as the coordinator in your own Herdr workspace. Manage worker workspaces through their first tab while letting each first-tab agent spawn and control its own extra tabs inside that workspace.

Delegate sibling concerns to existing skills rather than duplicating their rules.

- Daily priorities and wrap-up sheets live in `daily-schedule`.
- Persistent item status and notes live in `work-tracker` via `track-work`.
- CUBRID ticket worktree naming and safety live in `cubrid-worktree`.
- Low-level pane and tab control lives in `herdr`.

## 1. Preflight and reconcile

Verify `HERDR_ENV=1` and check the CLI:

```bash
test "${HERDR_ENV:-}" = 1 && hss --version
```

Inspect today's commitments and live Herdr workspaces before launching anything:

```bash
work-tracker todo today --json
hss status --workers-only --verify-busy --with-tracker --json
```

Read `daily/today-YYYY-MM-DD.md` and the active weekly sheet per `daily-schedule`.

Completion criterion. Produce three explicit lists before dispatching work.

1. Active workspaces already mapped to open items.
2. Open tasks that need a workspace launched or resumed.
3. Workspaces needing attention (`blocked`, `done`, or `sub_tabs.any_blocked`).

## 2. Choose directory and launcher

Map each task to its target directory and launcher before calling `ensure`.

| Task type | Target flag | Default launcher |
| --- | --- | --- |
| CUBRID ticket implementation or debug | `--ticket CBRD-NNNNN` | `ay` or `ai-best` |
| CUBRID PR review checkout | `--pr NNNN` | `codex` or `ay` |
| Parallel medium work, tests, or docs | `--cwd /path/to/repo` | `ai-round` |
| Independent code review | `--cwd /path/to/repo` | `codex` |

Before starting multiple Claude sessions (`ai-round` or `ai-best`), check remaining account quota with `ai-status`. Switch to `ay` or `codex` when Claude slots are low.

## 3. Ensure workspaces and brief Tab 1

Run `ensure` for each assigned task. It reuses matching open workspaces, creates CUBRID worktrees via `cubrid-worktree` or general directories via `herdr-next-to-current`, and waits until the first-tab agent is ready.

```bash
hss ensure --ticket CBRD-10002 --launcher ay --json
hss ensure --cwd ~/gh/my-project --label my-project --launcher ai-round --json
```

If `ensure` exits with `OCCUPIED_BY_OTHER_AGENT`, inspect the workspace with `hss peek <ws>` instead of overwriting the pane.

Send the kickoff brief to Tab 1 via stdin so multiline text needs no shell escaping:

```bash
hss send <ws> --prompt - --json <<'EOF'
Objective: <concise goal and acceptance criteria>.
Tracker ID: <work-tracker ID or none>.
Workspace autonomy: Create and manage extra Herdr tabs in this workspace as needed for builds, tests, or helper agents.
Reporting rule: Keep final findings, verification evidence (commits, test runs, PR or doc paths), and blockers summarized in this first tab.
EOF
```

Never pass `--force` to bypass `AGENT_BLOCKED`. Inspect blocked dialogs with `hss peek <ws>` and ask the user before answering approval or question prompts.

Completion criterion. Every assigned task shows a ready or working agent in Tab 1, and `work-tracker` notes record the workspace ID.

## 4. Monitor events and harvest outcomes

Wait on Herdr socket events instead of sleeping in a polling loop:

```bash
hss wait --until blocked,done --timeout 300 --verify-busy --json
```

Handle woken workspaces in priority order.

1. Read `blocked` workspaces first with `hss peek <ws> --lines 60`. Forward non-trivial approvals or decisions to the user.
2. Read `done` workspaces with `hss peek <ws> --lines 60`.
3. Check `sub_tabs.any_working` and `sub_tabs.any_blocked` from `hss status --workspace <ws> --json`. If a sub-tab is still working while Tab 1 reports `done`, ask Tab 1 to confirm sub-tab completion before closing out the item.
4. Record verified outcomes with `work-tracker note` or `work-tracker status` and update `daily/today-YYYY-MM-DD.md`.

## 5. Wrap up

List workspaces whose `work-tracker` items are `done` or whose `deleted_hint` is `true` as cleanup candidates. Never close Herdr workspaces or delete worktrees without explicit user approval.
