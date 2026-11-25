# herdr-commander (`hcmd`)

First-tab-centric status inspection, idempotent orchestration, and commander skill for Herdr workspaces. (Legacy `hss` alias is kept for compatibility.)

## Install

Run from a clean `main` checkout:

```bash
just sync
```

This installs `herdr-commander`, `hcmd`, and compatibility alias `hss` via `uv tool install --reinstall .` and symlinks `skill/herdr-commander` into `~/.agents/skills/herdr-commander`, `~/.claude/skills/herdr-commander` (Claude), `${CODEX_HOME:-~/.codex}/skills/herdr-commander` (Codex), and `~/.gemini/config/skills/herdr-commander` (Antigravity / `agy`).

## Commands

### `status`

Inspect every workspace through its first tab (`tabs[0]` per workspace, regardless of tab ID or number) plus a rollup of all sub-tabs and split panes.

```bash
hcmd status
hcmd status --workers-only --verify-busy --with-tracker --json
hcmd status --plain --fields number,workspace_id,attention,agent_kind,label
```

- `--verify-busy` checks `agent.read(..., source="detection")` on `idle`/`done` Claude panes and marks background-active panes as `busy_bg` (counted under `working`).
- `--with-tracker` correlates workspaces with open `work-tracker` items by `cwd` and `CBRD-NNNNN` ticket token.

### `find`

Check before launching whether an existing Herdr workspace already matches a target name, label, working directory (`--cwd`/`--pwd`), ticket, or PR:

```bash
hcmd find --label my-repo --cwd ~/gh/my-repo --verify-busy --json
hcmd find --ticket CBRD-10002 --verify-busy --json
hcmd find demo-lead --json
```

- Supports positional `query` and explicit `--name`, `--label`, `--cwd` / `--pwd`, `--ticket`, and `--pr` filters, plus `--verify-busy`, `--with-tracker`, `--include-self`, and `--include-deleted`.
- Exits `0` both when matches are found (`found: true`) and when none match (`found: false`), reporting a state-aware `recommendation`:
  - `create_new`: no matching workspace exists.
  - `reuse_empty_shell`: single exact primary match running an empty shell (`agent_kind: null`).
  - `ask_reuse`: idle/done agent match or multiple candidate workspaces.
  - `warn_busy`: at least one matched workspace (or its sub-tabs) is `working`, `busy_bg`, or `blocked`.
- Matches workspace labels, primary and sub-pane agent names, primary/foreground/sub-pane working directories (including symlinks and subdirectories under the target directory), tickets, and PR tokens while excluding `is_self` and `deleted_hint` workspaces by default.

### `peek`

Read recent terminal output from a workspace's first-tab pane:

```bash
hcmd peek w1 --lines 60
hcmd peek CBRD-10001 --source visible --json
```

### `wait`

Subscribe to Herdr socket events until any watched first-tab reaches target states:

```bash
hcmd wait --until blocked,done --timeout 300 --verify-busy --json
```

### `ensure`

Idempotently converge a workspace and its first-tab agent:

```bash
hcmd ensure --ticket CBRD-10002 --on-existing fail --launcher ai-agent --json
hcmd ensure --reuse w2 --launcher ai-agent --json
hcmd ensure --cwd ~/gh/my-repo --pwd ~/gh/my-repo --label my-repo --on-existing new --launcher ai-agent --json
```

| Launcher | Herdr agent kind | Notes |
| --- | --- | --- |
| `ai-agent` (default) | `agy` or `claude` | Picks `ay` or `ai-round` by remaining quota and never selects Codex. |
| `ay` | `agy` | Antigravity CLI. |
| `ai-round` | `claude` | Claude round-robin across numbered slots. |
| `ai-best` | `claude` | Strongest Claude setup. |
| `codex`, `claude` | `codex`, `claude` | Direct `herdr agent start` in bypass mode. Use only on explicit request. |
| `shell` | none | Workspace only, no agent. |

- `ai-agent` runs `ai-agent --dry-run --json` before creating a workspace. `--dry-run` reports the predicted agent kind, and a quota failure such as `AI_AGENT_NO_ELIGIBLE_BACKEND` stops before any worktree is created. Either `agy` or `claude` in the first tab counts as a match for reuse and readiness.
- Uses the same `find_matching_spaces` matcher as `hcmd find`, controlled by `--on-existing {reuse,fail,new}`:
  - `reuse` (default): reuses an exact primary match and matching first-tab agent.
  - `fail`: aborts before creating or reusing (`EXISTING_WORKSPACE_FOUND`) and returns `recommendation` + `matches` (with Claude `busy_bg` verification) in `error.details`.
  - `new` (or shorthand `--new`): creates a fresh `--cwd`/`--pwd` workspace even when a matching workspace exists.
