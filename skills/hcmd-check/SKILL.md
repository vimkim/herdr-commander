---
name: hcmd-check
description: Harvest live Herdr workspace deltas via hcmd digest --since-last --compact and surface only user-required decisions in a concise Korean brief. Use for Space check-ins, progress updates, or wakeup reconciliation without duplicating daily-schedule or work-tracker.
---

# Herdr Check (`hcmd-check`)

Fast, lean Commander check-in focused exclusively on **what changed in live Herdr Spaces** and **what decisions the user must make**.

## Boundaries (No Duplication)

- **`hcmd-check` owns**: Live Herdr Space delta inspection (`hcmd digest --since-last`), targeted `hcmd peek` drill-down, optional `cubrid-tcr` CI unblock reverse-join, and decision-first reporting.
- **`daily-schedule` owns**: Daily/weekly priority planning, `my-cubrid-schedule` sheets, and "what should I do today" queries. Do **not** read or edit `my-cubrid-schedule` during routine `hcmd-check` runs; only flag completed milestones when a sheet update is warranted.
- **`track-work` (`work-tracker`) owns**: Persistent status/notes. Mutate `work-tracker` only on verified outcomes or blockers — **never** write heartbeat notes.
- **`/musk` (`/decide`) alignment**: Output follows decision-first brevity (Korean, 1 line per decision with recommendation; reversible defaults handled silently and listed concisely).

## 1. Pull Live Delta

Verify `HERDR_ENV=1` and fetch only changes since the last cursor:

```bash
hcmd digest --since-last --compact --json
```

Notes:
- Pass `--cursor <name>` if operating under a named watch/cron slot so independent callers do not consume each other's deltas.
- Pass `--no-advance` for read-only previews that must not move the watermark.
- If `changed == false` and `still_blocked` is empty and no `wait-for:` check is needed, report a 1-line status (`변경 없음 — 활성 N개, 유휴 M개`) and stop.

## 2. Triage in Priority Order

Handle entries returned by `hcmd digest` strictly in this order:

1. **`blocked` / `subtab_blocked` and `still_blocked` (Highest priority)**:
   - New `blocked` or `subtab_blocked` entries include a compact peek. Call `hcmd peek <ws> [--tab <tab_id_or_label>] --lines 60` **only** if `truncated: true` or the approval question is ambiguous.
   - Never pass `--force` to bypass `AGENT_BLOCKED`.
   - `still_blocked` lists previously surfaced blockers that were not re-peeked; remind the user briefly without flooding context.
2. **`settled_done` / `settled_idle` / `output_updated`**:
   - Note that Claude Code sessions routinely settle directly to `idle`; `--since-last` catches `working`/`busy_bg` -> `idle` transitions automatically as `settled_idle`.
   - Check `workspace.sub_tabs.any_working` before declaring a Space completely finished; if sub-tabs are still running while Tab 1 settled, report that sub-tabs remain active (or ask Tab 1 to confirm).
   - If `truncated: true` and evidence (commit/PR/test count) is cut off, run a targeted `hcmd peek <ws> --lines 60`.
   - Record verified completion/blocker outcomes in `work-tracker` per `track-work` (only when `work_item_ids` is present and outcome is verified).
3. **`closed_workspaces`**:
   - Report closed workspaces so stale `work-tracker` items can be reconciled.

## 3. Optional CI-Blocked Reverse Join (`cubrid-tcr` <-> `work-tracker`)

Run this 4-step reverse join when checking waiting items (`wait-for:<KEY>`) or when no active workers changed:

1. Find candidate waiting items:
   ```bash
   work-tracker search "wait-for:" --limit 100 --json
   ```
   Keep items with `status` in `("waiting", "blocked")`.
2. Read recent history per matched item to extract active keys since the latest transition into `waiting` or `blocked`:
   ```bash
   work-tracker history <ID> --limit 10 --json
   ```
   Collect `\bwait-for:([A-Za-z0-9._-]+)\b` tokens.
3. Query `cubrid-tcr` once for all collected keys:
   ```bash
   cubrid-tcr blockers --key <K1> --key <K2> --json
   ```
   Verify `schema_version == 1` and match `blockers[].wait_keys`. Wake an item **only** when every active wait key has `readiness == "READY"`.
4. Correlate ready items to live Herdr Spaces:
   ```bash
   hcmd find --cwd <workdir> --json
   ```
   Include `unblock_action` (`REBASE_ENGINE`, `RERUN_OR_SYNC_TC`, `REBASE_ENGINE_AND_SYNC_TC`).

## 4. Output Format (Korean Decision-First Brief)

Present only what needs action, keeping code, commands, paths, and identifiers verbatim:

1. **사용자 결정 필요** (1줄씩: `[Space/ID] 핵심 질문 -> 추천: <권장안>` — 없으면 `"결정 필요 항목 없음"`).
2. **자동 처리 / 기본값** (검증된 `work-tracker` 기록, `still_blocked` 리마인드, 정리 후보).
3. **Space 현황 요약** (`changed` 엔트리 요약 + 활성/유휴 카운트; 변경 없으면 1줄로 종료).
