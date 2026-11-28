"""Domain models, constants, and structured errors for herdr-commander."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_DOMAIN = 1
EXIT_USAGE = 2
EXIT_TEMP = 75
EXIT_CONFIG = 78
EXIT_SIGINT = 130

ATTENTION_ORDER: dict[str, int] = {
    "blocked": 0,
    "done": 1,
    "shell": 2,
    "idle": 3,
    "working": 4,
    "unknown": 5,
}

VALID_SORT_MODES = ("attention", "number")
VALID_WAIT_STATES = frozenset(ATTENTION_ORDER.keys()) | {"busy_bg"}
DEFAULT_WAIT_STATES = ("blocked", "done")

DEFAULT_MAX_SUB_TABS = 8
DEFAULT_MAX_WORKER_PANES = 3
DEFAULT_MAX_SPACE_AGENTS = 4
DEFAULT_MAX_SESSION_AGENTS = 10

DEFAULT_MAX_PEEK_CHARS = 1500
DEFAULT_ROTATE_CTX = 30
DEFAULT_ROTATE_COOLDOWN = 300.0

VALID_SPLIT_DIRECTIONS = ("auto", "right", "down")
VALID_ON_EXISTING_POLICIES = ("reuse", "fail", "new")


class CliError(Exception):
    """Structured error raised across domain boundaries and formatted by cli.py."""

    def __init__(
        self,
        code: str,
        message: str,
        fix: str,
        *,
        exit_code: int = EXIT_DOMAIN,
        transient: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix
        self.exit_code = exit_code
        self.transient = transient
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "fix": self.fix,
            "transient": self.transient,
        }
        if self.details is not None:
            payload["details"] = self.details
        return payload


@dataclass(frozen=True)
class PrimaryPane:
    """Representative pane of a workspace's first tab."""

    pane_id: str
    cwd: str
    foreground_cwd: str
    cwd_exists: bool
    agent_kind: str | None
    agent_name: str | None
    raw_status: str
    effective_status: str
    interactive_ready: bool
    session_id: str | None
    revision: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TabSummary:
    """Compact metadata for one tab inside a workspace."""

    tab_id: str
    label: str
    number: int
    pane_count: int
    agent_status: str
    is_primary: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SubTabRollup:
    """Aggregated status of non-primary tabs and non-primary split panes."""

    extra_tab_count: int
    extra_pane_count: int
    status_counts: dict[str, int]
    any_blocked: bool
    any_working: bool
    blocked_panes: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SpaceSnapshot:
    """First-tab-centric summary of a single Herdr workspace."""

    workspace_id: str
    label: str
    number: int
    focused: bool
    is_self: bool
    deleted_hint: bool
    primary_tab_id: str | None
    primary_tab_label: str | None
    primary_pane: PrimaryPane | None
    sub_tabs: SubTabRollup
    attention: str
    ticket_key: str | None
    work_item_ids: tuple[int, ...]
    tabs: tuple[TabSummary, ...]

    def to_dict(self, *, include_tabs: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "workspace_id": self.workspace_id,
            "label": self.label,
            "number": self.number,
            "focused": self.focused,
            "is_self": self.is_self,
            "deleted_hint": self.deleted_hint,
            "primary_tab_id": self.primary_tab_id,
            "primary_tab_label": self.primary_tab_label,
            "primary_pane": self.primary_pane.to_dict() if self.primary_pane else None,
            "sub_tabs": self.sub_tabs.to_dict(),
            "attention": self.attention,
            "ticket_key": self.ticket_key,
            "work_item_ids": list(self.work_item_ids),
        }
        if include_tabs:
            data["tabs"] = [tab.to_dict() for tab in self.tabs]
        return data


