---
name: herdr-commander
description: Coordinate all tasks through Herdr workspaces and Herdr tabs by managing each workspace's first-tab agent. Use when acting as a Herdr commander to plan or dispatch work into Herdr workspaces, launch ai-agent, ay, ai-round, or ai-best in matching worktrees or directories, monitor first-tab progress, or harvest completed workspace results.
---

# Herdr Commander

Act strictly as the coordinator in your own Herdr workspace. Execute **all** work through Herdr workspaces and Herdr tabs:

- **All tasks run in Herdr workspaces + tabs**: Dispatch every piece of task work — implementation, debugging, investigation, code review, builds, tests, docs, or PR/CI work — into a Herdr workspace (Tab 1 via `hcmd ensure` and `hcmd send`) and its Herdr tabs. Never perform task work directly in the commander session or bypass Herdr with in-process subagents or swarms.
- **Commander session scope**: Keep this session strictly on coordination — reconciling schedules and trackers, checking status, ensuring workspaces, sending briefs, waiting on events, peeking output, updating `work-tracker` / daily sheets, and reporting to the user.
- **3-tier hierarchy & hard caps**:
  - **Commander** (this session): manages Spaces via `hcmd status` / `find` / `ensure` / `send` / `wait` / `peek` (`hss` works as a compatibility alias).
  - **Space Leader** (`tabs[0]`, `panes[0]`): manages its own Space and may spawn up to **8 sub-tabs** (9 tabs total) via `hcmd tab spawn --label <role>` (`ai-agent` default; default guards of 4 live non-primary AI agents per Space and 10 working agents session-wide; `--launcher shell` unrestricted within the 8-tab cap).
  - **Tab Leader** (`tabs[1..8]`, `panes[0]`): may split up to **3 worker panes** (2×2 grid) inside its own tab via `hcmd pane spawn`; never creates new tabs.
  - **Leaf Worker** (`panes[1..]`): executes assigned work; never creates tabs or splits panes.
- **Context-size management**: Simple, quick checks can be handled directly, but **Commander, Space Leaders, and Tab Leaders should prefer delegating substantial, exploratory, noisy, or multi-step work** to Herdr workspaces/tabs (`hcmd tab`), worker panes (`hcmd pane`), or in-process subagents (within Space/Tab Leaders) so each leader keeps its context window compact and focused on orchestration and synthesis.

Delegate sibling concerns to existing skills rather than duplicating their rules.

- Daily priorities and wrap-up sheets live in `daily-schedule`.
- Persistent item status and notes live in `work-tracker` via `track-work`.
- CUBRID ticket worktree naming and safety live in `cubrid-worktree`.
- Low-level pane and tab control lives in `herdr`.

## 1. Preflight and reconcile

Verify `HERDR_ENV=1` and check the CLI:

```bash
test "${HERDR_ENV:-}" = 1 && hcmd --version
```

Inspect today's commitments and live Herdr workspaces before launching anything:

