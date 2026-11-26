"""Pure snapshot parsing, first-tab resolution, Claude busy check, and filtering."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from .models import (
    ATTENTION_ORDER,
    EXIT_CONFIG,
    EXIT_USAGE,
    CallerContext,
    CliError,
    FindQuery,
    FindResult,
    PaneInfo,
    PrimaryPane,
    SpaceSnapshot,
    SubTabRollup,
    TabSummary,
    WorkspaceMatch,
    WorkspaceTabInfo,
)

TICKET_PATTERN = re.compile(r"\b(CBRD-\d+)\b", re.IGNORECASE)
_DECORATED_PREFIX = re.compile(r"^\d+\s+[▶!✓○?—]\s+")
_DECORATED_SUFFIX = re.compile(r"\s+\[t\d+\]$")

# Ported from herdr-attention's detection-buffer classifier for Claude Code.
_RULE = re.compile(r"^\s*─{3,}\s*$")
_PROMPT = re.compile(r"^\s*❯")
_CONTROL = re.compile(
    r"^\s*(?:⏵⏵|▶▶|⏸|⏵|▶)\s+.*·\s*(?:[1-9]\d*)\s+"
    r"(?:shells?|(?:background |running )?agents?|(?:background |running )?tasks?)"
    r"(?:\s*$|\s*·)",
    re.I,
)
_LIVE_ACTIVITY = re.compile(
    r"^\s*[✻✽✶✳✢✣⠂⠐⠄⠠⠁⠈⠙⠹⠸⠼⠴⠦⠧⠇⠏*·]?\s*"
    r"(?:Waiting for [1-9]\d* background agents? to finish|"
    r"[1-9]\d* MCP tasks? still running)\s*[.…]*\s*$",
    re.I,
)
_FOOTER_CONTROL = re.compile(r"^\s*(?:⏵⏵|▶▶|⏸|⏵|▶)\s+")

_VALID_RAW_STATUSES = frozenset({"working", "blocked", "done", "idle", "unknown"})


def extract_ticket_key(*candidates: str | None) -> str | None:
    """Extract the first CBRD-NNNNN ticket token from candidate strings."""
    for candidate in candidates:
        if not candidate:
            continue
        match = TICKET_PATTERN.search(candidate)
        if match:
            return match.group(1).upper()
    return None


def _normalize_status(value: Any) -> str:
    if isinstance(value, str) and value in _VALID_RAW_STATUSES:
        return value
    return "unknown"


def _compute_attention(primary_pane: PrimaryPane | None, deleted_hint: bool) -> str:
    if deleted_hint or primary_pane is None:
        return "unknown"
    status = primary_pane.effective_status
    if status == "busy_bg":
        return "working"
    if status in ATTENTION_ORDER:
        return status
    return "unknown"


def _extract_pane_fields(
    pane: dict[str, Any],
    agent: dict[str, Any] | None,
    *,
    path_exists: Callable[[str], bool],
) -> dict[str, Any]:
    merged = {**pane, **(agent or {})}
    cwd = str(merged.get("cwd") or "")
    foreground_cwd = str(merged.get("foreground_cwd") or cwd)
    cwd_exists = bool(cwd) and path_exists(cwd)

    agent_kind_raw = merged.get("agent")
    agent_kind = str(agent_kind_raw) if isinstance(agent_kind_raw, str) and agent_kind_raw else None
    agent_name_raw = merged.get("name")
    agent_name = str(agent_name_raw) if isinstance(agent_name_raw, str) and agent_name_raw else None

    raw_status = _normalize_status(merged.get("agent_status"))
    effective_status = "shell" if agent_kind is None else raw_status

    session_obj = merged.get("agent_session")
    session_id = None
    if isinstance(session_obj, dict):
        val = session_obj.get("value")
        if isinstance(val, str) and val:
            session_id = val

    return {
        "pane_id": str(pane["pane_id"]),
        "cwd": cwd,
        "foreground_cwd": foreground_cwd,
        "cwd_exists": cwd_exists,
        "agent_kind": agent_kind,
        "agent_name": agent_name,
        "raw_status": raw_status,
        "effective_status": effective_status,
        "interactive_ready": bool(merged.get("interactive_ready", False)),
        "session_id": session_id,
        "revision": int(merged.get("revision") or 0),
    }


def _build_primary_pane(
    pane: dict[str, Any],
    agent: dict[str, Any] | None,
    *,
    path_exists: Callable[[str], bool],
) -> PrimaryPane:
    return PrimaryPane(**_extract_pane_fields(pane, agent, path_exists=path_exists))


def _build_pane_info(
    pane: dict[str, Any],
    agent: dict[str, Any] | None,
    *,
    tab_id: str,
    workspace_id: str,
    index: int,
    self_pane_id: str | None,
    path_exists: Callable[[str], bool],
) -> PaneInfo:
    fields = _extract_pane_fields(pane, agent, path_exists=path_exists)
    return PaneInfo(
        tab_id=tab_id,
        workspace_id=workspace_id,
        index=index,
        is_primary=(index == 0),
        is_self=(self_pane_id is not None and fields["pane_id"] == self_pane_id),
        **fields,
    )


def _pane_effective_status(pane: dict[str, Any], agent: dict[str, Any] | None) -> str:
    merged = {**pane, **(agent or {})}
    kind = merged.get("agent")
    if not isinstance(kind, str) or not kind:
        return "shell"
    return _normalize_status(merged.get("agent_status"))


def parse_snapshot(
    raw: dict[str, Any],
    *,
    self_workspace_id: str | None = None,
    path_exists: Callable[[str], bool] = os.path.exists,
) -> list[SpaceSnapshot]:
    """Parse a Herdr session.snapshot dictionary into SpaceSnapshot models."""
    workspaces = raw.get("workspaces")
    tabs = raw.get("tabs")
    panes = raw.get("panes")
    agents = raw.get("agents")
    layouts = raw.get("layouts")
    if not all(isinstance(x, list) for x in (workspaces, tabs, panes, agents)):
        raise ValueError("Herdr snapshot is missing workspace, tab, pane, or agent lists")
    layouts_list = layouts if isinstance(layouts, list) else []

    panes_by_id: dict[str, dict[str, Any]] = {}
    panes_by_tab: dict[str, list[dict[str, Any]]] = {}
    for pane in panes:
        pid = pane.get("pane_id")
        tid = pane.get("tab_id")
        if isinstance(pid, str) and pid:
            panes_by_id[pid] = pane
        if isinstance(tid, str) and tid:
            panes_by_tab.setdefault(tid, []).append(pane)

    agents_by_pane: dict[str, dict[str, Any]] = {}
    for agent in agents:
        pid = agent.get("pane_id")
        if isinstance(pid, str) and pid:
            agents_by_pane[pid] = agent

    layout_pane_order: dict[str, list[str]] = {}
    for layout in layouts_list:
        tid = layout.get("tab_id")
        layout_panes = layout.get("panes")
        if isinstance(tid, str) and isinstance(layout_panes, list):
            ordered_ids: list[str] = []
            for entry in layout_panes:
                if isinstance(entry, str):
                    ordered_ids.append(entry)
                elif isinstance(entry, dict) and isinstance(entry.get("pane_id"), str):
                    ordered_ids.append(entry["pane_id"])
            if ordered_ids:
                layout_pane_order[tid] = ordered_ids

    tabs_by_workspace: dict[str, list[dict[str, Any]]] = {}
    for tab in tabs:
        wid = tab.get("workspace_id")
        if isinstance(wid, str) and wid:
            tabs_by_workspace.setdefault(wid, []).append(tab)

    result: list[SpaceSnapshot] = []
    for ws in workspaces:
        wid = str(ws["workspace_id"])
        label = str(ws.get("label") or "")
        number = int(ws.get("number") or 0)
        focused = bool(ws.get("focused", False))
        ws_tabs = tabs_by_workspace.get(wid, [])

        primary_tab = ws_tabs[0] if ws_tabs else None
        primary_tab_id = str(primary_tab["tab_id"]) if primary_tab else None
        primary_tab_label = str(primary_tab.get("label") or "") if primary_tab else None

        primary_pane_raw: dict[str, Any] | None = None
        if primary_tab_id is not None:
            for candidate_pid in layout_pane_order.get(primary_tab_id, ()):
                candidate = panes_by_id.get(candidate_pid)
                if candidate is not None and candidate.get("tab_id") == primary_tab_id:
                    primary_pane_raw = candidate
                    break
            if primary_pane_raw is None:
                tab_panes = panes_by_tab.get(primary_tab_id, [])
                if tab_panes:
                    primary_pane_raw = tab_panes[0]

        primary_pane = (
            _build_primary_pane(
                primary_pane_raw,
                agents_by_pane.get(str(primary_pane_raw["pane_id"])),
                path_exists=path_exists,
            )
            if primary_pane_raw is not None
            else None
        )

        primary_pane_id = primary_pane.pane_id if primary_pane else None
        status_counts: dict[str, int] = {}
        extra_pane_count = 0
        for tab in ws_tabs:
            tid = str(tab["tab_id"])
            for pane in panes_by_tab.get(tid, []):
                pid = str(pane["pane_id"])
                if pid == primary_pane_id:
                    continue
                extra_pane_count += 1
                st = _pane_effective_status(pane, agents_by_pane.get(pid))
                status_counts[st] = status_counts.get(st, 0) + 1

        sub_tabs = SubTabRollup(
            extra_tab_count=max(0, len(ws_tabs) - 1),
            extra_pane_count=extra_pane_count,
            status_counts=status_counts,
            any_blocked=status_counts.get("blocked", 0) > 0,
            any_working=status_counts.get("working", 0) > 0,
        )

        tab_summaries = tuple(
            TabSummary(
                tab_id=str(tab["tab_id"]),
                label=str(tab.get("label") or ""),
                number=int(tab.get("number") or 0),
                pane_count=int(
                    tab.get("pane_count") or len(panes_by_tab.get(str(tab["tab_id"]), []))
                ),
                agent_status=_normalize_status(tab.get("agent_status")),
                is_primary=(idx == 0),
            )
            for idx, tab in enumerate(ws_tabs)
        )

        deleted_hint = "(deleted)" in label.lower() or (
            primary_pane is not None and bool(primary_pane.cwd) and not primary_pane.cwd_exists
        )
        cwd_name = Path(primary_pane.cwd).name if primary_pane and primary_pane.cwd else None
        ticket_key = extract_ticket_key(label, cwd_name)
        attention = _compute_attention(primary_pane, deleted_hint)

        result.append(
            SpaceSnapshot(
                workspace_id=wid,
                label=label,
                number=number,
                focused=focused,
                is_self=bool(self_workspace_id and wid == self_workspace_id),
                deleted_hint=deleted_hint,
                primary_tab_id=primary_tab_id,
                primary_tab_label=primary_tab_label,
                primary_pane=primary_pane,
                sub_tabs=sub_tabs,
                attention=attention,
                ticket_key=ticket_key,
                work_item_ids=(),
                tabs=tab_summaries,
            )
        )
    return result


def classify_claude_detection(text: str) -> str:
    """Classify a Claude detection snapshot as claude_busy, eligible, or unverified."""
    lines = text.splitlines()
    box: tuple[int, int] | None = None
    for end in range(len(lines) - 1, -1, -1):
        if _RULE.match(lines[end]):
            for start in range(end - 1, -1, -1):
                if _RULE.match(lines[start]):
                    if any(_PROMPT.match(line) for line in lines[start + 1 : end]):
                        box = (start, end)
                    break
            break
    if box is None:
        return "unverified"
    start, end = box
    footer = lines[end + 1 :]
    if any(_CONTROL.search(line) for line in footer):
        return "claude_busy"
    above = next((line for line in reversed(lines[:start]) if line.strip()), "")
    if _LIVE_ACTIVITY.match(above):
        return "claude_busy"
    if not any(_FOOTER_CONTROL.match(line) for line in footer):
        return "unverified"
    return "eligible"


def verify_claude_busy(
    spaces: Sequence[SpaceSnapshot],
    read_detection: Callable[[str], str],
) -> list[SpaceSnapshot]:
    """Promote idle/done Claude primary panes with active background tasks to busy_bg."""
    updated: list[SpaceSnapshot] = []
    for space in spaces:
        pane = space.primary_pane
        if pane is not None and pane.agent_kind == "claude" and pane.raw_status in {"idle", "done"}:
            try:
                text = read_detection(pane.pane_id)
                verdict = classify_claude_detection(text)
            except (CliError, ValueError, KeyError, TypeError):
                verdict = "unverified"
            if verdict == "claude_busy":
                new_pane = replace(pane, effective_status="busy_bg")
                updated.append(
                    replace(
                        space,
                        primary_pane=new_pane,
                        attention=_compute_attention(new_pane, space.deleted_hint),
                    )
                )
                continue
        updated.append(space)
    return updated


def correlate_work_tracker(
    spaces: Sequence[SpaceSnapshot],
    items: Sequence[dict[str, Any]],
    *,
    home_dir: str | None = None,
) -> list[SpaceSnapshot]:
    """Attach matching work-tracker item IDs to each SpaceSnapshot."""
    if not items:
        return list(spaces)
    normalized_home = os.path.normpath(os.path.abspath(home_dir or str(Path.home())))

    updated: list[SpaceSnapshot] = []
    for space in spaces:
        pane_cwd = (
            os.path.normpath(os.path.abspath(space.primary_pane.cwd))
            if space.primary_pane and space.primary_pane.cwd
            else None
        )
        matched_ids: list[int] = []
        for item in items:
            item_id = item.get("id")
            if not isinstance(item_id, int):
                continue
            item_workdir = item.get("workdir")
            norm_workdir = (
                os.path.normpath(os.path.abspath(item_workdir))
                if isinstance(item_workdir, str) and item_workdir
                else None
            )
            item_ticket = extract_ticket_key(
                item.get("title") if isinstance(item.get("title"), str) else None,
                Path(norm_workdir).name if norm_workdir else None,
            )

            cwd_match = (
                pane_cwd is not None
                and norm_workdir is not None
                and pane_cwd == norm_workdir
                and not (pane_cwd == normalized_home and space.ticket_key != item_ticket)
            )
            ticket_match = (
                space.ticket_key is not None
                and item_ticket is not None
                and space.ticket_key == item_ticket
            )
            if cwd_match or ticket_match:
                matched_ids.append(item_id)

        updated.append(replace(space, work_item_ids=tuple(matched_ids)))
    return updated


def resolve_target(spaces: Sequence[SpaceSnapshot], selector: str) -> SpaceSnapshot:
    """Resolve a workspace selector by ID, exact label, case-insensitive label, or ticket key."""
    if not selector:
        raise CliError(
            "INVALID_TARGET",
            "Workspace selector cannot be empty.",
            "Pass a workspace ID (e.g. w1), workspace label, or ticket key (e.g. CBRD-10001).",
        )

    by_id = [s for s in spaces if s.workspace_id == selector]
    if len(by_id) == 1:
        return by_id[0]

    by_exact_label = [s for s in spaces if s.label == selector]
    if len(by_exact_label) == 1:
        return by_exact_label[0]
    if len(by_exact_label) > 1:
        raise CliError(
            "SPACE_AMBIGUOUS",
            f"Multiple workspaces match label {selector!r}.",
            "Pass an exact workspace ID instead.",
            details={"candidates": [s.workspace_id for s in by_exact_label]},
        )

    lowered = selector.lower()
    by_ci_label = [s for s in spaces if s.label.lower() == lowered]
    if len(by_ci_label) == 1:
        return by_ci_label[0]
    if len(by_ci_label) > 1:
        raise CliError(
            "SPACE_AMBIGUOUS",
            f"Multiple workspaces match label {selector!r} (case-insensitive).",
            "Pass an exact workspace ID instead.",
            details={"candidates": [s.workspace_id for s in by_ci_label]},
        )

    ticket = selector.upper()
    by_ticket = [s for s in spaces if s.ticket_key == ticket]
    if len(by_ticket) == 1:
        return by_ticket[0]
    if len(by_ticket) > 1:
        raise CliError(
            "SPACE_AMBIGUOUS",
            f"Multiple workspaces match ticket {ticket}.",
            "Pass an exact workspace ID or full workspace label.",
            details={"candidates": [s.workspace_id for s in by_ticket]},
        )

    raise CliError(
        "SPACE_NOT_FOUND",
        f"No workspace found matching {selector!r}.",
        "Run `hcmd status` to list active workspace IDs and labels.",
    )


def filter_spaces(
    spaces: Sequence[SpaceSnapshot],
    *,
    workers_only: bool = False,
    attention_only: bool = False,
    workspace: str | None = None,
    sort: str = "attention",
) -> list[SpaceSnapshot]:
    """Filter and sort SpaceSnapshot records."""
    if workspace is not None:
        selected = [resolve_target(spaces, workspace)]
    else:
        selected = list(spaces)

    if workers_only:
        selected = [s for s in selected if not s.is_self]

    if attention_only:
        selected = [
            s
            for s in selected
            if s.attention in {"blocked", "done", "shell", "idle"} or s.sub_tabs.any_blocked
        ]

    if sort == "attention":
        selected.sort(
            key=lambda s: (
                0 if s.sub_tabs.any_blocked else 1,
                ATTENTION_ORDER.get(s.attention, 99),
                s.number,
                s.workspace_id,
            )
        )
    elif sort == "number":
        selected.sort(key=lambda s: (s.number, s.workspace_id))

    return selected


def collect_live_agent_names(raw: dict[str, Any]) -> set[str]:
    """Return all live unique agent names currently registered in a raw snapshot."""
    names: set[str] = set()
    for agent in raw.get("agents", []):
        name = agent.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def strip_tab_decorations(label: str) -> str:
    """Strip herdr-tab-status position/marker prefix and `[tN]` suffix from a tab label."""
    cleaned = _DECORATED_SUFFIX.sub("", label)
    cleaned = _DECORATED_PREFIX.sub("", cleaned)
    return cleaned.strip() or label


def _index_snapshot_entities(
    raw: dict[str, Any],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, list[dict[str, Any]]],
    dict[str, dict[str, Any]],
    dict[str, list[str]],
]:
    tabs = raw.get("tabs")
    panes = raw.get("panes")
    agents = raw.get("agents")
    layouts = raw.get("layouts")
    if not all(isinstance(x, list) for x in (tabs, panes, agents)):
        raise ValueError("Herdr snapshot is missing tab, pane, or agent lists")
    layouts_list = layouts if isinstance(layouts, list) else []

    panes_by_id: dict[str, dict[str, Any]] = {}
    panes_by_tab: dict[str, list[dict[str, Any]]] = {}
    for pane in panes:
        pid = pane.get("pane_id")
        tid = pane.get("tab_id")
        if isinstance(pid, str) and pid:
            panes_by_id[pid] = pane
        if isinstance(tid, str) and tid:
            panes_by_tab.setdefault(tid, []).append(pane)

    agents_by_pane: dict[str, dict[str, Any]] = {}
    for agent in agents:
        pid = agent.get("pane_id")
        if isinstance(pid, str) and pid:
            agents_by_pane[pid] = agent

    layout_pane_order: dict[str, list[str]] = {}
    for layout in layouts_list:
        tid = layout.get("tab_id")
        layout_panes = layout.get("panes")
        if isinstance(tid, str) and isinstance(layout_panes, list):
            ordered_ids: list[str] = []
            for entry in layout_panes:
                if isinstance(entry, str):
                    ordered_ids.append(entry)
                elif isinstance(entry, dict) and isinstance(entry.get("pane_id"), str):
                    ordered_ids.append(entry["pane_id"])
            if ordered_ids:
                layout_pane_order[tid] = ordered_ids

    return panes_by_id, panes_by_tab, agents_by_pane, layout_pane_order


def _ordered_tab_panes(
    tab_id: str,
    panes_by_id: dict[str, dict[str, Any]],
    panes_by_tab: dict[str, list[dict[str, Any]]],
    layout_pane_order: dict[str, list[str]],
) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pid in layout_pane_order.get(tab_id, ()):
        candidate = panes_by_id.get(pid)
        if candidate is not None and candidate.get("tab_id") == tab_id and pid not in seen:
            ordered.append(candidate)
            seen.add(pid)
    for pane in panes_by_tab.get(tab_id, []):
        pid = str(pane.get("pane_id") or "")
        if pid and pid not in seen:
            ordered.append(pane)
            seen.add(pid)
    return ordered


def parse_workspace_tabs(
    raw: dict[str, Any],
    workspace_id: str,
    *,
    self_tab_id: str | None = None,
    self_pane_id: str | None = None,
    path_exists: Callable[[str], bool] = os.path.exists,
) -> list[WorkspaceTabInfo]:
    """Parse ordered tabs and panes belonging to a single Herdr workspace."""
    panes_by_id, panes_by_tab, agents_by_pane, layout_pane_order = _index_snapshot_entities(raw)
    ws_tabs = [tab for tab in raw.get("tabs", []) if tab.get("workspace_id") == workspace_id]

    result: list[WorkspaceTabInfo] = []
    for tab_idx, tab in enumerate(ws_tabs):
        tid = str(tab["tab_id"])
        label = str(tab.get("label") or "")
        raw_panes = _ordered_tab_panes(tid, panes_by_id, panes_by_tab, layout_pane_order)
        pane_infos = tuple(
            _build_pane_info(
                pane,
                agents_by_pane.get(str(pane["pane_id"])),
                tab_id=tid,
                workspace_id=workspace_id,
                index=pane_idx,
                self_pane_id=self_pane_id,
                path_exists=path_exists,
            )
            for pane_idx, pane in enumerate(raw_panes)
        )
        primary_pane = pane_infos[0] if pane_infos else None
        fallback_status = (
            "shell" if primary_pane is not None else _normalize_status(tab.get("agent_status"))
        )
        agent_status = (
            primary_pane.effective_status
            if primary_pane and primary_pane.agent_kind is not None
            else fallback_status
        )
        result.append(
            WorkspaceTabInfo(
                tab_id=tid,
                workspace_id=workspace_id,
                label=label,
                base_label=strip_tab_decorations(label),
                index=tab_idx,
                number=int(tab.get("number") or (tab_idx + 1)),
                is_primary=(tab_idx == 0),
                is_self=(self_tab_id is not None and tid == self_tab_id),
                pane_count=int(tab.get("pane_count") or len(pane_infos)),
                agent_status=agent_status,
                primary_pane=primary_pane,
                panes=pane_infos,
            )
        )
    return result


def resolve_caller_context(
    raw: dict[str, Any],
    env: Mapping[str, str],
    *,
    path_exists: Callable[[str], bool] = os.path.exists,
) -> CallerContext:
    """Resolve the calling pane's hierarchy role from Herdr env vars and live snapshot."""
    workspace_id = env.get("HERDR_WORKSPACE_ID", "").strip()
    tab_id = env.get("HERDR_TAB_ID", "").strip()
    pane_id = env.get("HERDR_PANE_ID", "").strip()
    if not (workspace_id and tab_id and pane_id):
        raise CliError(
            "MISSING_CALLER_CONTEXT",
            "HERDR_WORKSPACE_ID, HERDR_TAB_ID, and HERDR_PANE_ID are required.",
            "Run this command from inside a Herdr-managed pane.",
            exit_code=EXIT_CONFIG,
        )

    tabs = parse_workspace_tabs(
        raw,
        workspace_id,
        self_tab_id=tab_id,
        self_pane_id=pane_id,
        path_exists=path_exists,
    )
    tab = next((t for t in tabs if t.tab_id == tab_id), None)
    if tab is None:
        raise CliError(
            "CALLER_NOT_IN_SNAPSHOT",
            f"Calling tab {tab_id} in workspace {workspace_id} was not found in Herdr snapshot.",
            "Verify HERDR_WORKSPACE_ID and HERDR_TAB_ID match the active Herdr session.",
            exit_code=EXIT_CONFIG,
        )
    pane = next((p for p in tab.panes if p.pane_id == pane_id), None)
    if pane is None:
        raise CliError(
            "CALLER_NOT_IN_SNAPSHOT",
            f"Calling pane {pane_id} in tab {tab_id} was not found in Herdr snapshot.",
            "Verify HERDR_PANE_ID matches the active Herdr session.",
            exit_code=EXIT_CONFIG,
        )

    tab_role_env = env.get("HERDR_TAB_ROLE", "").strip().lower()
    pane_role_env = env.get("HERDR_PANE_ROLE", "").strip().lower()
    if pane_role_env == "worker" or pane.index > 0:
        role = "pane_worker"
    elif tab_role_env == "worker" or tab.index > 0:
        role = "tab_lead"
    else:
        role = "space_lead"

    return CallerContext(
        workspace_id=workspace_id,
        tab_id=tab_id,
        pane_id=pane_id,
        tab_index=tab.index,
        pane_index=pane.index,
        role=role,
    )