@dataclass(frozen=True)
class PeekResult:
    """Output retrieved from a workspace's first-tab pane."""

    workspace_id: str
    label: str
    tab_id: str
    pane_id: str
    agent_kind: str | None
    agent_status: str
    source: str
    lines: int
    text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DigestPeek:
    """One terminal snippet attached to a workspace digest entry."""

    tab_id: str
    tab_label: str
    pane_id: str
    agent_kind: str | None
    agent_status: str
    is_primary: bool
    lines: int
    text: str
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "tab_id": self.tab_id,
            "tab_label": self.tab_label,
            "pane_id": self.pane_id,
            "agent_kind": self.agent_kind,
            "agent_status": self.agent_status,
            "is_primary": self.is_primary,
            "lines": self.lines,
            "text": self.text,
        }
        if self.truncated:
            data["truncated"] = True
        return data


@dataclass(frozen=True)
class DigestCursorEntry:
    """Persisted watermark state for one workspace across `hcmd digest --since-last` calls."""

    workspace_id: str
    label: str
    primary_pane_id: str | None
    agent_kind: str | None
    raw_status: str
    effective_status: str
    attention: str
    revision: int
    peek_hash: str | None = None
    blocked_subtabs: tuple[tuple[str, int, str | None], ...] = ()
    deleted_hint: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "label": self.label,
            "primary_pane_id": self.primary_pane_id,
            "agent_kind": self.agent_kind,
            "raw_status": self.raw_status,
            "effective_status": self.effective_status,
            "attention": self.attention,
            "revision": self.revision,
            "peek_hash": self.peek_hash,
            "blocked_subtabs": [
                {"pane_id": pid, "revision": rev, "peek_hash": ph}
                for pid, rev, ph in self.blocked_subtabs
            ],
            "deleted_hint": self.deleted_hint,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DigestCursorEntry:
        subtabs: list[tuple[str, int, str | None]] = []
        raw_subs = raw.get("blocked_subtabs")
        if isinstance(raw_subs, list):
            for item in raw_subs:
                if isinstance(item, dict) and isinstance(item.get("pane_id"), str):
                    rev = int(item.get("revision") or 0)
                    ph = item.get("peek_hash")
                    subtabs.append((item["pane_id"], rev, str(ph) if isinstance(ph, str) else None))
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    pid = str(item[0])
                    rev = int(item[1] or 0)
                    ph = str(item[2]) if len(item) >= 3 and isinstance(item[2], str) else None
                    subtabs.append((pid, rev, ph))
        subtabs.sort(key=lambda t: t[0])
        return cls(
            workspace_id=str(raw.get("workspace_id") or ""),
            label=str(raw.get("label") or ""),
            primary_pane_id=(
                str(raw["primary_pane_id"]) if isinstance(raw.get("primary_pane_id"), str) else None
            ),
            agent_kind=(str(raw["agent_kind"]) if isinstance(raw.get("agent_kind"), str) else None),
            raw_status=str(raw.get("raw_status") or "unknown"),
            effective_status=str(raw.get("effective_status") or "unknown"),
            attention=str(raw.get("attention") or "unknown"),
            revision=int(raw.get("revision") or 0),
            peek_hash=str(raw["peek_hash"]) if isinstance(raw.get("peek_hash"), str) else None,
            blocked_subtabs=tuple(subtabs),
            deleted_hint=bool(raw.get("deleted_hint", False)),
        )


@dataclass(frozen=True)
class DigestCursor:
    """Persisted watermark cursor across workspaces for a named or scoped digest slot."""

    schema_version: int = 1
    scope: str = ""
    updated_at: float = 0.0
    workspaces: dict[str, DigestCursorEntry] | None = None

    def entries_map(self) -> dict[str, DigestCursorEntry]:
        return self.workspaces or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scope": self.scope,
            "updated_at": round(self.updated_at, 3),
            "workspaces": {
                wid: entry.to_dict() for wid, entry in sorted(self.entries_map().items())
            },
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DigestCursor:
        ws_map: dict[str, DigestCursorEntry] = {}
        raw_ws = raw.get("workspaces")
        if isinstance(raw_ws, dict):
            for wid, entry_raw in raw_ws.items():
                if isinstance(wid, str) and isinstance(entry_raw, dict):
                    entry = DigestCursorEntry.from_dict({**entry_raw, "workspace_id": wid})
                    ws_map[wid] = entry
        return cls(
            schema_version=int(raw.get("schema_version") or 1),
            scope=str(raw.get("scope") or ""),
            updated_at=float(raw.get("updated_at") or 0.0),
            workspaces=ws_map,
        )


