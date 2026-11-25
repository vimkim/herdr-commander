# herdr-spaces-status (`hss`)

First-tab-centric status inspection, idempotent orchestration, and commander skill for Herdr workspaces.

## Install

Run from a clean `main` checkout:

```bash
just sync
```

This installs `herdr-spaces-status` and `hss` via `uv tool install --reinstall .` and symlinks `skill/herdr-commander` into `~/.agents/skills/herdr-commander`, `~/.claude/skills/herdr-commander` (Claude), `${CODEX_HOME:-~/.codex}/skills/herdr-commander` (Codex), and `~/.gemini/config/skills/herdr-commander` (Antigravity / `agy`).

## Commands

### `status`

Inspect every workspace through its first tab (`tabs[0]` per workspace, regardless of tab ID or number) plus a rollup of all sub-tabs and split panes.

```bash
hss status
hss status --workers-only --verify-busy --with-tracker --json
hss status --plain --fields number,workspace_id,attention,agent_kind,label
```

- `--verify-busy` checks `agent.read(..., source="detection")` on `idle`/`done` Claude panes and marks background-active panes as `busy_bg` (counted under `working`).
- `--with-tracker` correlates workspaces with open `work-tracker` items by `cwd` and `CBRD-NNNNN` ticket token.

### `peek`

Read recent terminal output from a workspace's first-tab pane:

```bash
hss peek w1 --lines 60
hss peek CBRD-10001 --source visible --json
```

### `wait`

Subscribe to Herdr socket events until any watched first-tab reaches target states:

```bash
hss wait --until blocked,done --timeout 300 --verify-busy --json
```

### `ensure`

Idempotently converge a workspace and its first-tab agent (`ay`, `ai-round`, `ai-best`, `codex`, `claude`, `shell`):

```bash
hss ensure --ticket CBRD-10002 --launcher ay --dry-run --json
hss ensure --cwd ~/gh/my-repo --label my-repo --launcher ai-round --json
```

- Reuses matching open workspaces and matching running first-tab agents.
- Delegates `--ticket` and `--pr` workspace creation to `cubrid-worktree` and `--cwd` creation to `herdr-next-to-current` or `herdr workspace create --no-focus`.
- Refuses to overwrite a first-tab pane already occupied by a different agent kind (`OCCUPIED_BY_OTHER_AGENT`).

### `send`

Validate first-tab readiness and send a prompt (supports `--prompt -` from stdin):

```bash
hss send w1 --prompt - --json <<'EOF'
Summarize your findings in Tab 1.
EOF
```

Refuses `blocked` agents (`AGENT_BLOCKED`), `working`/`busy_bg` agents without `--force` (`AGENT_BUSY`), and the caller's own workspace without `--force` (`CANNOT_SEND_TO_SELF`).

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
```
