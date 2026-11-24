"""Pure snapshot parsing, first-tab resolution, Claude busy check, and filtering."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from .models import (
    ATTENTION_ORDER,
    CliError,
    PrimaryPane,
    SpaceSnapshot,
    SubTabRollup,
    TabSummary,
)

TICKET_PATTERN = re.compile(r"\b(CBRD-\d+)\b", re.IGNORECASE)

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


def _build_primary_pane(
    pane: dict[str, Any],
    agent: dict[str, Any] | None,
    *,
    path_exists: Callable[[str], bool],
) -> PrimaryPane:
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

    return PrimaryPane(
        pane_id=str(pane["pane_id"]),
        cwd=cwd,
        foreground_cwd=foreground_cwd,
        cwd_exists=cwd_exists,
        agent_kind=agent_kind,
        agent_name=agent_name,
        raw_status=raw_status,
        effective_status=effective_status,
        interactive_ready=bool(merged.get("interactive_ready", False)),
        session_id=session_id,
        revision=int(merged.get("revision") or 0),
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
        "Run `herdr-spaces-status status` to list active workspace IDs and labels.",
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