@dataclass(frozen=True)
class DigestEntry:
    """One workspace included in an async `hcmd digest` report."""

    space: SpaceSnapshot
    needs_attention: bool
    peeks: tuple[DigestPeek, ...] = ()
    change_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "needs_attention": self.needs_attention,
            "workspace": self.space.to_dict(include_tabs=False),
            "peeks": [peek.to_dict() for peek in self.peeks],
        }
        if self.change_reason is not None:
            data["change_reason"] = self.change_reason
        return data

    def to_compact_dict(self) -> dict[str, Any]:
        pane = self.space.primary_pane
        compact_ws: dict[str, Any] = {
            "workspace_id": self.space.workspace_id,
            "label": self.space.label,
            "attention": self.space.attention,
            "effective_status": pane.effective_status if pane else None,
            "agent_kind": pane.agent_kind if pane else None,
            "primary_tab_id": self.space.primary_tab_id,
            "pane_id": pane.pane_id if pane else None,
            "sub_tabs": {
                "extra_tab_count": self.space.sub_tabs.extra_tab_count,
                "extra_pane_count": self.space.sub_tabs.extra_pane_count,
                "status_counts": self.space.sub_tabs.status_counts,
                "any_blocked": self.space.sub_tabs.any_blocked,
                "any_working": self.space.sub_tabs.any_working,
            },
            "ticket_key": self.space.ticket_key,
            "work_item_ids": list(self.space.work_item_ids),
            "deleted_hint": self.space.deleted_hint,
        }
        data: dict[str, Any] = {
            "needs_attention": self.needs_attention,
            "workspace": compact_ws,
            "peeks": [peek.to_dict() for peek in self.peeks],
        }
        if self.change_reason is not None:
            data["change_reason"] = self.change_reason
        return data


