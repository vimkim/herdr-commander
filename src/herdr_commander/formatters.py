"""Output mode resolution and human, plain, and JSON formatters."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .models import (
    EXIT_USAGE,
    SCHEMA_VERSION,
    CallerContext,
    CliError,
    EnsureResult,
    FindResult,
    PaneCloseResult,
    PaneInfo,
    PaneSpawnResult,
    PaneWaitResult,
    PeekResult,
    SendResult,
    SpaceSnapshot,
    TabCloseResult,
    TabSpawnResult,
    TabWaitResult,
    WaitResult,
    WorkspaceTabInfo,
)

STATUS_FIELDS: tuple[str, ...] = (
    "number",
    "workspace_id",
    "label",
    "attention",
    "effective_status",
    "agent_kind",
    "agent_name",
    "primary_tab_id",
    "pane_id",
    "sub_tabs",
    "ticket_key",
    "work_item_ids",
    "cwd",
    "focused",
    "is_self",
    "deleted_hint",
)

DEFAULT_PLAIN_FIELDS: tuple[str, ...] = (
    "number",
    "workspace_id",
    "attention",
    "agent_kind",
    "sub_tabs",
    "ticket_key",
    "label",
    "cwd",
)

_ANSI_RESET = "\033[0m"
_ANSI_DIM = "\033[2m"
_STATUS_COLOR: dict[str, str] = {
    "blocked": "\033[31;1m",
    "done": "\033[32;1m",
    "shell": "\033[33m",
    "idle": "\033[36m",
    "working": "\033[34m",
    "busy_bg": "\033[34m",
    "unknown": "\033[90m",
}


@dataclass(frozen=True)
class OutputMode:
    """Resolved output format and color capabilities."""

    fmt: str  # "human" | "plain" | "json"
    color: bool
    is_tty_stdout: bool
    is_tty_stderr: bool


def resolve_output_mode(
    *,
    json_flag: bool,
    plain_flag: bool,
    no_color_flag: bool,
    env: Mapping[str, str],
    is_tty_stdout: bool,
    is_tty_stderr: bool,
) -> OutputMode:
    """Resolve output mode once according to CLI format and TTY priority rules."""
    if json_flag and plain_flag:
        raise CliError(
            "CONFLICTING_FORMAT",
            "Specify either --json or --plain, not both.",
            "Remove one format flag.",
            exit_code=EXIT_USAGE,
        )
    if json_flag:
        return OutputMode("json", False, is_tty_stdout, is_tty_stderr)
    if plain_flag:
        return OutputMode("plain", False, is_tty_stdout, is_tty_stderr)

    color = is_tty_stdout
    if no_color_flag:
        color = False
    elif "FORCE_COLOR" in env:
        force = env["FORCE_COLOR"]
        color = force in {"", "1", "2", "3", "true"}
    elif env.get("NO_COLOR") or "NODE_DISABLE_COLORS" in env:
        color = False
    elif env.get("TERM") == "dumb":
        color = False

    return OutputMode("human", color, is_tty_stdout, is_tty_stderr)


def parse_fields(raw_fields: str | None) -> tuple[str, ...] | None:
    """Parse and validate comma-separated `--fields` column names."""
    if raw_fields is None:
        return None
    fields = tuple(part.strip() for part in raw_fields.split(",") if part.strip())
    if not fields:
        raise CliError(
            "INVALID_FIELDS",
            "--fields cannot be empty.",
            f"Choose from: {', '.join(STATUS_FIELDS)}.",
            exit_code=EXIT_USAGE,
        )
    unknown = [f for f in fields if f not in STATUS_FIELDS]
    if unknown:
        raise CliError(
            "UNKNOWN_FIELD",
            f"Unknown field(s): {', '.join(unknown)}.",
            f"Valid fields: {', '.join(STATUS_FIELDS)}.",
            exit_code=EXIT_USAGE,
        )
    return fields


def _shorten_path(path: str) -> str:
    if not path:
        return "-"
    home = str(os.path.expanduser("~"))
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~/" + path[len(home) + 1 :]
    return path


def _sub_tab_badge(space: SpaceSnapshot) -> str:
    sub = space.sub_tabs
    if sub.extra_tab_count == 0 and sub.extra_pane_count == 0:
        return "-"
    parts: list[str] = []
    if sub.status_counts.get("blocked", 0):
        parts.append(f"{sub.status_counts['blocked']}!")
    if sub.status_counts.get("working", 0):
        parts.append(f"{sub.status_counts['working']}w")
    if sub.status_counts.get("done", 0):
        parts.append(f"{sub.status_counts['done']}d")
    if sub.status_counts.get("idle", 0):
        parts.append(f"{sub.status_counts['idle']}i")
    if sub.status_counts.get("shell", 0):
        parts.append(f"{sub.status_counts['shell']}s")
    detail = f"({','.join(parts)})" if parts else ""
    return f"+{sub.extra_tab_count}t/{sub.extra_pane_count}p{detail}"


def _extract_field(space: SpaceSnapshot, field: str) -> str:
    pane = space.primary_pane
    mapping = {
        "number": str(space.number),
        "workspace_id": space.workspace_id,
        "label": space.label,
        "attention": space.attention,
        "effective_status": pane.effective_status if pane else "none",
        "agent_kind": pane.agent_kind or "-" if pane else "-",
        "agent_name": pane.agent_name or "-" if pane else "-",
        "primary_tab_id": space.primary_tab_id or "-",
        "pane_id": pane.pane_id if pane else "-",
        "sub_tabs": _sub_tab_badge(space),
        "ticket_key": space.ticket_key or "-",
        "work_item_ids": ",".join(str(i) for i in space.work_item_ids) or "-",
        "cwd": pane.cwd if pane and pane.cwd else "-",
        "focused": "true" if space.focused else "false",
        "is_self": "true" if space.is_self else "false",
        "deleted_hint": "true" if space.deleted_hint else "false",
    }
    return mapping[field]


def _colorize(text: str, status: str, mode: OutputMode) -> str:
    if not mode.color:
        return text
    code = _STATUS_COLOR.get(status)
    if not code:
        return text
    return f"{code}{text}{_ANSI_RESET}"


def format_json_success(data: dict[str, Any]) -> str:
    """Serialize a successful command payload in the standard JSON envelope."""
    return json.dumps(
        {"ok": True, "schema_version": SCHEMA_VERSION, "data": data},
        ensure_ascii=False,
    )


def format_error(err: CliError, mode: OutputMode) -> str:
    """Render an error for stderr in JSON or human-readable form."""
    if mode.fmt == "json":
        return json.dumps({"ok": False, "error": err.to_dict()}, ensure_ascii=False)
    lines = [
        f"Error: {err.code} - {err.message}",
        f"Fix: {err.fix}",
    ]
    return "\n".join(lines)


def format_status(
    spaces: Sequence[SpaceSnapshot],
    mode: OutputMode,
    *,
    include_tabs: bool = False,
    fields: tuple[str, ...] | None = None,
    warnings: Sequence[str] = (),
) -> str:
    """Render `status` output in json, plain, or human format."""
    counts: dict[str, int] = {}
    for space in spaces:
        counts[space.attention] = counts.get(space.attention, 0) + 1

    if mode.fmt == "json":
        rows: list[dict[str, Any]] = []
        for space in spaces:
            full = space.to_dict(include_tabs=include_tabs)
            if fields is not None:
                filtered: dict[str, Any] = {}
                for field in fields:
                    if field in full:
                        filtered[field] = full[field]
                    elif field in {
                        "effective_status",
                        "agent_kind",
                        "agent_name",
                        "pane_id",
                        "cwd",
                    }:
                        pane_dict = full.get("primary_pane") or {}
                        filtered[field] = pane_dict.get(field)
                rows.append(filtered)
            else:
                rows.append(full)
        payload: dict[str, Any] = {
            "count": len(spaces),
            "counts": counts,
            "workspaces": rows,
        }
        if warnings:
            payload["warnings"] = list(warnings)
        return format_json_success(payload)

    if mode.fmt == "plain":
        cols = fields or DEFAULT_PLAIN_FIELDS
        return "\n".join("\t".join(_extract_field(space, col) for col in cols) for space in spaces)

    if not spaces:
        return "No matching Herdr workspaces."

    lines: list[str] = []
    has_tracker = any(space.work_item_ids for space in spaces)
    header_parts = [
        f"{'#':>2}",
        f"{'WS':<5}",
        f"{'ATTN':<8}",
        f"{'AGENT':<9}",
        f"{'SUBTABS':<14}",
    ]
    if has_tracker:
        header_parts.append(f"{'TRACKER':<10}")
    header_parts.extend([f"{'WORKSPACE':<24}", "CWD"])
    header = "  ".join(header_parts)
    lines.append(f"{_ANSI_DIM}{header}{_ANSI_RESET}" if mode.color else header)

    for space in spaces:
        pane = space.primary_pane
        agent_str = pane.agent_kind or "shell" if pane else "-"
        if pane and pane.effective_status == "busy_bg":
            agent_str = f"{agent_str}(bg)"
        attn_text = _colorize(f"{space.attention:<8}", space.attention, mode)
        label_str = f"{space.label}*" if space.is_self else space.label
        cwd_str = _shorten_path(pane.cwd) if pane else "-"
        row_parts = [
            f"{space.number:>2}",
            f"{space.workspace_id:<5}",
            attn_text,
            f"{agent_str:<9}",
            f"{_sub_tab_badge(space):<14}",
        ]
        if has_tracker:
            wt = ",".join(f"#{i}" for i in space.work_item_ids) if space.work_item_ids else "-"
            row_parts.append(f"{wt:<10}")
        row_parts.extend([f"{label_str:<24}", cwd_str])
        lines.append("  ".join(row_parts))

        if include_tabs and len(space.tabs) > 1:
            for tab in space.tabs[1:]:
                lines.append(f"      tab {tab.tab_id} [{tab.agent_status}] {tab.label}")

    summary_bits = [f"{k}={v}" for k, v in counts.items() if v]
    lines.append("")
    lines.append(f"Total: {len(spaces)} ({', '.join(summary_bits)})")
    return "\n".join(lines)


def format_find(
    result: FindResult,
    mode: OutputMode,
    *,
    warnings: Sequence[str] = (),
) -> str:
    """Render `find` output in json, plain, or human format."""
    if mode.fmt == "json":
        payload = result.to_dict()
        if warnings:
            payload["warnings"] = list(warnings)
        return format_json_success(payload)

    if mode.fmt == "plain":
        rows = []
        for match in result.matches:
            space = match.space
            pane = space.primary_pane
            rows.append(
                "\t".join(
                    (
                        space.workspace_id,
                        match.match_strength,
                        ",".join(match.match_reasons),
                        space.attention,
                        pane.agent_kind or "shell" if pane else "-",
                        space.primary_tab_id or "-",
                        pane.pane_id if pane else "-",
                        space.label,
                        pane.cwd if pane and pane.cwd else "-",
                    )
                )
            )
        return "\n".join(rows)

    if not result.found:
        return "No existing Herdr workspace matches the query. Recommendation: create_new."

    lines = [
        f"Found {len(result.matches)} existing workspace(s) "
        f"(recommendation: {result.recommendation}):"
    ]
    has_tracker = any(match.space.work_item_ids for match in result.matches)
    header_parts = [
        f"{'WS':<5}",
        f"{'MATCH':<7}",
        f"{'ATTN':<8}",
        f"{'AGENT':<9}",
        f"{'TAB':<7}",
        f"{'PANE':<7}",
    ]
    if has_tracker:
        header_parts.append(f"{'TRACKER':<10}")
    header_parts.extend([f"{'LABEL':<22}", f"{'CWD':<28}", "REASONS"])
    header = "  ".join(header_parts)
    lines.append(f"{_ANSI_DIM}{header}{_ANSI_RESET}" if mode.color else header)

    for match in result.matches:
        space = match.space
        pane = space.primary_pane
        agent_str = pane.agent_kind or "shell" if pane else "-"
        if pane and pane.effective_status == "busy_bg":
            agent_str = f"{agent_str}(bg)"
        attn_text = _colorize(f"{space.attention:<8}", space.attention, mode)
        label_str = f"{space.label}*" if space.is_self else space.label
        cwd_str = _shorten_path(pane.cwd) if pane else "-"
        row_parts = [
            f"{space.workspace_id:<5}",
            f"{match.match_strength:<7}",
            attn_text,
            f"{agent_str:<9}",
            f"{space.primary_tab_id or '-':<7}",
            f"{pane.pane_id if pane else '-':<7}",
        ]
        if has_tracker:
            wt = ",".join(f"#{i}" for i in space.work_item_ids) if space.work_item_ids else "-"
            row_parts.append(f"{wt:<10}")
        row_parts.extend([f"{label_str:<22}", f"{cwd_str:<28}", ",".join(match.match_reasons)])
        lines.append("  ".join(row_parts))

    lines.append("")
    lines.append(
        "Tip: Ask the user whether to reuse an existing workspace above before creating a new one."
    )
    return "\n".join(lines)


def format_peek(result: PeekResult, mode: OutputMode) -> str:
    """Render `peek` output."""
    if mode.fmt == "json":
        return format_json_success(result.to_dict())
    if mode.fmt == "plain":
        return result.text.rstrip("\n")
    header = (
        f"[{result.workspace_id} {result.label} | {result.pane_id} "
        f"| {result.agent_kind or 'shell'}:{result.agent_status} | {result.source}]"
    )
    body = result.text.rstrip("\n")
    return f"{header}\n{body}" if body else f"{header}\n(empty)"


def format_wait(result: WaitResult, mode: OutputMode) -> str:
    """Render `wait` output."""
    if mode.fmt == "json":
        return format_json_success(result.to_dict())
    if mode.fmt == "plain":
        return "\n".join(
            "\t".join(
                (
                    s.workspace_id,
                    s.attention,
                    s.primary_pane.pane_id if s.primary_pane else "-",
                    s.label,
                )
            )
            for s in result.matched
        )
    lines = [
        f"Matched {len(result.matched)} workspace(s) for [{', '.join(result.until)}] "
        f"in {result.elapsed_seconds:.2f}s:"
    ]
    for s in result.matched:
        pid = s.primary_pane.pane_id if s.primary_pane else "-"
        lines.append(f"  {s.workspace_id}  {s.attention:<8}  {pid}  {s.label}")
    return "\n".join(lines)


def format_ensure(result: EnsureResult, mode: OutputMode) -> str:
    """Render `ensure` output."""
    if mode.fmt == "json":
        return format_json_success(result.to_dict())
    if mode.fmt == "plain":
        return "\t".join(
            (
                "dry-run" if result.dry_run else "applied",
                result.workspace_action,
                result.agent_action,
                result.workspace_id or "-",
                result.pane_id or "-",
                result.launcher,
                result.label,
                result.cwd or "-",
            )
        )
    prefix = "[dry-run] " if result.dry_run else ""
    return (
        f"{prefix}{result.label} ({result.workspace_id or 'new'}): "
        f"workspace={result.workspace_action}, agent={result.agent_action} ({result.launcher}), "
        f"pane={result.pane_id or 'new'}, cwd={result.cwd or '-'}"
    )


def format_send(result: SendResult, mode: OutputMode) -> str:
    """Render `send` output."""
    if mode.fmt == "json":
        return format_json_success(result.to_dict())
    if mode.fmt == "plain":
        return "\t".join(
            (
                "dry-run" if result.dry_run else "sent",
                result.workspace_id,
                result.pane_id,
                result.agent_kind,
                result.settled_status or result.agent_status,
                str(result.prompt_chars),
            )
        )
    prefix = "[dry-run] " if result.dry_run else ""
    status_info = f" -> {result.settled_status}" if result.settled_status else ""
    return (
        f"{prefix}Prompt ({result.prompt_chars} chars) -> "
        f"{result.label} ({result.workspace_id}, {result.pane_id}, {result.agent_kind})"
        f"{status_info}"
    )


def format_tab_list(
    caller: CallerContext,
    tabs: Sequence[WorkspaceTabInfo],
    mode: OutputMode,
) -> str:
    """Render `tab list` output."""
    if mode.fmt == "json":
        return format_json_success(
            {
                "workspace_id": caller.workspace_id,
                "caller": caller.to_dict(),
                "count": len(tabs),
                "sub_tab_count": max(0, len(tabs) - 1),
                "tabs": [tab.to_dict(include_panes=True) for tab in tabs],
            }
        )
    if mode.fmt == "plain":
        rows = []
        for tab in tabs:
            pane = tab.primary_pane
            rows.append(
                "\t".join(
                    (
                        str(tab.index + 1),
                        tab.tab_id,
                        "lead" if tab.is_primary else "worker",
                        tab.agent_status,
                        pane.agent_kind or "shell" if pane else "-",
                        pane.pane_id if pane else "-",
                        str(len(tab.panes)),
                        tab.base_label,
                    )
                )
            )
        return "\n".join(rows)

    lines = [
        f"Workspace {caller.workspace_id} | caller={caller.pane_id} ({caller.role})",
        f"{'#':>2}  {'TAB':<7}  {'ROLE':<6}  {'STATUS':<8}  {'AGENT':<8}  {'PANES':>5}  LABEL",
    ]
    for tab in tabs:
        pane = tab.primary_pane
        role = "lead" if tab.is_primary else "worker"
        agent = pane.agent_kind or "shell" if pane else "-"
        if pane and pane.effective_status == "busy_bg":
            agent = f"{agent}(bg)"
        status_str = _colorize(f"{tab.agent_status:<8}", tab.agent_status, mode)
        self_mark = "*" if tab.is_self else ""
        lines.append(
            f"{tab.index + 1:>2}  {tab.tab_id:<7}  {role:<6}  {status_str}  "
            f"{agent:<8}  {len(tab.panes):>5}  {tab.base_label}{self_mark}"
        )
    return "\n".join(lines)


def format_tab_spawn(result: TabSpawnResult, mode: OutputMode) -> str:
    """Render `tab spawn` output."""
    if mode.fmt == "json":
        return format_json_success(result.to_dict())
    if mode.fmt == "plain":
        return "\t".join(
            (
                "dry-run" if result.dry_run else "spawned",
                result.workspace_id,
                result.tab_id or "-",
                result.pane_id or "-",
                result.launcher,
                result.agent_kind or "shell",
                result.label,
            )
        )
    prefix = "[dry-run] " if result.dry_run else ""
    tab_desc = f"{result.tab_id or 'new'}, pane={result.pane_id or 'new'}"
    return (
        f"{prefix}Sub-tab {result.label} ({tab_desc}): "
        f"launcher={result.launcher}, agent={result.agent_kind or 'shell'}"
        f"{' (prompted)' if result.prompt_sent else ''}"
    )


def format_tab_wait(result: TabWaitResult, mode: OutputMode) -> str:
    """Render `tab wait` output."""
    if mode.fmt == "json":
        return format_json_success(result.to_dict())
    if mode.fmt == "plain":
        return "\n".join(
            "\t".join(
                (
                    tab.tab_id,
                    tab.agent_status,
                    tab.primary_pane.pane_id if tab.primary_pane else "-",
                    tab.base_label,
                )
            )
            for tab in result.matched
        )
    lines = [
        f"Matched {len(result.matched)} tab(s) in {result.workspace_id} for "
        f"[{', '.join(result.until)}] in {result.elapsed_seconds:.2f}s:"
    ]
    for tab in result.matched:
        pid = tab.primary_pane.pane_id if tab.primary_pane else "-"
        lines.append(f"  {tab.tab_id}  {tab.agent_status:<8}  {pid}  {tab.base_label}")
    return "\n".join(lines)


def format_tab_close(result: TabCloseResult, mode: OutputMode) -> str:
    """Render `tab close` output."""
    if mode.fmt == "json":
        return format_json_success(result.to_dict())
    if mode.fmt == "plain":
        return "\t".join(("dry-run" if result.dry_run else "closed", result.tab_id, result.label))
    prefix = "[dry-run] " if result.dry_run else ""
    return f"{prefix}Closed sub-tab {result.tab_id} ({result.label})"


def format_pane_list(
    caller: CallerContext,
    tab: WorkspaceTabInfo,
    panes: Sequence[PaneInfo],
    mode: OutputMode,
) -> str:
    """Render `pane list` output."""
    if mode.fmt == "json":
        return format_json_success(
            {
                "workspace_id": caller.workspace_id,
                "tab_id": tab.tab_id,
                "tab_label": tab.base_label,
                "caller": caller.to_dict(),
                "count": len(panes),
                "worker_pane_count": max(0, len(panes) - 1),
                "panes": [pane.to_dict() for pane in panes],
            }
        )
    if mode.fmt == "plain":
        return "\n".join(
            "\t".join(
                (
                    str(pane.index + 1),
                    pane.pane_id,
                    "lead" if pane.is_primary else "worker",
                    pane.effective_status,
                    pane.agent_kind or "shell",
                    pane.agent_name or "-",
                    pane.cwd or "-",
                )
            )
            for pane in panes
        )
    lines = [
        f"Tab {tab.tab_id} ({tab.base_label}) | caller={caller.pane_id} ({caller.role})",
        f"{'#':>2}  {'PANE':<8}  {'ROLE':<6}  {'STATUS':<8}  {'AGENT':<8}  {'NAME':<16}  CWD",
    ]
    for pane in panes:
        role = "lead" if pane.is_primary else "worker"
        agent = pane.agent_kind or "shell"
        if pane.effective_status == "busy_bg":
            agent = f"{agent}(bg)"
        status_str = _colorize(f"{pane.effective_status:<8}", pane.effective_status, mode)
        self_mark = "*" if pane.is_self else ""
        lines.append(
            f"{pane.index + 1:>2}  {pane.pane_id:<8}  {role:<6}  {status_str}  "
            f"{agent:<8}  {(pane.agent_name or '-') + self_mark:<16}  {_shorten_path(pane.cwd)}"
        )
    return "\n".join(lines)


def format_pane_spawn(result: PaneSpawnResult, mode: OutputMode) -> str:
    """Render `pane spawn` output."""
    if mode.fmt == "json":
        return format_json_success(result.to_dict())
    if mode.fmt == "plain":
        return "\t".join(
            (
                "dry-run" if result.dry_run else "spawned",
                result.tab_id,
                result.pane_id or "-",
                result.direction,
                result.launcher,
                result.agent_kind or "shell",
            )
        )
    prefix = "[dry-run] " if result.dry_run else ""
    return (
        f"{prefix}Worker pane {result.pane_id or 'new'} in {result.tab_id} "
        f"({result.direction} from {result.split_target_pane_id}): "
        f"launcher={result.launcher}, agent={result.agent_kind or 'shell'}"
        f"{' (prompted)' if result.prompt_sent else ''}"
    )


def format_pane_wait(result: PaneWaitResult, mode: OutputMode) -> str:
    """Render `pane wait` output."""
    if mode.fmt == "json":
        return format_json_success(result.to_dict())
    if mode.fmt == "plain":
        return "\n".join(
            "\t".join((pane.pane_id, pane.effective_status, pane.agent_kind or "shell"))
            for pane in result.matched
        )
    lines = [
        f"Matched {len(result.matched)} pane(s) in {result.tab_id} for "
        f"[{', '.join(result.until)}] in {result.elapsed_seconds:.2f}s:"
    ]
    for pane in result.matched:
        lines.append(f"  {pane.pane_id}  {pane.effective_status:<8}  {pane.agent_kind or 'shell'}")
    return "\n".join(lines)


def format_pane_close(result: PaneCloseResult, mode: OutputMode) -> str:
    """Render `pane close` output."""
    if mode.fmt == "json":
        return format_json_success(result.to_dict())
    if mode.fmt == "plain":
        return "\t".join(("dry-run" if result.dry_run else "closed", result.tab_id, result.pane_id))
    prefix = "[dry-run] " if result.dry_run else ""
    return f"{prefix}Closed worker pane {result.pane_id} in {result.tab_id}"
