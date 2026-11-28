---
name: hcmd-init
description: Bootstrap and coordinate Herdr Commander sessions asynchronously through Herdr workspaces and tabs using hcmd only. Handles preflight reconciliation, pre-launch dedup, Space Lead kickoff briefs, and 2-phase rotation handoff resume (--handoff).
---

# Herdr Commander Init

Act strictly as the async-first coordinator in your own Herdr workspace. Execute **all** task work through Herdr workspaces and Herdr tabs, and inspect/harvest **strictly via `hcmd`**:

- **All tasks run asynchronously in Herdr Spaces**: Dispatch every piece of task work — implementation, debugging, investigation, code review, builds, tests, docs, or PR/CI work — into a Herdr Space (Tab 1 via `hcmd ensure` and `hcmd send`). Once dispatched, record the workspace ID in `work-tracker`, report the live Herdr location, and **return control immediately**.
  - Do **not** run `hcmd wait` or `hcmd send --wait` by default after dispatching.
  - Do **not** ask *"should I wait here?"* — background async execution is the default.
  - Blocking waits (`hcmd wait` / `hcmd send --wait`) are strictly **opt-in** (or run inside `/hcmd-auto` event-driven watch loops).
- **`hcmd`-only inspection & reporting**: Inspect Space status, blockers, and results **strictly via `hcmd`** (`hcmd digest`, `hcmd status`, `hcmd peek [--tab/--pane]`, `hcmd find`, `hcmd ensure`, `hcmd send`, `hcmd self-context`, `hcmd rotate`).
  - **Forbidden bypasses**: Never call raw `herdr pane read` / `herdr agent read` / `herdr api snapshot`, never probe worker worktrees directly (`git -C`, `cat`, `rg`), and never execute task work inside the Commander session or bypass Herdr with in-process subagents or swarms.
  - **Permitted coordination tools**: `work-tracker`, `daily-schedule` sheets, `cubrid-tcr` (read-only blocker/unblock lookups), and `ai-agent --dry-run` (quota preflight).
- **4-tier hierarchy & hard caps**:
  - **Herdr Commander** (this session): dispatches and monitors Spaces asynchronously via `hcmd` (`hss` works as a compatibility alias).
  - **Space Lead** (`tabs[0]`, `panes[0]`): manages its own Space and may spawn up to **8 sub-tabs** (9 tabs total) via `hcmd tab spawn --label <role>` (`ai-agent` default; default guards of 4 live non-primary AI agents per Space and 10 working agents session-wide; `--launcher shell` unrestricted within the 8-tab cap).
  - **Tab Lead** (`tabs[1..8]`, `panes[0]`): may split up to **3 worker panes** (2×2 grid) inside its own tab via `hcmd pane spawn`; never creates new tabs.
  - **Pane Worker** (`panes[1..]`): executes assigned work; never creates tabs or splits panes.
- **Automatic workspace pre-trust**: `hcmd ensure`, `hcmd tab spawn`, and `hcmd pane spawn` automatically pre-trust the target directory in Claude, Codex, and Antigravity (`agy`) local configs before launching an AI agent (`--no-trust` to opt out).
- **Context-size management**: Keep this Commander session strictly on coordination — reconciling schedules and trackers, checking status, ensuring workspaces, sending briefs, harvesting reports via `hcmd-check` / `hcmd digest`, updating `work-tracker` / daily sheets, and rotating cleanly via `hcmd rotate` before context bloats.

Delegate sibling concerns to existing skills rather than duplicating their rules:

- On-demand delta check-ins, harvest, and `cubrid-tcr` wakeup checks: `hcmd-check`
- Event-driven autonomous watch & self-rotation loop: `hcmd-auto`
- Daily priorities and wrap-up sheets: `daily-schedule`
- Persistent item status and notes: `track-work` (`work-tracker`)
- CUBRID ticket worktree naming and safety: `cubrid-worktree`
- Low-level pane and tab control: `herdr`

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

Completion criterion. Produce three explicit lists before dispatching work:

1. Active workspaces already mapped to open items.
2. Open tasks that need a workspace launched or resumed.
3. Workspaces needing attention (`blocked`, `done`, `idle` completed turns, or `sub_tabs.any_blocked`).

## 2. Choose directory and launcher

Map each task to its target directory and launcher before calling `ensure`:

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

Before opening or dispatching any workspace, **always** enforce the pre-launch check:

- **Single-task dispatch (fail-closed single step)**: call `ensure` with `--on-existing fail`. If no matching workspace exists, it creates and starts the workspace immediately. If a match exists, it stops safely with `EXISTING_WORKSPACE_FOUND` and returns `error.details.recommendation` + `error.details.matches`:

```bash
hcmd ensure --ticket CBRD-10002 --on-existing fail --launcher ai-agent --json
hcmd ensure --cwd ~/gh/my-project --label my-project --on-existing fail --launcher ai-agent --json
```

- **Multi-task / batch dispatch**: run `hcmd find ... --verify-busy --json` across all planned tasks first so every matching workspace is collected before creating anything, then present **all** matched items to the user in **one grouped summary table** and ask once:

```bash
hcmd find --label my-project --cwd ~/gh/my-project --verify-busy --json
hcmd find --ticket CBRD-10002 --verify-busy --json
hcmd find --name demo-lead --verify-busy --json
```

Handle each `recommendation` (`data.recommendation` from `find`, or `error.details.recommendation` from `EXISTING_WORKSPACE_FOUND`, unless the user already explicitly named that exact workspace to reuse in this turn):