@dataclass(frozen=True)
class DigestResult:
    """Non-blocking status summary and harvested output snippets across Spaces."""

    total_workspaces: int
    counts: dict[str, int]
    sub_tabs_blocked_count: int
    entries: tuple[DigestEntry, ...]
    peek_lines: int
    include_all: bool
    has_active_work: bool = False
    since_last: bool = False
    changed: bool | None = None
    compact: bool = False
    cursor_scope: str | None = None
    still_blocked: tuple[dict[str, str], ...] = ()
    closed_workspaces: tuple[dict[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        entries_payload = (
            [entry.to_compact_dict() for entry in self.entries]
            if self.compact
            else [entry.to_dict() for entry in self.entries]
        )
        data: dict[str, Any] = {
            "total_workspaces": self.total_workspaces,
            "count": len(self.entries),
            "counts": self.counts,
            "sub_tabs_blocked_count": self.sub_tabs_blocked_count,
            "has_active_work": self.has_active_work,
            "peek_lines": self.peek_lines,
            "include_all": self.include_all,
            "entries": entries_payload,
        }
        if self.compact:
            data["compact"] = True
        if self.since_last:
            data["since_last"] = True
            data["changed"] = self.changed
            data["cursor_scope"] = self.cursor_scope
            data["still_blocked"] = list(self.still_blocked)
            data["closed_workspaces"] = list(self.closed_workspaces)
        return data


@dataclass(frozen=True)
class WaitResult:
    """Result of waiting for watched first-tab agents to enter target states."""

    until: tuple[str, ...]
    matched: tuple[SpaceSnapshot, ...]
    elapsed_seconds: float
    from_initial_snapshot: bool
    reason: str = "matched"  # "matched" | "changed" | "timeout" | "no_active_work"
    timed_out: bool = False
    digest: DigestResult | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "until": list(self.until),
            "matched": [space.to_dict() for space in self.matched],
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "from_initial_snapshot": self.from_initial_snapshot,
            "reason": self.reason,
            "timed_out": self.timed_out,
        }
        if self.digest is not None:
            data["digest"] = self.digest.to_dict()
        return data


@dataclass(frozen=True)
class SelfContextResult:
    """Parsed context-window telemetry from the calling pane's statusline."""

    workspace_id: str
    tab_id: str
    pane_id: str
    agent_kind: str | None
    source: str
    parsed: bool
    model_display: str | None
    ctx_percent: int | None
    tokens_used: int | None
    tokens_limit: int | None
    tokens_display: str | None
    rate_limits: dict[str, Any]
    statusline: str | None
    rotate_ctx: int | None = None
    should_rotate: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RotateResult:
    """Result of initiating or acknowledging a 2-phase same-tab pane rotation."""

    mode: str  # "rotate" | "ack"
    dry_run: bool
    workspace_id: str
    tab_id: str
    old_pane_id: str
    new_pane_id: str | None
    launcher: str | None
    agent_kind: str | None
    agent_status: str | None
    generation: int
    status: str  # "dry_run" | "pending_ack" | "completed"
    handoff_json_path: str | None
    handoff_md_path: str | None
    old_pane_closed: bool = False
    prompt_sent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EnsurePlan:
    """Pure execution plan produced before mutating Herdr or Git worktrees."""

    workspace_action: str
    agent_action: str
    launcher: str
    expected_agent: str | None
    label: str
    cwd: str | None
    ticket: str | None
    pr_number: str | None
    base_ref: str | None
    existing_workspace_id: str | None
    existing_pane_id: str | None
    agent_name: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EnsureResult:
    """Result of planning or executing idempotent workspace/agent convergence."""

    dry_run: bool
    workspace_action: str
    agent_action: str
    launcher: str
    workspace_id: str | None
    label: str
    tab_id: str | None
    pane_id: str | None
    cwd: str | None
    agent_kind: str | None
    agent_status: str | None
    agent_name: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SendResult:
    """Result of validating and dispatching a prompt to a first-tab agent."""

    dry_run: bool
    workspace_id: str
    label: str
    pane_id: str
    agent_kind: str
    agent_status: str
    waited: bool
    settled_status: str | None
    prompt_chars: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CallerContext:
    """Resolved Herdr hierarchy role of the calling pane."""

    workspace_id: str
    tab_id: str
    pane_id: str
    tab_index: int
    pane_index: int
    role: str  # "space_lead" | "tab_lead" | "pane_worker"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PaneInfo:
    """Detailed state of one pane inside a workspace tab."""

    pane_id: str
    tab_id: str
    workspace_id: str
    index: int
    is_primary: bool
    is_self: bool
    cwd: str
    foreground_cwd: str
    cwd_exists: bool
    agent_kind: str | None
    agent_name: str | None
    raw_status: str
    effective_status: str
    interactive_ready: bool
    session_id: str | None
    revision: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkspaceTabInfo:
    """Detailed state of one tab and its panes inside a workspace."""

    tab_id: str
    workspace_id: str
    label: str
    base_label: str
    index: int
    number: int
    is_primary: bool
    is_self: bool
    pane_count: int
    agent_status: str
    primary_pane: PaneInfo | None
    panes: tuple[PaneInfo, ...]

    def to_dict(self, *, include_panes: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "tab_id": self.tab_id,
            "workspace_id": self.workspace_id,
            "label": self.label,
            "base_label": self.base_label,
            "index": self.index,
            "number": self.number,
            "is_primary": self.is_primary,
            "is_self": self.is_self,
            "pane_count": self.pane_count,
            "agent_status": self.agent_status,
            "primary_pane": self.primary_pane.to_dict() if self.primary_pane else None,
        }
        if include_panes:
            data["panes"] = [pane.to_dict() for pane in self.panes]
        return data


@dataclass(frozen=True)
class TabSpawnPlan:
    """Validated plan for creating a worker sub-tab inside the caller's Space."""

    workspace_id: str
    label: str
    cwd: str
    launcher: str
    expected_agent: str | None
    agent_name: str | None
    prompt: str | None
    current_sub_tabs: int
    max_sub_tabs: int
    space_agents: int
    max_space_agents: int
    session_working_agents: int
    max_session_agents: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TabSpawnResult:
    """Result of planning or creating a sub-tab and optional worker agent."""

    dry_run: bool
    workspace_id: str
    tab_id: str | None
    pane_id: str | None
    label: str
    cwd: str
    launcher: str
    agent_kind: str | None
    agent_status: str | None
    agent_name: str | None
    prompt_sent: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TabWaitResult:
    """Result of waiting for sub-tabs in the caller's Space to enter target states."""

    workspace_id: str
    until: tuple[str, ...]
    matched: tuple[WorkspaceTabInfo, ...]
    elapsed_seconds: float
    from_initial_snapshot: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "until": list(self.until),
            "matched": [tab.to_dict(include_panes=False) for tab in self.matched],
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "from_initial_snapshot": self.from_initial_snapshot,
        }