```bash
work-tracker todo today --json
hcmd status --workers-only --verify-busy --with-tracker --json
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
| CUBRID ticket implementation or debug | `--ticket CBRD-NNNNN` | `ai-agent` (or `ai-best` for hard tasks) |
| CUBRID PR review checkout | `--pr NNNN` | `ai-agent` |
| Parallel medium work, tests, or docs | `--cwd /path/to/repo` | `ai-agent` |
| Independent code review | `--cwd /path/to/repo` | `ai-agent` |

Launcher meanings:

- `ai-agent` is the `ensure` default. It picks `ay` (Herdr agent `agy`) or `ai-round` (Herdr agent `claude`) by remaining quota at launch time. It never selects Codex. `ensure` runs `ai-agent --dry-run --json` first, so `--dry-run` reports the predicted agent kind and an exhausted quota fails before any worktree is created.
- `ay` forces Antigravity CLI. `ai-round` forces Claude round-robin across numbered slots. `ai-best` forces the strongest Claude setup.

Do not choose `codex` or `claude` unless the user asks for it.

Before dispatching several workspaces at once, check quota with `ai-agent --dry-run`. If it reports `NO_ELIGIBLE_BACKEND`, pause dispatch and tell the user instead of falling back to Codex.

## 3. Check existing workspaces before launching

Before calling `hcmd ensure` or opening any new workspace, **always** check whether a matching Herdr workspace is already open for the target name, label, pwd/cwd, ticket, or PR:

```bash
hcmd find --label my-project --cwd ~/gh/my-project --verify-busy --json
hcmd find --ticket CBRD-10002 --verify-busy --json
hcmd find --name demo-lead --verify-busy --json
```

Pass every known identifier (`--name`, `--label`, `--cwd` / `--pwd`, `--ticket`, `--pr`) so exact and partial matches on workspace labels, primary/sub-pane agent names, working directories, tickets, and PRs are all checked.

- **When `data.found` is `true` (`recommendation == "ask_reuse"`)**:
  - **Stop before calling `hcmd ensure` or `hcmd send`** (unless the user already explicitly asked in this turn to reuse or send to that exact workspace).
  - Present each matched workspace with its live Herdr location (workspace label, primary tab label + `primary_tab_id`, `pane_id`), agent kind and `effective_status` / `attention` (plus `sub_tabs` if active), `cwd`, and `match_reasons`.
  - **Propose delegating to the existing workspace and ask the user**, for example:
    `"이미 <label> (tab <primary_tab_label> (<primary_tab_id>), pane <pane_id>, <agent_kind>:<effective_status>, cwd <cwd>) 워크스페이스가 띄워져 있습니다. 거기에 맡길까요, 아니면 새 워크스페이스를 띄울까요?"`
  - If the user chooses to **reuse**: dispatch with `hcmd send <ws>` (if the workspace is an empty shell, run `hcmd ensure` first; if `working`/`busy_bg` or `blocked`, coordinate or surface the blocker before sending).
  - If the user chooses to **create new**: run `hcmd ensure --cwd <dir> --label <new-label> --new --launcher ai-agent --json`.
- **When `data.found` is `false` (`recommendation == "create_new"`)**: proceed directly to Step 4.

## 4. Ensure workspaces and brief Tab 1

Run `ensure` for each new or user-confirmed task. It reuses matching open workspaces (unless `--new` is passed for a `--cwd` workspace), creates CUBRID worktrees via `cubrid-worktree` or general directories via `herdr-next-to-current`, and waits until the first-tab agent is ready.

```bash
hcmd ensure --ticket CBRD-10002 --launcher ai-agent --json
hcmd ensure --cwd ~/gh/my-project --label my-project --launcher ai-agent --json
```

If `ensure` exits with `OCCUPIED_BY_OTHER_AGENT`, inspect the workspace with `hcmd peek <ws>` instead of overwriting the pane.

Send the kickoff brief to Tab 1 via stdin so multiline text needs no shell escaping:

```bash
hcmd send <ws> --prompt - --json <<'EOF'
Objective: <concise goal and acceptance criteria>.
Tracker ID: <work-tracker ID or none>.
Workspace execution: Use `hcmd tab` inside this workspace for parallel work, builds, tests, or helper agents (up to 8 sub-tabs, short role `--label` such as build, test, review, or ci, `ai-agent` default for AI workers, `--launcher shell` for commands). Keep your own context window lean by delegating substantial, noisy, or exploratory work to sub-tabs or subagents while handling trivial steps directly. Keep same-worktree edits to one agent at a time, and close finished sub-tabs you created with `hcmd tab close`.
Reporting rule: Keep final findings, verification evidence (commits, test runs, PR or doc paths), and blockers summarized in this first tab.
EOF
```

Never pass `--force` to bypass `AGENT_BLOCKED`. Inspect blocked dialogs with `hcmd peek <ws>` and ask the user before answering approval or question prompts.

Completion criterion. Every assigned task shows a ready or working agent in Tab 1, and `work-tracker` notes record the workspace ID.

## 5. Monitor events and harvest outcomes

Wait on Herdr socket events instead of sleeping in a polling loop:

```bash
hcmd wait --until blocked,done --timeout 300 --verify-busy --json
```

Handle woken workspaces in priority order.

1. Read `blocked` workspaces first with `hcmd peek <ws> --lines 60`. Forward non-trivial approvals or decisions to the user.
2. Read `done` workspaces with `hcmd peek <ws> --lines 60`.
3. Check `sub_tabs.any_working` and `sub_tabs.any_blocked` from `hcmd status --workspace <ws> --json`. If a sub-tab is still working while Tab 1 reports `done`, ask Tab 1 to confirm sub-tab completion before closing out the item.
4. Record verified outcomes with `work-tracker note` or `work-tracker status` and update `daily/today-YYYY-MM-DD.md`.

## 6. Wrap up

List workspaces whose `work-tracker` items are `done` or whose `deleted_hint` is `true` as cleanup candidates. Never close Herdr workspaces or delete worktrees without explicit user approval.