- `create_new`: no matching workspace exists; proceed directly to Step 4 (`ensure` already ran if using `--on-existing fail`).
- `warn_busy`: at least one matched workspace (or its sub-tabs) is `working`, `busy_bg`, or `blocked`. Report its Herdr location (`label`, `tab <primary_tab_label> (<primary_tab_id>)`, `pane <pane_id>`, `agent:status`, `cwd`, `match_reasons`), warn that work is currently active or blocked, and ask the user whether to wait, inspect with `hcmd peek <ws>`, attach a sub-tab, or create a new workspace (`--on-existing new`).
- `ask_reuse`: an existing agent (`idle` / `done`) or multiple candidate workspaces match. Present each match's live Herdr location, agent kind/status, `cwd`, and `match_reasons`, and propose reusing it vs creating new—for example:
  `"이미 <label> (tab <primary_tab_label> (<primary_tab_id>), pane <pane_id>, <agent_kind>:<effective_status>, cwd <cwd>) 워크스페이스가 띄워져 있습니다. 거기에 맡길까요, 아니면 새 워크스페이스를 띄울까요?"`
- `reuse_empty_shell`: an exact matching workspace exists with only an empty shell (`agent_kind: null`). Report its Herdr location and ask whether to launch an agent inside it (`hcmd ensure --reuse <ws> --launcher ai-agent --json`) or create a new workspace (`--on-existing new`).

Follow-up after user choice:

- **Reuse**: send directly with `hcmd send <ws>` when an agent is ready, or run `hcmd ensure --reuse <ws> --launcher ai-agent --json` to launch an agent in an empty shell / confirm readiness.
- **Create new**: run `hcmd ensure --cwd <dir> --label <new-label> --on-existing new --launcher ai-agent --json`.

## 4. Ensure workspaces and brief Tab 1

Run `ensure` with `--on-existing fail` for new tasks, or `--reuse <ws>` / `--on-existing new` after user confirmation. `ensure` delegates CUBRID worktrees to `cubrid-worktree` and general directories to `herdr-next-to-current`, then waits until the first-tab agent is ready (and automatically dismisses any `ay` startup overlay before returning).

```bash
hcmd ensure --ticket CBRD-10002 --on-existing fail --launcher ai-agent --json
hcmd ensure --reuse w2 --launcher ai-agent --json
hcmd ensure --cwd ~/gh/my-project --label my-project-2 --on-existing new --launcher ai-agent --json
```

If `ensure` exits with `OCCUPIED_BY_OTHER_AGENT`, inspect the workspace with `hcmd peek <ws>` instead of overwriting the pane.

Send the kickoff brief to Tab 1 via stdin so multiline text needs no shell escaping (never pass `--wait` unless the user explicitly requested blocking):

```bash
hcmd send <ws> --prompt - --json <<'EOF'
Objective: <concise goal and acceptance criteria>.
Tracker ID: <work-tracker ID or none>.
Workspace execution: Use `hcmd tab` inside this workspace for parallel work, builds, tests, or helper agents (up to 8 sub-tabs, short role `--label` such as build, test, review, or ci, `ai-agent` default for AI workers, `--launcher shell` for commands). Keep your own context window lean by delegating substantial, noisy, or exploratory work to sub-tabs or subagents while handling trivial steps directly. Keep same-worktree edits to one agent at a time, and close finished sub-tabs you created with `hcmd tab close`.
Reporting rule: Keep final findings, verification evidence, and blockers in this first tab. End your final or blocked Tab 1 response with a compact block so Commander can harvest it via `hcmd digest` / `hcmd peek`:
- Status: <done | blocked>
- Summary: <1-3 concise bullets>
- Evidence: <commits, test runs, PR/doc paths>
- Blockers / Next: <approval/decision needed or none>
EOF
```

Never pass `--force` to bypass `AGENT_BLOCKED`. Inspect blocked dialogs with `hcmd peek <ws>` and ask the user before answering approval or question prompts.

Completion criterion. Every assigned task shows a ready or working agent in Tab 1, `work-tracker` notes record the workspace ID, and **Commander returns control immediately without blocking on `hcmd wait`**.

## 5. Handoff resume (`--handoff` / context rotation takeover)

When launched as a successor pane from `hcmd rotate` (or invoked with `--handoff <path>`):

1. Read the handoff file (`${XDG_STATE_HOME:-~/.local/state}/herdr-commander/handoff/<ws>.md` and `.json`) to recover:
   - Retiring `old_pane_id`
   - Digest `cursor_name`
   - `rotate_ctx` threshold and `auto_config`
   - `pending_decisions` and `recent_audit` tail
2. Inspect live Spaces **without** consuming unread deltas:
   ```bash
   hcmd digest --since-last --no-advance --compact --json  # add --cursor <name> if set
   ```
3. Reconcile `work-tracker todo today --json` if needed, and verify `HERDR_ENV=1`.
4. **Only after init completes cleanly**, acknowledge takeover and retire the old pane in the same tab:
   ```bash
   hcmd rotate --ack <old_pane_id> --json
   ```
   Never run `--ack` before reading the handoff and verifying state, and never close `tabs[0]` or a pane other than `old_pane_id`.
5. Surface any `pending_decisions` first, then resume `/hcmd-auto` with the handoff's `--rotate-ctx` and `--cursor` settings.

## 6. Ongoing checks and wrap up

- For on-demand status checks and delta harvesting, follow `hcmd-check`.
- For opt-in event-driven autonomous monitoring and context rotation, follow `hcmd-auto`.
- List workspaces whose `work-tracker` items are `done` or whose `deleted_hint` is `true` as cleanup candidates. Never close Herdr workspaces or delete worktrees without explicit user approval.