- `--reuse <target>` explicitly targets an existing workspace ID, label, or ticket to ensure/launch its first-tab agent.
- Delegates `--ticket` and `--pr` workspace creation to `cubrid-worktree` and `--cwd` creation to `herdr-next-to-current` or `herdr workspace create --no-focus`.
- Refuses to overwrite a first-tab pane already occupied by a different agent kind (`OCCUPIED_BY_OTHER_AGENT`).

### `send`

Validate first-tab readiness and send a prompt (supports `--prompt -` from stdin):

```bash
hcmd send w1 --prompt - --json <<'EOF'
Summarize your findings in Tab 1.
EOF
```

Refuses `blocked` agents (`AGENT_BLOCKED`), `working`/`busy_bg` agents without `--force` (`AGENT_BUSY`), and the caller's own workspace without `--force` (`CANNOT_SEND_TO_SELF`).

### `tab` (Space Leader)

Manage worker sub-tabs inside the caller's own Space (up to 8 sub-tabs by default, total 9 tabs matching `Alt+Shift+1..9`). Enforces Space Leader identity (`tabs[0]`, `panes[0]`, not `HERDR_TAB_ROLE=worker` or `HERDR_PANE_ROLE=worker`) and tags created tabs with `HERDR_TAB_ROLE=worker` to block recursive tab spawning.

```bash
hcmd tab list --verify-busy
hcmd tab spawn --label review --launcher ai-agent --prompt "Review current diff." --json
hcmd tab wait --until blocked,done --verify-busy --json
hcmd tab peek review --lines 60
hcmd tab send review --prompt "Check edge cases." --json
hcmd tab close review
```

### `pane` (Tab Leader)

Manage worker panes inside the caller's own tab (up to 3 worker panes by default, forming a 2×2 grid). Allowed for Space/Tab Leaders (`panes[0]`, not `HERDR_PANE_ROLE=worker`); tags split panes with `HERDR_PANE_ROLE=worker`.

```bash
hcmd pane list --verify-busy
hcmd pane spawn --launcher ai-agent --prompt "Audit tests." --json
hcmd pane spawn --launcher shell --cmd "just test" --json
hcmd pane wait --until blocked,done --verify-busy --json
hcmd pane peek p2 --lines 60
hcmd pane send p2 --prompt "Summarize failures." --json
hcmd pane close p2
```

Default limits (override via CLI flags or environment variables; legacy `HSS_MAX_*` fallbacks are also accepted; `0` = unlimited):

- `HCMD_MAX_SUB_TABS=8`: max non-primary sub-tabs per Space (`hcmd tab spawn --max-sub-tabs`).
- `HCMD_MAX_WORKER_PANES=3`: max non-primary worker panes per tab (`hcmd pane spawn --max-worker-panes`).
- `HCMD_MAX_SPACE_AGENTS=4`: max live non-primary AI agents per Space (`--max-space-agents`; `--launcher shell` bypasses).
- `HCMD_MAX_SESSION_AGENTS=10`: max currently working AI agents across the Herdr session (`--max-session-agents`; `--launcher shell` bypasses).

## JSON Contract and Exit Codes

Under `--json`, stdout emits only:

```json
{ "ok": true, "schema_version": 1, "data": { ... } }
```

Failures leave stdout empty and write one structured JSON line to stderr:

```json
{ "ok": false, "error": { "code": "...", "message": "...", "fix": "...", "transient": false } }
```

| Exit code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Domain failure (missing target, occupied pane, blocked/busy agent, timeout) |
| `2` | Invalid CLI usage or flags |
| `75` | Temporary retry-safe Herdr socket error |
| `78` | Environment or configuration error (`HERDR_ENV != 1`, missing dependency) |
| `130` | Interrupted by SIGINT |

## Development

```bash
just check
just --choose
```

Parameterless `hcmd` CLI lifecycle and query presets live in `hcmd.just` (`just --list hcmd` / `just hcmd <recipe>`).
