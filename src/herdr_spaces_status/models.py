"""Domain models, constants, and structured errors for herdr-spaces-status."""

from __future__ import annotations

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
class WaitResult:
    """Result of waiting for watched first-tab agents to enter target states."""

    until: tuple[str, ...]
    matched: tuple[SpaceSnapshot, ...]
    elapsed_seconds: float
    from_initial_snapshot: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "until": list(self.until),
            "matched": [space.to_dict() for space in self.matched],
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "from_initial_snapshot": self.from_initial_snapshot,
        }


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