def count_workspace_non_primary_agents(tabs: Sequence[WorkspaceTabInfo]) -> int:
    """Count live AI agent panes in a workspace excluding the primary Space Lead pane."""
    count = 0
    for tab in tabs:
        for pane in tab.panes:
            if tab.is_primary and pane.is_primary:
                continue
            if pane.agent_kind is not None:
                count += 1
    return count


def count_session_working_agents(raw: dict[str, Any]) -> int:
    """Count AI agent panes across the entire Herdr session currently in working state."""
    agents_by_pane: dict[str, dict[str, Any]] = {}
    for agent in raw.get("agents", []):
        pid = agent.get("pane_id")
        if isinstance(pid, str) and pid:
            agents_by_pane[pid] = agent
    working = 0
    for pane in raw.get("panes", []):
        pid = pane.get("pane_id")
        if (
            isinstance(pid, str)
            and _pane_effective_status(pane, agents_by_pane.get(pid)) == "working"
        ):
            working += 1
    return working


def verify_panes_claude_busy(
    panes: Sequence[PaneInfo],
    read_detection: Callable[[str], str],
) -> list[PaneInfo]:
    """Promote idle/done Claude panes with active background tasks to busy_bg."""
    updated: list[PaneInfo] = []
    for pane in panes:
        if pane.agent_kind == "claude" and pane.raw_status in {"idle", "done"}:
            try:
                verdict = classify_claude_detection(read_detection(pane.pane_id))
            except (CliError, ValueError, KeyError, TypeError):
                verdict = "unverified"
            if verdict == "claude_busy":
                updated.append(replace(pane, effective_status="busy_bg"))
                continue
        updated.append(pane)
    return updated