@dataclass(frozen=True)
class TabCloseResult:
    """Result of closing a non-primary sub-tab in the caller's Space."""

    dry_run: bool
    workspace_id: str
    tab_id: str
    label: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PaneSpawnPlan:
    """Validated plan for splitting a worker pane inside the caller's tab."""

    workspace_id: str
    tab_id: str
    split_target_pane_id: str
    direction: str
    cwd: str
    launcher: str
    expected_agent: str | None
    agent_name: str | None
    shell_cmd: str | None
    prompt: str | None
    current_worker_panes: int
    max_worker_panes: int
    space_agents: int
    max_space_agents: int
    session_working_agents: int
    max_session_agents: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PaneSpawnResult:
    """Result of planning or splitting a worker pane in the caller's tab."""

    dry_run: bool
    workspace_id: str
    tab_id: str
    pane_id: str | None
    split_target_pane_id: str
    direction: str
    cwd: str
    launcher: str
    agent_kind: str | None
    agent_status: str | None
    agent_name: str | None
    shell_cmd: str | None
    prompt_sent: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PaneWaitResult:
    """Result of waiting for worker panes in the caller's tab to enter target states."""

    workspace_id: str
    tab_id: str
    until: tuple[str, ...]
    matched: tuple[PaneInfo, ...]
    elapsed_seconds: float
    from_initial_snapshot: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "tab_id": self.tab_id,
            "until": list(self.until),
            "matched": [pane.to_dict() for pane in self.matched],
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "from_initial_snapshot": self.from_initial_snapshot,
        }


@dataclass(frozen=True)
class PaneCloseResult:
    """Result of closing a non-primary worker pane in the caller's tab."""

    dry_run: bool
    workspace_id: str
    tab_id: str
    pane_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkspaceMatch:
    """One existing Herdr workspace matched during a pre-launch lookup."""

    space: SpaceSnapshot
    match_reasons: tuple[str, ...]
    match_strength: str  # "exact" | "partial"
    matched_panes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "match_strength": self.match_strength,
            "match_reasons": list(self.match_reasons),
            "matched_panes": list(self.matched_panes),
            "workspace": self.space.to_dict(include_tabs=False),
        }


@dataclass(frozen=True)
class FindQuery:
    """Normalized parameters used for a pre-launch workspace lookup."""

    query: str | None
    name: str | None
    label: str | None
    cwd: str | None
    ticket: str | None
    pr_number: str | None
    include_self: bool = False
    include_deleted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FindResult:
    """Result of checking whether matching Herdr workspaces already exist."""

    query: FindQuery
    found: bool
    recommendation: str  # "create_new" | "reuse_empty_shell" | "ask_reuse" | "warn_busy"
    matches: tuple[WorkspaceMatch, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "count": len(self.matches),
            "recommendation": self.recommendation,
            "query": self.query.to_dict(),
            "matches": [match.to_dict() for match in self.matches],
        }
