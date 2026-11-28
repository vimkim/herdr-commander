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
- **Pre-agreed standing policies**: Actions explicitly pre-authorized by the user in this session or handoff (for example, flaky CI `/run rerun` up to 3 attempts when failure matches a known transient signature), and actions satisfying every guardrail in `Conditional Standing Allowlist (CUBRID PR Sync & CI)` below.

#### Conditional Standing Allowlist (CUBRID PR Sync & CI)

Pre-approved autonomous CUBRID PR sync and CI recovery is permitted **only** when **all 5 guardrails** below hold:

1. **Branch/remote scope**:
   - Allow `git push` **ONLY** to the user's open PR head branch (`vk/*`) and linked TC branches (`tc/pr-<N>`), plus posting `/run all` or `/run rerun <run-id>` and running `cubrid-ci-watch register` on that PR.
   - Keep `origin` (`CUBRID/cubrid`) `develop`/`release/*`/`feature/*` direct push, `gh pr merge`, draft/reviewer/description edits, JIRA mutations, and workspace/worktree deletion on the Hard Blocklist.
2. **`merge` vs `rebase --force-with-lease`**:
   - On PRs that already have human reviews or approvals: allow `git merge origin/develop` + normal push **only** (escalate any `rebase --force-with-lease`).
   - On unapproved/Draft PRs: allow either `git rebase origin/develop` + `git push --force-with-lease` (`git rebase origin/develop --force-with-lease`) or `git merge origin/develop` + normal push.
3. **Mandatory local pre-push/CI gates** (all must pass before pushing or posting `/run`):
   - `conflict == 0` (abort and escalate immediately on any merge/rebase conflict).
   - Local `just build` passes.
   - `cubrid-pr-tc-base-check --pr <N>` exits `0`.
   - No in-flight `/run` on the PR.
   - Prefer `/run rerun <run-id>` over `/run all` when an eligible same-head run exists.
4. **Verified unblock trigger & runaway cap**:
   - Trigger autonomous unblock recovery **only** when `cubrid-tcr blockers` reports `READY` or the blocking fix PR is verified merged into `origin/develop` via `gh`/`git`.
   - Cap autonomous `sync + /run all` at **max 2 per PR per session**, integrated with the per-Space circuit breaker (Section 3).
   - Log every action (triggers, gate results, dispatches, skips, and cap trips) in `auto-audit.jsonl` (Section 4).
5. **Runtime handoff & Space Lead prompt tag**:
   - Propagate the standing allowlist summary across pane rotations via `hcmd rotate --auto-config "<summary>"` (Section 5).
   - When dispatching an allowed sync/CI action to a Space Lead via `hcmd send`, include `[AUTO-POLICY: standing-approved]` plus the mandatory gate checklist (`conflict==0`, `just build`, `cubrid-pr-tc-base-check --pr <N>`, no in-flight `/run`, prefer `/run rerun <run-id>`, merge-only if human-reviewed/approved, push scope `vk/*` + `tc/pr-<N>`) so Space Lead executes without re-asking.

### Hard Blocklist (NEVER Autonomous — Always Escalate)
- **NO** direct `git push` or `git push --force` to `origin` (`CUBRID/cubrid`) `develop`, `release/*`, `feature/*`, or any branch/remote outside the user's open PR head branch (`vk/*`) and linked TC branches (`tc/pr-<N>`) under the Conditional Standing Allowlist (and **NEVER** `rebase --force-with-lease` on PRs with human reviews or approvals).
- **NO** `gh pr merge`, PR reviews, draft/reviewer/description edits, or general PR comments on external repositories (only `/run all` or `/run rerun <run-id>` plus `cubrid-ci-watch register` on the user's open PR when all Conditional Standing Allowlist gates pass).
- **NO** live JIRA mutations (`cubrid-jira` transitions, comments, or description updates).
- **NO** `herdr workspace close` or `cubrid-worktree delete` / `git worktree remove`.
- **NO** answering `AGENT_BLOCKED` prompts that involve design choices, destructive commands, external side effects, or ambiguous user intent.
- **NO** overriding circuit-breaker limits or quota exhaustion (`NO_ELIGIBLE_BACKEND` -> never fall back to `codex`).

## 3. Per-Space Circuit Breaker

- Maintain a per-Space consecutive autonomous dispatch counter (`<= 3` max, configurable down to `2` if requested) without human check-in, and enforce the Conditional Standing Allowlist cap of **max 2** autonomous `sync + /run all` cycles per PR per session.
- Reset a Space's counter when the user explicitly interacts with or confirms that Space.
- When a Space or PR hits its cap (e.g., 3 consecutive autonomous follow-ups without finishing cleanly or needing user input, or max 2 `sync + /run all` recoveries on one PR in a session):
  - **Pause** autonomous dispatches to that Space/PR.
  - Record a `"circuit_breaker_tripped"` entry in `auto-audit.jsonl`.
  - Escalate to the user with a 1-line summary and recommendation.

## 4. Persistent Audit Log (`auto-audit.jsonl`)

Append one JSON line per autonomous action, skip-on-blocklist, or circuit-breaker event to:
`${XDG_STATE_HOME:-~/.local/state}/herdr-commander/auto-audit.jsonl`

Required fields per JSONL line:
```json
{"ts":"2026-11-28T16:30:00+09:00","workspace_id":"w3","tracker_id":1124,"action":"dispatch_followup","reason":"test_repro","outcome":"sent","consecutive":1}
```

For Conditional Standing Allowlist actions, also include PR, policy, and cap fields when applicable (e.g., `"pr":1234,"pr_sync_count":1,"policy":"standing-approved"`). Never log secrets, tokens, or full terminal dumps in `auto-audit.jsonl`.

## 5. Self-Context Check & Safe Same-Tab Rotation

At the end of **every** cycle (before re-arming `hcmd wait`), inspect context window usage:

```bash
hcmd self-context --rotate-ctx 30 --json
```

(Default threshold is `30`%; honor any custom `--rotate-ctx N` configured by the user or carried in handoff.)

When `data.should_rotate == true`:
1. Summarize any pending human decisions (`--decision "..."`) and active watch cursor (`--cursor <name>`).
2. Initiate 2-phase same-tab rotation (splits `t1` right without creating a new tab, writes handoff state, launches successor `ay`, and verifies startup prompt acceptance); pass `--auto-config "<summary>"` carrying the active Conditional Standing Allowlist summary (and any non-zero per-PR sync counts) so the successor pane preserves the policy across rotation:
   ```bash
   hcmd rotate --rotate-ctx 30 [--cursor <name>] [--auto-config "<summary>"] [--decision "..."] --json
   ```
3. Confirm `data.status == "pending_ack"` and `data.agent_status == "working"`, then **stop issuing commands and yield** — do NOT close yourself or call `--ack` from the old pane. The successor pane runs `/herdr-commander-init` (`/hcmd-init`), verifies state, runs `hcmd rotate --ack <old_pane_id> --json`, and resumes `/hcmd-auto`.
4. If `ROTATE_COOLDOWN_ACTIVE` is returned (within 300s of prior rotation), report the remaining cooldown to the user and stop autonomous looping instead of thrashing.
