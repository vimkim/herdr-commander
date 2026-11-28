# Design: `hcmd-check`, `hcmd-auto`, Delta Digest/Wait, and 2-Phase Same-Tab Rotation

## 1. Motivation

1. **Claude Code `idle` blind spot**: Claude sessions routinely settle directly to `idle` (or transition `busy_bg -> idle`), which `hcmd digest` previously omitted even under `--all`.
2. **Context flood on repeated checks**: Re-peeking unchanged `blocked`/`done` Spaces appended redundant terminal output into Commander's context window.
3. **10-minute LLM cron waste vs event-driven wait**: Polling an LLM every 10 minutes exceeds the ~5-minute prompt-cache TTL and adds up to 10-minute lag on `blocked` prompts.
4. **Long-running Commander context rotation & `ay` banner race**: Autonomous Commander sessions need deterministic context-percentage detection (`self-context`), safe same-tab handover without breaking `tabs[0]` (`rotate` + `--ack`), and reliable `ay` startup overlay dismissal so kickoff prompts are never swallowed.

## 2. CLI Additions (`src/herdr_commander/`)

### 2.1 Delta `hcmd digest`
- Flags: `--since-last`, `--cursor NAME`, `--no-advance`, `--reset-cursor`, `--include-idle`, `--compact`, `--max-peek-chars N`.
- Cursor storage: `${XDG_STATE_HOME:-~/.local/state}/herdr-commander/cursors/<scope>.json` via locked atomic writes (`fcntl.flock` + `os.replace`). Default scope hashes `(socket_path, self_workspace_id)`.
- Transition detection:
  - Surfaces `newly_blocked`, `settled_done`, `settled_idle` (`working`/`busy_bg`/`unknown`/`blocked` -> `idle` for live agents), `subtab_blocked`, `pane_changed`, `new_workspace`, and `closed_workspaces`.
  - When only `revision` bumps in an unchanged settled state, `digest` locally reads and hashes normalized text (`normalize_peek_text` strips ANSI, volatile statusline clock/rate-limit footers, and trailing prompt chrome). Identical hashes are suppressed with zero LLM bloat.
  - Unchanged `blocked` Spaces are omitted from `entries` (no re-peek) and summarized in `still_blocked`.
- `--compact`: defaults `--peek-lines` to `20`, prefers the Space Lead `Status / Summary / Evidence / Blockers / Next` tail block, bounds peeks to `--max-peek-chars` (default `1500`), and emits lean workspace metadata.

### 2.2 Transition-Aware `hcmd wait`
- Flags: `--since-last`, `--cursor NAME`, `--no-advance`, `--changes-only`, `--include-subtabs`, `--stop-when-idle`, `--allow-timeout`, `--emit-digest`, `--compact`, `--peek-lines`, `--max-peek-chars`, `--source`, `--with-tracker`.
- `--changes-only` / `--since-last` establishes a baseline without firing on already-settled Spaces.
- `--include-subtabs` subscribes to all panes in watched Spaces and wakes on `sub_tabs.any_blocked` and sub-tab `busy_bg` verification under `--verify-busy`.
- Ignores `pane.updated` events from `self_workspace_id` and debounces rapid `pane.updated` bursts (250ms).
- `--stop-when-idle` exits immediately (`reason: "no_active_work"`, exit `0`) when no primary or sub-tab agent is `working`/`busy_bg`.
- `--allow-timeout` returns `reason: "timeout", timed_out: true` (exit `0`) for clean loop integration.

### 2.3 `hcmd self-context`
- Reads caller pane `visible` (falling back to `recent`) and parses `ay` and `claude` statusline bars (`ctx ... XX% used/limit`, `5h`/`7d` remaining limits, fallback `ctx: XX%` or token ratio).
- `--rotate-ctx N` computes `should_rotate: bool`.

### 2.4 `ay` Startup Overlay Prompt-Drop Fix
- `_dismiss_startup_overlay_if_present` detects overlays such as `Introducing Gemini 4 Neon / esc to dismiss` on `agy` panes and sends `esc` via `herdr agent send-keys`.
- `_send_kickoff_prompt_verified` checks transition out of `idle` (or revision advance without overlay) after prompt submission, dismissing and retrying once if swallowed, or raising `PROMPT_NOT_ACCEPTED`.

### 2.5 Safe 2-Phase Same-Tab Rotation (`hcmd rotate`)
- Splits the caller's tab (`t1`, `herdr pane split`) without `HERDR_PANE_ROLE=worker`, preserving `tabs[0]` and all existing sub-tabs.
- Enforces cooldown (default `300s`, `--force` to bypass) to prevent crash loops.
- Writes `${XDG_STATE_HOME:-~/.local/state}/herdr-commander/handoff/<ws>.{json,md}` with `generation`, `cursor_name`, `rotate_ctx`, `auto_config`, `pending_decisions`, `note`, and `auto-audit.jsonl` tail.
- Launches successor (`ay` default), sends bootstrap prompt, and verifies `working` status while leaving the retiring pane alive (`status: "pending_ack"`).
- Successor pane completes `/herdr-commander-init` (`/hcmd-init`), then executes `hcmd rotate --ack <old_pane_id>` — verified against `handoff/<ws>.json` (`CANNOT_ACK_SELF` and `HANDOFF_MISMATCH` guards) before closing the old pane.

## 3. Skill Layout (`skill/*`)

| Skill | Responsibility |
| --- | --- |
| `herdr-commander-init` / `hcmd-init` | Canonical session bootstrap, dedup, kickoff briefs, and `--handoff` rotation takeover |
| `hcmd-check` | Lean delta harvest (`digest --since-last --compact`), `cubrid-tcr` reverse join, Korean decision-first brief |
| `hcmd-auto` | Event-driven watch loop (`wait --since-last ...`), fail-closed allowlist/blocklist, `auto-audit.jsonl`, 3x circuit breaker, `--rotate-ctx 30` self-rotation |
| `herdr-commander` | Backward-compatible wrapper delegating to the specialized skills above |