def resolve_tab_target(tabs: Sequence[WorkspaceTabInfo], selector: str) -> WorkspaceTabInfo:
    """Resolve a tab inside one workspace by tab_id, short ID, pane_id, label, or base_label."""
    if not selector:
        raise CliError(
            "INVALID_TARGET",
            "Tab selector cannot be empty.",
            "Pass a tab ID (e.g. w1:t2 or t2) or tab label.",
        )

    for match_fn, err_label in (
        (lambda t: t.tab_id == selector, "tab ID"),
        (
            lambda t: (
                selector.startswith("t")
                and selector[1:].isdigit()
                and t.tab_id.endswith(f":{selector}")
            ),
            "short tab ID",
        ),
        (lambda t: any(p.pane_id == selector for p in t.panes), "pane ID"),
        (lambda t: t.label == selector or t.base_label == selector, "label"),
        (
            lambda t: (
                t.label.lower() == selector.lower() or t.base_label.lower() == selector.lower()
            ),
            "label",
        ),
    ):
        matches = [t for t in tabs if match_fn(t)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise CliError(
                "TAB_AMBIGUOUS",
                f"Multiple tabs match {err_label} {selector!r}.",
                "Pass an exact tab ID instead.",
                details={"candidates": [t.tab_id for t in matches]},
            )

    raise CliError(
        "TAB_NOT_FOUND",
        f"No tab found matching {selector!r}.",
        "Run `hcmd tab list` to inspect tabs in this workspace.",
    )


def resolve_pane_target(panes: Sequence[PaneInfo], selector: str) -> PaneInfo:
    """Resolve a pane inside one tab by pane_id, short ID, 1-based position, or agent name."""
    if not selector:
        raise CliError(
            "INVALID_TARGET",
            "Pane selector cannot be empty.",
            "Pass a pane ID (e.g. w1:p2 or p2), 1-based pane index, or agent name.",
        )

    lowered = selector.lower()
    for match_fn, err_label in (
        (lambda p: p.pane_id == selector, "pane ID"),
        (
            lambda p: (
                selector.startswith("p")
                and selector[1:].isdigit()
                and p.pane_id.endswith(f":{selector}")
            ),
            "short pane ID",
        ),
        (
            lambda p: selector.isdigit() and (p.index + 1) == int(selector),
            "pane position",
        ),
        (lambda p: (p.agent_name or "").lower() == lowered and bool(p.agent_name), "agent name"),
    ):
        matches = [p for p in panes if match_fn(p)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise CliError(
                "PANE_AMBIGUOUS",
                f"Multiple panes match {err_label} {selector!r}.",
                "Pass an exact pane ID instead.",
                details={"candidates": [p.pane_id for p in matches]},
            )

    raise CliError(
        "PANE_NOT_FOUND",
        f"No pane found matching {selector!r}.",
        "Run `hcmd pane list` to inspect panes in this tab.",
    )


_PRIMARY_EXACT_REASONS = frozenset(
    {
        "workspace_id_exact",
        "label_exact",
        "label_ci_exact",
        "agent_name_exact",
        "cwd_exact",
        "foreground_cwd_exact",
        "cwd_basename_exact",
        "ticket_exact",
        "pr_label_match",
        "pr_cwd_match",
        "pr_agent_name_match",
    }
)
_SUB_PANE_EXACT_REASONS = frozenset(
    {
        "sub_pane_agent_name_exact",
        "sub_pane_cwd_exact",
    }
)
_EXACT_MATCH_REASONS = _PRIMARY_EXACT_REASONS | _SUB_PANE_EXACT_REASONS


def is_primary_exact_match(match: WorkspaceMatch) -> bool:
    """Return True when a match includes at least one exact primary-space reason."""
    return any(reason in _PRIMARY_EXACT_REASONS for reason in match.match_reasons)


def _is_space_busy_or_blocked(space: SpaceSnapshot) -> bool:
    pane = space.primary_pane
    if pane is not None and pane.effective_status in {"working", "busy_bg", "blocked"}:
        return True
    return (
        space.attention in {"working", "blocked"}
        or space.sub_tabs.any_working
        or space.sub_tabs.any_blocked
    )


def compute_find_recommendation(matches: Sequence[WorkspaceMatch]) -> str:
    """Compute state-aware pre-launch recommendation from matched workspaces."""
    if not matches:
        return "create_new"
    if any(_is_space_busy_or_blocked(m.space) for m in matches):
        return "warn_busy"
    if len(matches) == 1:
        top = matches[0]
        pane = top.space.primary_pane
        if (
            is_primary_exact_match(top)
            and pane is not None
            and pane.agent_kind is None
            and pane.effective_status == "shell"
        ):
            return "reuse_empty_shell"
    return "ask_reuse"


def _normalize_path_candidates(
    path: str,
    realpath_fn: Callable[[str], str] = os.path.realpath,
) -> tuple[str, str]:
    abs_path = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
    real_path = os.path.normpath(realpath_fn(abs_path))
    return abs_path, real_path


def _is_subdir(child: str, parent: str) -> bool:
    return child != parent and child.startswith(parent.rstrip("/") + "/")


def _matches_pr_token(text: str | None, norm_pr: str) -> bool:
    if not text:
        return False
    pattern = rf"(?<![a-z0-9])pr[-_#]?{re.escape(norm_pr)}(?!\d)"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _clean_opt(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def collect_workspace_panes(
    raw: dict[str, Any],
    *,
    path_exists: Callable[[str], bool] = os.path.exists,
) -> dict[str, tuple[PaneInfo, ...]]:
    """Collect all ordered PaneInfo records across every tab for each workspace."""
    result: dict[str, tuple[PaneInfo, ...]] = {}
    for ws in raw.get("workspaces", []):
        wid = ws.get("workspace_id")
        if not isinstance(wid, str) or not wid:
            continue
        tabs = parse_workspace_tabs(raw, wid, path_exists=path_exists)
        panes: list[PaneInfo] = []
        for tab in tabs:
            panes.extend(tab.panes)
        result[wid] = tuple(panes)
    return result


def find_matching_spaces(
    spaces: Sequence[SpaceSnapshot],
    *,
    query: str | None = None,
    name: str | None = None,
    label: str | None = None,
    cwd: str | None = None,
    ticket: str | None = None,
    pr_number: str | None = None,
    include_self: bool = False,
    include_deleted: bool = False,
    panes_by_workspace: Mapping[str, Sequence[PaneInfo]] | None = None,
    home_dir: str | None = None,
    realpath_fn: Callable[[str], str] = os.path.realpath,
) -> FindResult:
    """Find existing Herdr workspaces matching name, label, cwd/pwd, ticket, or PR."""
    clean_query = _clean_opt(query)
    clean_name = _clean_opt(name)
    clean_label = _clean_opt(label)
    clean_cwd = _clean_opt(cwd)
    clean_ticket = _clean_opt(ticket)
    clean_pr = _clean_opt(pr_number)

    if not any((clean_query, clean_name, clean_label, clean_cwd, clean_ticket, clean_pr)):
        raise CliError(
            "MISSING_QUERY",
            "Specify a query or at least one of --name, --label, --cwd/--pwd, --ticket, or --pr.",
            "Example: `hcmd find --label my-repo --cwd ~/gh/my-repo --json`.",
            exit_code=EXIT_USAGE,
        )

    norm_ticket = clean_ticket.upper() if clean_ticket else None
    norm_pr = clean_pr.lstrip("#") if clean_pr else None
    if norm_pr is not None and not norm_pr.isdigit():
        raise CliError(
            "INVALID_PR",
            f"PR identifier must be a number, got {pr_number!r}.",
            "Pass a numeric PR ID such as `--pr 1234`.",
            exit_code=EXIT_USAGE,
        )

    norm_cwd: str | None = None
    target_paths: set[str] = set()
    if clean_cwd is not None:
        norm_cwd, real_cwd = _normalize_path_candidates(clean_cwd, realpath_fn)
        target_paths.update((norm_cwd, real_cwd))
    elif clean_query is not None and (
        clean_query.startswith(("~", "/", "./", "../")) or "/" in clean_query
    ):
        target_paths.update(_normalize_path_candidates(clean_query, realpath_fn))

    home_paths = set(_normalize_path_candidates(home_dir or str(Path.home()), realpath_fn)) | {"/"}

    matches: list[WorkspaceMatch] = []
    for space in spaces:
        if space.is_self and not include_self:
            continue
        if space.deleted_hint and not include_deleted:
            continue

        reasons: list[str] = []
        matched_panes: list[str] = []

        def add_reason(reason: str, *, _reasons: list[str] = reasons) -> None:
            if reason not in _reasons:
                _reasons.append(reason)

        def add_pane(pane_id: str, *, _panes: list[str] = matched_panes) -> None:
            if pane_id not in _panes:
                _panes.append(pane_id)

        pane = space.primary_pane
        ws_panes = panes_by_workspace.get(space.workspace_id, ()) if panes_by_workspace else ()
        sub_panes = [p for p in ws_panes if pane is None or p.pane_id != pane.pane_id]
        primary_cwd_basename = Path(pane.cwd).name if pane and pane.cwd else ""

        if clean_label is not None:
            if space.label == clean_label:
                add_reason("label_exact")
            elif space.label.lower() == clean_label.lower():
                add_reason("label_ci_exact")
            elif len(clean_label) >= 2 and clean_label.lower() in space.label.lower():
                add_reason("label_contains")
            label_ticket = extract_ticket_key(clean_label)
            if label_ticket is not None and space.ticket_key == label_ticket:
                add_reason("ticket_exact")

        for term in (clean_name, clean_query):
            if term is None:
                continue
            term_lower = term.lower()
            if space.workspace_id == term:
                add_reason("workspace_id_exact")
            if space.label == term:
                add_reason("label_exact")
            elif space.label.lower() == term_lower:
                add_reason("label_ci_exact")
            if pane and pane.agent_name and pane.agent_name.lower() == term_lower:
                add_reason("agent_name_exact")
                add_pane(pane.pane_id)
            for sp in sub_panes:
                if sp.agent_name and sp.agent_name.lower() == term_lower:
                    add_reason("sub_pane_agent_name_exact")
                    add_pane(sp.pane_id)
            if primary_cwd_basename and primary_cwd_basename.lower() == term_lower:
                add_reason("cwd_basename_exact")
                if pane:
                    add_pane(pane.pane_id)
            term_ticket = extract_ticket_key(term)
            if term_ticket is not None and space.ticket_key == term_ticket:
                add_reason("ticket_exact")
            if len(term) >= 2:
                if (
                    "label_exact" not in reasons
                    and "label_ci_exact" not in reasons
                    and term_lower in space.label.lower()
                ):
                    add_reason("label_contains")
                if (
                    "agent_name_exact" not in reasons
                    and pane
                    and pane.agent_name
                    and term_lower in pane.agent_name.lower()
                ):
                    add_reason("agent_name_contains")
                    add_pane(pane.pane_id)
                if (
                    "cwd_basename_exact" not in reasons
                    and primary_cwd_basename
                    and term_lower in primary_cwd_basename.lower()
                ):
                    add_reason("cwd_basename_contains")
                    if pane:
                        add_pane(pane.pane_id)

        if target_paths:
            if pane is not None:
                pane_cwd_set = (
                    set(_normalize_path_candidates(pane.cwd, realpath_fn)) if pane.cwd else set()
                )
                fg_cwd_set = (
                    set(_normalize_path_candidates(pane.foreground_cwd, realpath_fn))
                    if pane.foreground_cwd
                    else set()
                )
                if target_paths & pane_cwd_set:
                    add_reason("cwd_exact")
                    add_pane(pane.pane_id)
                elif target_paths & fg_cwd_set:
                    add_reason("foreground_cwd_exact")
                    add_pane(pane.pane_id)
                elif not (target_paths & home_paths):
                    if any(
                        _is_subdir(p_path, t_path)
                        for p_path in (pane_cwd_set | fg_cwd_set)
                        for t_path in target_paths
                    ):
                        add_reason("cwd_subdir")
                        add_pane(pane.pane_id)
            for sp in sub_panes:
                sp_paths: set[str] = set()
                if sp.cwd:
                    sp_paths.update(_normalize_path_candidates(sp.cwd, realpath_fn))
                if sp.foreground_cwd:
                    sp_paths.update(_normalize_path_candidates(sp.foreground_cwd, realpath_fn))
                if target_paths & sp_paths:
                    add_reason("sub_pane_cwd_exact")
                    add_pane(sp.pane_id)

        if norm_ticket is not None:
            if space.ticket_key == norm_ticket:
                add_reason("ticket_exact")
            elif norm_ticket.lower() in space.label.lower() or (
                pane is not None and norm_ticket.lower() in pane.cwd.lower()
            ):
                add_reason("ticket_contains")

        if norm_pr is not None:
            if _matches_pr_token(space.label, norm_pr):
                add_reason("pr_label_match")
            if primary_cwd_basename and _matches_pr_token(primary_cwd_basename, norm_pr):
                add_reason("pr_cwd_match")
                if pane:
                    add_pane(pane.pane_id)
            if pane and _matches_pr_token(pane.agent_name, norm_pr):
                add_reason("pr_agent_name_match")
                add_pane(pane.pane_id)

        if reasons:
            ordered_reasons = (
                *[r for r in reasons if r in _PRIMARY_EXACT_REASONS],
                *[r for r in reasons if r in _SUB_PANE_EXACT_REASONS],
                *[r for r in reasons if r not in _EXACT_MATCH_REASONS],
            )
            strength = (
                "exact" if any(r in _EXACT_MATCH_REASONS for r in ordered_reasons) else "partial"
            )
            matches.append(
                WorkspaceMatch(
                    space=space,
                    match_reasons=ordered_reasons,
                    match_strength=strength,
                    matched_panes=tuple(matched_panes),
                )
            )

    matches.sort(
        key=lambda m: (
            0 if m.match_strength == "exact" else 1,
            0 if is_primary_exact_match(m) else 1,
            0 if "label_exact" in m.match_reasons else 1,
            0 if (norm_ticket is not None and m.space.label.upper() == norm_ticket) else 1,
            -len(m.match_reasons),
            m.space.number,
            m.space.workspace_id,
        )
    )

    return FindResult(
        query=FindQuery(
            query=clean_query,
            name=clean_name,
            label=clean_label,
            cwd=norm_cwd,
            ticket=norm_ticket,
            pr_number=norm_pr,
            include_self=include_self,
            include_deleted=include_deleted,
        ),
        found=bool(matches),
        recommendation=compute_find_recommendation(matches),
        matches=tuple(matches),
    )
