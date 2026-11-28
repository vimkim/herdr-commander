---
name: hcmd-auto
description: Opt-in autonomous event-driven Commander watch loop using hcmd wait --since-last with strict fail-closed safety allowlist, JSONL audit log, per-Space circuit breaker, and automatic same-tab pane rotation via hcmd self-context and hcmd rotate.
---

# Herdr Auto (`hcmd-auto`)

Opt-in autonomous event-driven Commander watch, triage, and self-rotation loop. Never start `hcmd-auto` silently on ordinary dispatch — activate only when the user explicitly requests autonomous monitoring/watch mode (e.g., `/hcmd-auto`, "지켜봐줘", "자동으로 돌리면서 체크해줘").

## 1. Core Loop (Event-Driven + 10m Fallback)

Run one blocking event wait per cycle (0 LLM tokens while workers run, <1s wakeup on `blocked`, `done`, `working -> idle`, or blocked sub-tabs):

```bash
hcmd wait --since-last --include-subtabs --emit-digest --compact --stop-when-idle --allow-timeout --timeout 600 --verify-busy --json
```

Pass `--cursor <name>` if a named cursor was provided (or carried in a rotation handoff).

On each wakeup:
1. **`reason == "no_active_work"`**:
   - All watched primary and sub-tab agents have settled and no unread deltas remain.
   - Report final summary via `hcmd-check` format and **auto-stop** (do not re-arm).
2. **`reason == "changed"`**:
   - Inspect `data.digest` (`entries`, `still_blocked`, `closed_workspaces`).
   - Apply the Fail-Closed Policy (Section 2) and Circuit Breaker (Section 3).
   - Log every autonomous action to `auto-audit.jsonl` (Section 4).
   - If any item requires human approval/decision, surface it immediately and pause autonomous action on that Space.
3. **`reason == "timeout"`** (10 minutes elapsed with workers still running and zero deltas):
   - Optionally check `cubrid-tcr` reverse join if open `wait-for:` items exist.
   - Check context budget (Section 5), then re-arm `hcmd wait` if `has_active_work == true` (or exit cleanly if stopped/idle).

## 2. Fail-Closed Safety Policy (Allowlist vs Hard Blocklist)

Autonomous action is permitted **only** when the action unambiguously falls inside the **Allowlist** and touches **zero** items in the **Hard Blocklist**. When in doubt, fail closed and ask the user.

### Allowlist (Autonomous Action Permitted)
- **Read-only inspection & verification**: `hcmd peek`, `hcmd status`, `hcmd find`, `cubrid-tcr blockers`, read-only `gh pr view` / `gh pr checks`, read-only `git status` / `log` / `diff` via Space workers.
- **Local build, test, & reproduction**: Dispatching follow-up local build/test/repro/audit prompts to an idle/done Space when within the current task's approved scope and circuit-breaker budget.
- **Verification audits**: Triggering `close-gate` or local test re-checks before marking work ready.
- **Verified tracker updates**: Recording concrete commit SHAs, test counts, or verified blockers via `work-tracker` (`track-work`) or `failure-tracker`.
- **Pre-agreed standing policies**: Actions explicitly pre-authorized by the user in this session or handoff (for example, flaky CI `/run rerun` up to 3 attempts when failure matches a known transient signature).

### Hard Blocklist (NEVER Autonomous — Always Escalate)
- **NO** `git push` or `git push --force` to `CUBRID/*` or any shared/upstream remote.
- **NO** `gh pr merge`, PR comments, PR reviews, or PR description edits on external repositories.
- **NO** live JIRA mutations (`cubrid-jira` transitions, comments, or description updates).
- **NO** `herdr workspace close` or `cubrid-worktree delete` / `git worktree remove`.
- **NO** answering `AGENT_BLOCKED` prompts that involve design choices, destructive commands, external side effects, or ambiguous user intent.
- **NO** overriding circuit-breaker limits or quota exhaustion (`NO_ELIGIBLE_BACKEND` -> never fall back to `codex`).

## 3. Per-Space Circuit Breaker

- Maintain a per-Space consecutive autonomous dispatch counter (`<= 3` max, configurable down to `2` if requested) without human check-in.
- Reset a Space's counter when the user explicitly interacts with or confirms that Space.
- When a Space hits the cap (e.g., 3 consecutive autonomous follow-ups without finishing cleanly or needing user input):
  - **Pause** autonomous dispatches to that Space.
  - Record a `"circuit_breaker_tripped"` entry in `auto-audit.jsonl`.
  - Escalate to the user with a 1-line summary and recommendation.

## 4. Persistent Audit Log (`auto-audit.jsonl`)

Append one JSON line per autonomous action, skip-on-blocklist, or circuit-breaker event to:
`${XDG_STATE_HOME:-~/.local/state}/herdr-commander/auto-audit.jsonl`

Required fields per JSONL line:
```json
{"ts":"2026-11-28T16:30:00+09:00","workspace_id":"w3","tracker_id":1124,"action":"dispatch_followup","reason":"test_repro","outcome":"sent","consecutive":1}
```

Never log secrets, tokens, or full terminal dumps in `auto-audit.jsonl`.

## 5. Self-Context Check & Safe Same-Tab Rotation

At the end of **every** cycle (before re-arming `hcmd wait`), inspect context window usage:

```bash
hcmd self-context --rotate-ctx 30 --json
```

(Default threshold is `30`%; honor any custom `--rotate-ctx N` configured by the user or carried in handoff.)

When `data.should_rotate == true`:
1. Summarize any pending human decisions (`--decision "..."`) and active watch cursor (`--cursor <name>`).
2. Initiate 2-phase same-tab rotation (splits `t1` right without creating a new tab, writes handoff state, launches successor `ay`, and verifies startup prompt acceptance):
   ```bash
   hcmd rotate --rotate-ctx 30 [--cursor <name>] [--auto-config "<summary>"] [--decision "..."] --json
   ```
3. Confirm `data.status == "pending_ack"` and `data.agent_status == "working"`, then **stop issuing commands and yield** — do NOT close yourself or call `--ack` from the old pane. The successor pane runs `/herdr-commander-init` (`/hcmd-init`), verifies state, runs `hcmd rotate --ack <old_pane_id> --json`, and resumes `/hcmd-auto`.
4. If `ROTATE_COOLDOWN_ACTIVE` is returned (within 300s of prior rotation), report the remaining cooldown to the user and stop autonomous looping instead of thrashing.
