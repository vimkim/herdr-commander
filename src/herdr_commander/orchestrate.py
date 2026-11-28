"""Pure orchestration planning and execution for ensure, send, and wait."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from .models import (
    DEFAULT_MAX_PEEK_CHARS,
    DEFAULT_MAX_SESSION_AGENTS,
    DEFAULT_MAX_SPACE_AGENTS,
    DEFAULT_MAX_SUB_TABS,
    DEFAULT_MAX_WORKER_PANES,
    DEFAULT_ROTATE_COOLDOWN,
    DEFAULT_ROTATE_CTX,
    EXIT_DOMAIN,
    EXIT_USAGE,
    VALID_ON_EXISTING_POLICIES,
    VALID_SPLIT_DIRECTIONS,
    CallerContext,
    CliError,
    DigestCursor,
    DigestCursorEntry,
    DigestEntry,
    DigestPeek,
    DigestResult,
    EnsurePlan,
    EnsureResult,
    PaneCloseResult,
    PaneInfo,
    PaneSpawnPlan,
    PaneSpawnResult,
    PaneWaitResult,
    RotateResult,
    SendResult,
    SpaceSnapshot,
    TabCloseResult,
    TabSpawnPlan,
    TabSpawnResult,
    TabWaitResult,
    WaitResult,
    WorkspaceTabInfo,
)
from .snapshot import (
    collect_live_agent_names,
    compute_find_recommendation,
    correlate_work_tracker,
    count_session_working_agents,
    count_workspace_non_primary_agents,
    find_matching_spaces,
    has_active_work,
    is_primary_exact_match,
    parse_snapshot,
    parse_workspace_tabs,
    resolve_pane_target,
    resolve_tab_target,
    resolve_target,
    strip_ansi,
    strip_tab_decorations,
    verify_claude_busy,
    verify_panes_claude_busy,
    verify_subtabs_claude_busy,
)

_CLAUDE_ENV_VARS = ("CLAUDE_ACCOUNTS_DIR", "CLAUDE_CONFIG_DIR", "XDG_STATE_HOME")

_STATUSLINE_FOOTER_RE = re.compile(
    r"│.*(?:\bctx\b|(?:gemini-)?5h\s+\d+%|\b7d\s+\d+%|\bLow allowance\b)",
    re.IGNORECASE,
)
_BOX_RULE_LINE_RE = re.compile(r"^\s*[─━═-]{3,}\s*$")
_EMPTY_PROMPT_LINE_RE = re.compile(r"^\s*[>❯]\s*$")
_REPORT_STATUS_TAIL_RE = re.compile(
    r"^\s*[-*]?\s*Status:\s*(?:done|blocked)\b",
    re.IGNORECASE,
)
_OVERLAY_PATTERNS = (
    re.compile(r"\besc\s+to\s+dismiss\b", re.IGNORECASE),
    re.compile(r"\bpress\s+esc(?:ape)?\s+to\s+(?:dismiss|close|continue)\b", re.IGNORECASE),
    re.compile(r"\bintroducing\s+gemini\b", re.IGNORECASE),
)


class SnapshotClient(Protocol):
    """Minimal Herdr client interface for snapshot-driven orchestration."""

    def snapshot(self) -> dict[str, Any]: ...


class WaitClient(SnapshotClient, Protocol):
    """Herdr client interface for event-driven waiting and Claude verification."""

    def read_detection(self, pane_id: str) -> str: ...

    def subscribe_events(
        self,
        pane_ids: Sequence[str],
        *,
        deadline: float,
    ) -> Iterator[dict[str, Any]]: ...


class CreateCwdFn(Protocol):
    """Callback matching `herdr_client.create_workspace_cwd`."""

    def __call__(self, label: str, cwd: str) -> dict[str, Any]: ...


class CreateTicketFn(Protocol):
    """Callback matching `herdr_client.create_workspace_cubrid_ticket`."""

    def __call__(self, ticket: str, *, base: str | None = None) -> dict[str, Any]: ...


class CreatePrFn(Protocol):
    """Callback matching `herdr_client.create_workspace_cubrid_pr`."""

    def __call__(self, pr_number: str) -> dict[str, Any]: ...


class RunPaneFn(Protocol):
    """Callback matching `herdr_client.pane_run`."""

    def __call__(self, pane_id: str, command: str) -> None: ...


class StartAgentFn(Protocol):
    """Callback matching `herdr_client.agent_start`."""

    def __call__(
        self,
        name: str,
        kind: str,
        pane_id: str,
        extra_args: Sequence[str],
        *,
        timeout_ms: int = 45000,
    ) -> dict[str, Any]: ...


class SelectBackendFn(Protocol):
    """Callback matching `herdr_client.ai_agent_select_backend`."""

    def __call__(self) -> str: ...


class PromptAgentFn(Protocol):
    """Callback matching `herdr_client.agent_prompt`."""

    def __call__(
        self,
        pane_id: str,
        prompt: str,
        *,
        wait: bool = False,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]: ...


class CreateTabFn(Protocol):
    """Callback matching `herdr_client.tab_create`."""

    def __call__(
        self,
        workspace_id: str,
        *,
        label: str,
        cwd: str,
        env_vars: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...


class CloseTabFn(Protocol):
    """Callback matching `herdr_client.tab_close`."""

    def __call__(self, tab_id: str) -> None: ...


class SplitPaneFn(Protocol):
    """Callback matching `herdr_client.pane_split`."""

    def __call__(
        self,
        target_pane_id: str,
        *,
        direction: str,
        cwd: str,
        env_vars: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...


class ClosePaneFn(Protocol):
    """Callback matching `herdr_client.pane_close`."""

    def __call__(self, pane_id: str) -> None: ...


class ReadPaneFn(Protocol):
    """Callback matching `HerdrSocketClient.read_pane`."""

    def __call__(
        self,
        pane_id: str,
        *,
        has_agent: bool,
        source: str = "recent-unwrapped",
        lines: int = 40,
    ) -> dict[str, Any]: ...


class SendKeysFn(Protocol):
    """Callback matching `herdr_client.agent_send_keys`."""

    def __call__(self, pane_id: str, keys: Sequence[str]) -> dict[str, Any]: ...


class TrustWorkspaceFn(Protocol):
    """Callback matching `herdr_client.ensure_trusted_workspace`."""

    def __call__(
        self,
        cwd: str,
        agent_kinds: Sequence[str],
        env: Mapping[str, str],
    ) -> Sequence[str]: ...


@dataclass(frozen=True)
class LauncherSpec:
    """Definition of a supported first-tab agent or shell launcher.

    `expected_agents` lists every Herdr agent kind the launcher may start; an
    empty tuple means a plain shell. `selects_backend` marks launchers such as
    `ai-agent` that choose their backend at launch time and support a
    `--dry-run --json` preflight.
    """

    name: str
    expected_agents: tuple[str, ...]
    mode: str  # "pane_run" | "agent_start" | "none"
    command: str | None = None
    agent_kind: str | None = None
    forward_claude_env: bool = False
    forward_env: tuple[str, ...] = ()
    selects_backend: bool = False

    @property
    def launches_agent(self) -> bool:
        return bool(self.expected_agents)

    def accepts(self, agent_kind: str | None) -> bool:
        return agent_kind is not None and agent_kind in self.expected_agents

    @property
    def expected_label(self) -> str:
        return " or ".join(repr(kind) for kind in self.expected_agents)


# Backend names reported by `ai-agent --dry-run --json` mapped to Herdr agent kinds.
AI_AGENT_BACKEND_AGENTS: dict[str, str] = {"ay": "agy", "ai-round": "claude"}


LAUNCHERS: dict[str, LauncherSpec] = {
    "ai-round": LauncherSpec(
        name="ai-round",
        expected_agents=("claude",),
        mode="pane_run",
        command="ai-round",
        forward_claude_env=True,
    ),
    "ai-best": LauncherSpec(
        name="ai-best",
        expected_agents=("claude",),
        mode="pane_run",
        command="ai-best",
        forward_claude_env=True,
    ),
    "ai-agent": LauncherSpec(
        name="ai-agent",
        expected_agents=("agy", "claude"),
        mode="pane_run",
        command="ai-agent",
        forward_claude_env=True,
        forward_env=("AI_AGENT_BACKEND", "AI_AGENT_PREFER", "AI_AGENT_MIN_REMAINING"),
        selects_backend=True,
    ),
    "ay": LauncherSpec(
        name="ay",
        expected_agents=("agy",),
        mode="pane_run",
        command="ay",
        forward_claude_env=False,
    ),
    "codex": LauncherSpec(
        name="codex",
        expected_agents=("codex",),
        mode="agent_start",
        agent_kind="codex",
    ),
    "claude": LauncherSpec(
        name="claude",
        expected_agents=("claude",),
        mode="agent_start",
        agent_kind="claude",
    ),
    "shell": LauncherSpec(
        name="shell",
        expected_agents=(),
        mode="none",
    ),
}


def resolve_claude_local_settings(env: Mapping[str, str]) -> str:
    """Resolve the absolute path to Claude local-settings.json per user rules."""
    raw_dir = env.get("CLAUDE_CONFIG_DIR", "").strip()
    base = Path(raw_dir).expanduser() if raw_dir else (Path.home() / ".claude")
    return str(base.resolve() / "local-settings.json")


def build_agent_start_args(launcher: LauncherSpec, env: Mapping[str, str]) -> list[str]:
    """Build native CLI flags passed after `--` to `herdr agent start`."""
    if launcher.name == "codex":
        return ["--profile", "local", "--dangerously-bypass-approvals-and-sandbox"]
    if launcher.name == "claude":
        return [
            "--settings",
            resolve_claude_local_settings(env),
            "--dangerously-skip-permissions",
        ]
    return []


def build_pane_run_command(launcher: LauncherSpec, env: Mapping[str, str]) -> str:
    """Build the shell command line sent to `herdr pane run`."""
    if not launcher.command:
        raise ValueError(f"Launcher {launcher.name!r} has no pane_run command")
    assignments: list[str] = []
    if launcher.forward_claude_env:
        for var in _CLAUDE_ENV_VARS:
            value = env.get(var, "").strip()
            if value:
                value = str(Path(value).expanduser().resolve())
                assignments.append(f"{var}={shlex.quote(value)}")
    for var in launcher.forward_env:
        value = env.get(var, "").strip()
        if value:
            assignments.append(f"{var}={shlex.quote(value)}")
    if not assignments:
        return launcher.command
    return f"{' '.join(assignments)} {launcher.command}"


def sanitize_agent_name(
    label: str,
    existing_names: set[str] | frozenset[str] = frozenset(),
    *,
    fallback: str = "lead",
) -> str:
    """Build a valid unique Herdr agent name matching `[a-z][a-z0-9_-]{0,31}`."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", label.lower()).strip("-_")
    if not slug or not slug[0].isalpha():
        slug = f"{fallback}-{slug}" if slug else fallback
    base = slug[:28]
    if base not in existing_names:
        return base
    for idx in range(2, 100):
        candidate = f"{base[: 28 - len(str(idx)) - 1]}-{idx}"
        if candidate not in existing_names:
            return candidate
    return f"{fallback}-{int(time.time()) % 10000}"


def _single_agent(spec: LauncherSpec) -> str | None:
    return spec.expected_agents[0] if len(spec.expected_agents) == 1 else None


def resolve_backend_agent(backend: str) -> str:
    """Map an `ai-agent` backend name to the Herdr agent kind it launches."""
    agent = AI_AGENT_BACKEND_AGENTS.get(backend)
    if agent is None:
        raise CliError(
            "AI_AGENT_UNKNOWN_BACKEND",
            f"ai-agent selected unsupported backend {backend!r}.",
            f"Expected one of: {', '.join(AI_AGENT_BACKEND_AGENTS)}. Update hcmd or ai-agent.",
            details={"backend": backend},
        )
    return agent


def plan_ensure(
    spaces: Sequence[SpaceSnapshot],
    *,
    label: str | None,
    cwd: str | None,
    ticket: str | None,
    pr_number: str | None,
    launcher_name: str,
    base_ref: str | None = None,
    on_existing: str | None = None,
    force_new: bool = False,
    reuse_target: str | None = None,
    panes_by_workspace: Mapping[str, Sequence[PaneInfo]] | None = None,
    read_detection: Callable[[str], str] | None = None,
    home_dir: str | None = None,
    realpath_fn: Callable[[str], str] = os.path.realpath,
    existing_agent_names: set[str] | frozenset[str] = frozenset(),
    dir_exists: Callable[[str], bool] = os.path.isdir,
) -> EnsurePlan:
    """Create a validated, idempotent plan for `ensure` without side effects."""
    spec = LAUNCHERS.get(launcher_name)
    if spec is None:
        raise CliError(
            "INVALID_LAUNCHER",
            f"Unsupported launcher {launcher_name!r}.",
            f"Choose one of: {', '.join(LAUNCHERS)}.",
            exit_code=EXIT_USAGE,
        )

    if on_existing is not None and on_existing not in VALID_ON_EXISTING_POLICIES:
        raise CliError(
            "INVALID_ON_EXISTING",
            f"Unsupported --on-existing policy {on_existing!r}.",
            f"Choose one of: {', '.join(VALID_ON_EXISTING_POLICIES)}.",
            exit_code=EXIT_USAGE,
        )
    if force_new and on_existing is not None and on_existing != "new":
        raise CliError(
            "CONFLICTING_POLICY",
            f"Cannot combine --new with --on-existing {on_existing}.",
            "Use either `--on-existing new` (or `--new`) or another `--on-existing` mode.",
            exit_code=EXIT_USAGE,
        )
    policy = "new" if force_new else (on_existing or "reuse")

    clean_reuse = reuse_target.strip() if reuse_target and reuse_target.strip() else None
    existing: SpaceSnapshot | None = None
    norm_cwd: str | None = None
    norm_ticket: str | None = None
    norm_pr: str | None = None
    derived_label = ""

    if clean_reuse is not None:
        if any((label, cwd, ticket, pr_number, base_ref)):
            raise CliError(
                "CONFLICTING_TARGET",
                "--reuse cannot be combined with --label, --cwd, --ticket, --pr, or --base.",
                "Pass `--reuse <workspace>` with `--launcher` only.",
                exit_code=EXIT_USAGE,
            )
        if policy != "reuse":
            raise CliError(
                "CONFLICTING_POLICY",
                f"--reuse cannot be combined with --on-existing {policy}.",
                "Remove `--new` / `--on-existing` when passing `--reuse`.",
                exit_code=EXIT_USAGE,
            )
        existing = resolve_target(spaces, clean_reuse)
    else:
        source_count = sum(1 for item in (cwd, ticket, pr_number) if item)
        if source_count > 1:
            raise CliError(
                "CONFLICTING_TARGET",
                "Specify at most one of --cwd, --ticket, or --pr.",
                "Pass a single workspace source option.",
                exit_code=EXIT_USAGE,
            )
        if source_count == 0 and not label:
            raise CliError(
                "MISSING_TARGET",
                "Specify --label, --cwd, --ticket, --pr, or --reuse.",
                "Provide a target workspace or directory, e.g. `hcmd ensure --ticket CBRD-10002`.",
                exit_code=EXIT_USAGE,
            )

        if cwd:
            norm_cwd = os.path.normpath(os.path.abspath(os.path.expanduser(cwd)))
            if not dir_exists(norm_cwd):
                raise CliError(
                    "CWD_NOT_FOUND",
                    f"Target directory does not exist: {norm_cwd}",
                    "Pass an existing directory to --cwd, or use --ticket for CUBRID worktrees.",
                    exit_code=EXIT_DOMAIN,
                )

        norm_ticket = ticket.strip().upper() if ticket else None
        norm_pr = pr_number.strip().lstrip("#") if pr_number else None
        if norm_pr and not norm_pr.isdigit():
            raise CliError(
                "INVALID_PR",
                f"PR identifier must be a number, got {pr_number!r}.",
                "Pass a numeric PR ID such as `--pr 1234`.",
                exit_code=EXIT_USAGE,
            )
        if policy == "new" and (norm_ticket or norm_pr):
            raise CliError(
                "NEW_UNSUPPORTED_FOR_WORKTREE",
                (
                    "--new cannot be used with --ticket or --pr because CUBRID worktrees "
                    "map 1:1 to a ticket or PR."
                ),
                (
                    "Pass `--cwd <worktree-path> --label <new-label> --on-existing new` to open "
                    "an additional workspace in that directory."
                ),
                exit_code=EXIT_USAGE,
            )

        derived_label = (
            label or norm_ticket or (f"PR-{norm_pr}" if norm_pr else Path(norm_cwd).name)
        )
        if policy == "fail":
            find_result = find_matching_spaces(
                spaces,
                label=derived_label,
                cwd=norm_cwd,
                ticket=norm_ticket,
                pr_number=norm_pr,
                include_self=False,
                include_deleted=False,
                panes_by_workspace=panes_by_workspace,
                home_dir=home_dir,
                realpath_fn=realpath_fn,
            )
            if find_result.found:
                if read_detection is not None:
                    verified_spaces = verify_claude_busy(
                        [m.space for m in find_result.matches], read_detection
                    )
                    updated_matches = tuple(
                        replace(m, space=s)
                        for m, s in zip(find_result.matches, verified_spaces, strict=True)
                    )
                    find_result = replace(
                        find_result,
                        matches=updated_matches,
                        recommendation=compute_find_recommendation(updated_matches),
                    )
                summary_items = []
                for match in find_result.matches[:3]:
                    pane = match.space.primary_pane
                    agent_str = pane.agent_kind or "shell" if pane else "-"
                    status_str = pane.effective_status if pane else "none"
                    summary_items.append(
                        f"{match.space.workspace_id}:{match.space.label} [{agent_str}:{status_str}]"
                    )
                summary = ", ".join(summary_items)
                raise CliError(
                    "EXISTING_WORKSPACE_FOUND",
                    (
                        f"Found {len(find_result.matches)} existing workspace(s) "
                        f"(recommendation={find_result.recommendation}): {summary}."
                    ),
                    (
                        "Ask the user whether to reuse an existing workspace "
                        "(`hcmd ensure --reuse <workspace_id>` or `hcmd send <workspace_id>`), "
                        "or create a new one with `--on-existing new`."
                    ),
                    exit_code=EXIT_DOMAIN,
                    details=find_result.to_dict(),
                )
        elif policy == "reuse":
            find_result = find_matching_spaces(
                spaces,
                label=label,
                cwd=norm_cwd,
                ticket=norm_ticket,
                pr_number=norm_pr,
                include_self=False,
                include_deleted=False,
                panes_by_workspace=panes_by_workspace,
                home_dir=home_dir,
                realpath_fn=realpath_fn,
            )
            first_exact = next((m for m in find_result.matches if is_primary_exact_match(m)), None)
            existing = first_exact.space if first_exact is not None else None

    if existing is not None:
        pane = existing.primary_pane
        existing_pane_id = pane.pane_id if pane else None
        if not spec.launches_agent:
            agent_action = "none"
        elif pane is None:
            raise CliError(
                "NO_PRIMARY_PANE",
                f"Workspace {existing.workspace_id} ({existing.label}) has no primary pane.",
                "Inspect the workspace tabs in Herdr before retrying.",
            )
        elif spec.accepts(pane.agent_kind):
            agent_action = "reuse"
        elif pane.agent_kind is None:
            agent_action = "launch"
        else:
            raise CliError(
                "OCCUPIED_BY_OTHER_AGENT",
                (
                    f"Primary pane {pane.pane_id} in {existing.label} already runs "
                    f"{pane.agent_kind!r}, not {spec.expected_label}."
                ),
                "Reuse the existing agent, or open a sibling tab manually if intentional.",
                details={
                    "workspace_id": existing.workspace_id,
                    "pane_id": pane.pane_id,
                    "running_agent": pane.agent_kind,
                    "expected_agent": _single_agent(spec),
                    "expected_agents": list(spec.expected_agents),
                },
            )
        return EnsurePlan(
            workspace_action="reuse",
            agent_action=agent_action,
            launcher=spec.name,
            expected_agent=(
                pane.agent_kind if agent_action == "reuse" and pane else _single_agent(spec)
            ),
            label=existing.label,
            cwd=pane.cwd if pane and pane.cwd else norm_cwd,
            ticket=norm_ticket or existing.ticket_key,
            pr_number=norm_pr,
            base_ref=base_ref,
            existing_workspace_id=existing.workspace_id,
            existing_pane_id=existing_pane_id,
            agent_name=(
                pane.agent_name
                if pane and pane.agent_name
                else sanitize_agent_name(existing.label, existing_agent_names)
            ),
        )

    if norm_ticket:
        workspace_action = "create_cubrid_ticket"
    elif norm_pr:
        workspace_action = "create_cubrid_pr"
    else:
        workspace_action = "create_cwd"

    return EnsurePlan(
        workspace_action=workspace_action,
        agent_action="launch" if spec.launches_agent else "none",
        launcher=spec.name,
        expected_agent=_single_agent(spec),
        label=derived_label,
        cwd=norm_cwd,
        ticket=norm_ticket,
        pr_number=norm_pr,
        base_ref=base_ref,
        existing_workspace_id=None,
        existing_pane_id=None,
        agent_name=sanitize_agent_name(derived_label, existing_agent_names),
    )


def execute_ensure(
    plan: EnsurePlan,
    *,
    client: SnapshotClient,
    create_cwd: CreateCwdFn,
    create_ticket: CreateTicketFn,
    create_pr: CreatePrFn,
    run_pane: RunPaneFn,
    start_agent: StartAgentFn,
    env: Mapping[str, str],
    select_backend: SelectBackendFn | None = None,
    trust_workspace: TrustWorkspaceFn | None = None,
    read_pane: ReadPaneFn | None = None,
    send_keys: SendKeysFn | None = None,
    self_workspace_id: str | None = None,
    dry_run: bool = False,
    ready_timeout: float = 45.0,
    poll_interval: float = 0.5,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> EnsureResult:
    """Execute an EnsurePlan idempotently, converging workspace and first-tab agent.

    For backend-selecting launchers (`ai-agent`), `select_backend` runs a read-only
    preflight before any workspace is created so quota failures surface fast and
    dry runs report the predicted agent kind.
    """
    spec = LAUNCHERS[plan.launcher]
    predicted_agent = plan.expected_agent
    if spec.selects_backend and plan.agent_action == "launch" and select_backend is not None:
        predicted_agent = resolve_backend_agent(select_backend())

    if dry_run:
        return EnsureResult(
            dry_run=True,
            workspace_action=plan.workspace_action,
            agent_action=plan.agent_action,
            launcher=plan.launcher,
            workspace_id=plan.existing_workspace_id,
            label=plan.label,
            tab_id=None,
            pane_id=plan.existing_pane_id,
            cwd=plan.cwd,
            agent_kind=predicted_agent,
            agent_status=None,
            agent_name=plan.agent_name,
        )

    created_meta: dict[str, Any] = {}
    if plan.workspace_action == "create_cwd":
        target_cwd = plan.cwd or os.getcwd()
        created_meta = create_cwd(plan.label, target_cwd)
    elif plan.workspace_action == "create_cubrid_ticket":
        assert plan.ticket is not None
        created_meta = create_ticket(plan.ticket, base=plan.base_ref)
    elif plan.workspace_action == "create_cubrid_pr":
        assert plan.pr_number is not None
        created_meta = create_pr(plan.pr_number)

    raw_snap = client.snapshot()
    spaces = parse_snapshot(raw_snap, self_workspace_id=self_workspace_id)
    target_space = _locate_ensured_space(spaces, plan, created_meta)
    if target_space is None or target_space.primary_pane is None:
        raise CliError(
            "ENSURE_SPACE_MISSING",
            f"Could not locate ensured workspace {plan.label!r} in Herdr snapshot.",
            "Run `hcmd status` to inspect created workspaces.",
        )

    pane = target_space.primary_pane
    if not spec.launches_agent:
        return _build_ensure_result(plan, target_space, agent_action="none")

    if spec.accepts(pane.agent_kind):
        return _build_ensure_result(plan, target_space, agent_action="reuse")

    if pane.agent_kind is not None:
        raise CliError(
            "OCCUPIED_BY_OTHER_AGENT",
            (
                f"Primary pane {pane.pane_id} already runs {pane.agent_kind!r}, "
                f"expected {spec.expected_label}."
            ),
            "Inspect the pane before launching another agent.",
            details={"workspace_id": target_space.workspace_id, "pane_id": pane.pane_id},
        )

    launch_cwd = pane.cwd or str(created_meta.get("cwd") or plan.cwd or "")
    if trust_workspace is not None and launch_cwd:
        trust_workspace(launch_cwd, spec.expected_agents, env)

    if spec.mode == "agent_start":
        live_names = collect_live_agent_names(raw_snap)
        agent_name = sanitize_agent_name(target_space.label, live_names)
        assert spec.agent_kind is not None
        timeout_ms = max(4000, min(300000, int(ready_timeout * 1000)))
        start_agent(
            agent_name,
            spec.agent_kind,
            pane.pane_id,
            build_agent_start_args(spec, env),
            timeout_ms=timeout_ms,
        )
        refreshed = parse_snapshot(client.snapshot(), self_workspace_id=self_workspace_id)
        final_space = _locate_ensured_space(
            refreshed,
            plan,
            {"workspace_id": target_space.workspace_id},
        )
        return _build_ensure_result(plan, final_space or target_space, agent_action="launch")

    cmd = build_pane_run_command(spec, env)
    run_pane(pane.pane_id, cmd)
    settled = _wait_for_launched_agent(
        client=client,
        workspace_id=target_space.workspace_id,
        expected_agents=spec.expected_agents,
        self_workspace_id=self_workspace_id,
        timeout=ready_timeout,
        poll_interval=poll_interval,
        sleep_fn=sleep_fn,
        read_pane=read_pane,
        send_keys=send_keys,
    )
    return _build_ensure_result(plan, settled, agent_action="launch")


def _locate_ensured_space(
    spaces: Sequence[SpaceSnapshot],
    plan: EnsurePlan,
    created_meta: Mapping[str, Any],
) -> SpaceSnapshot | None:
    wid = created_meta.get("workspace_id") or plan.existing_workspace_id
    if isinstance(wid, str) and wid:
        for space in spaces:
            if space.workspace_id == wid:
                return space
    cwd = created_meta.get("cwd") or plan.cwd
    norm_cwd = os.path.normpath(os.path.abspath(cwd)) if isinstance(cwd, str) and cwd else None
    ticket = created_meta.get("ticket") or plan.ticket
    norm_ticket = ticket.upper() if isinstance(ticket, str) and ticket else None
    found = find_matching_spaces(
        spaces,
        label=plan.label or None,
        cwd=norm_cwd,
        ticket=norm_ticket,
        pr_number=plan.pr_number,
        include_self=True,
        include_deleted=True,
    )
    return found.matches[0].space if found.matches else None


def detect_startup_overlay(text: str) -> bool:
    """Return True when terminal text shows a dismissible startup overlay (e.g. `ay` banner)."""
    cleaned = strip_ansi(text)
    return any(pattern.search(cleaned) for pattern in _OVERLAY_PATTERNS)


def _dismiss_startup_overlay_if_present(
    *,
    pane_id: str,
    agent_kind: str | None,
    read_pane: ReadPaneFn | None,
    send_keys: SendKeysFn | None,
    sleep_fn: Callable[[float], None],
    max_attempts: int = 2,
) -> bool:
    """Dismiss an `ay` startup overlay (`esc to dismiss`) if visible before prompting."""
    if agent_kind != "agy" or read_pane is None or send_keys is None:
        return False
    dismissed = False
    for _ in range(max_attempts):
        try:
            read = read_pane(pane_id, has_agent=True, source="visible", lines=25)
        except CliError:
            break
        if not detect_startup_overlay(str(read.get("text") or "")):
            break
        try:
            send_keys(pane_id, ("esc",))
        except CliError:
            break
        dismissed = True
        sleep_fn(0.4)
    return dismissed


def _wait_for_launched_agent(
    *,
    client: SnapshotClient,
    workspace_id: str,
    expected_agents: tuple[str, ...],
    self_workspace_id: str | None,
    timeout: float,
    poll_interval: float,
    sleep_fn: Callable[[float], None],
    read_pane: ReadPaneFn | None = None,
    send_keys: SendKeysFn | None = None,
) -> SpaceSnapshot:
    deadline = time.monotonic() + timeout
    expected_label = " or ".join(expected_agents)
    last_space: SpaceSnapshot | None = None
    while True:
        spaces = parse_snapshot(client.snapshot(), self_workspace_id=self_workspace_id)
        space = next((s for s in spaces if s.workspace_id == workspace_id), None)
        if space is not None and space.primary_pane is not None:
            last_space = space
            pane = space.primary_pane
            if pane.agent_kind in expected_agents:
                if pane.raw_status == "blocked":
                    raise CliError(
                        "AGENT_STARTUP_BLOCKED",
                        (
                            f"Launched agent {pane.agent_kind} is blocked at startup "
                            f"in {pane.pane_id}."
                        ),
                        f"Inspect it with `hcmd peek {workspace_id}`.",
                        details={"workspace_id": workspace_id, "pane_id": pane.pane_id},
                    )
                if pane.interactive_ready or pane.raw_status in {"idle", "done"}:
                    _dismiss_startup_overlay_if_present(
                        pane_id=pane.pane_id,
                        agent_kind=pane.agent_kind,
                        read_pane=read_pane,
                        send_keys=send_keys,
                        sleep_fn=sleep_fn,
                    )
                    return space
        if time.monotonic() >= deadline:
            raise CliError(
                "AGENT_READY_TIMEOUT",
                (
                    f"Timed out after {timeout:.0f}s waiting for {expected_label} "
                    f"in workspace {workspace_id}."
                ),
                f"Inspect the pane with `hcmd peek {workspace_id}` before retrying.",
                details={
                    "workspace_id": workspace_id,
                    "pane_id": last_space.primary_pane.pane_id
                    if last_space and last_space.primary_pane
                    else None,
                },
            )
        sleep_fn(poll_interval)


def _build_ensure_result(
    plan: EnsurePlan,
    space: SpaceSnapshot,
    *,
    agent_action: str,
) -> EnsureResult:
    pane = space.primary_pane
    return EnsureResult(
        dry_run=False,
        workspace_action=plan.workspace_action,
        agent_action=agent_action,
        launcher=plan.launcher,
        workspace_id=space.workspace_id,
        label=space.label,
        tab_id=space.primary_tab_id,
        pane_id=pane.pane_id if pane else None,
        cwd=pane.cwd if pane else plan.cwd,
        agent_kind=pane.agent_kind if pane else None,
        agent_status=pane.effective_status if pane else None,
        agent_name=pane.agent_name if pane else plan.agent_name,
    )


def validate_send(space: SpaceSnapshot, *, prompt: str, force: bool) -> None:
    """Validate that a workspace's first-tab agent can safely accept a prompt."""
    if not prompt.strip():
        raise CliError(
            "EMPTY_PROMPT",
            "Prompt text cannot be empty.",
            "Pass non-empty text via --prompt or stdin (`--prompt -`).",
            exit_code=EXIT_USAGE,
        )
    if space.is_self and not force:
        raise CliError(
            "CANNOT_SEND_TO_SELF",
            f"Refusing to send a prompt to the caller's own workspace ({space.workspace_id}).",
            "Target a worker workspace instead, or pass --force if intentional.",
        )
    pane = space.primary_pane
    if pane is None or pane.agent_kind is None:
        raise CliError(
            "NO_AGENT",
            f"Workspace {space.workspace_id} ({space.label}) has no agent in its first tab.",
            "Launch an agent first with `hcmd ensure`.",
        )
    if pane.effective_status == "blocked":
        raise CliError(
            "AGENT_BLOCKED",
            f"Agent in {space.label} ({pane.pane_id}) is blocked waiting for approval or input.",
            f"Inspect the dialog with `hcmd peek {space.workspace_id}` first.",
            details={"workspace_id": space.workspace_id, "pane_id": pane.pane_id},
        )
    if pane.effective_status in {"working", "busy_bg"} and not force:
        raise CliError(
            "AGENT_BUSY",
            (f"Agent in {space.label} ({pane.pane_id}) is currently {pane.effective_status}."),
            (
                "Check progress with `hcmd status` / `hcmd digest` "
                "(or `hcmd wait` if explicitly waiting), or pass --force."
            ),
            details={"workspace_id": space.workspace_id, "pane_id": pane.pane_id},
        )


def build_commander_kickoff_envelope(
    *,
    space: SpaceSnapshot,
    commander_workspace_id: str | None = None,
    tracker_id: str | None = None,
    context_notes: Sequence[str] = (),
) -> str:
    """Build the minimal 6-10 line kickoff envelope prepended by `hcmd send --kickoff`."""
    commander_ref = commander_workspace_id or "unknown"
    lines = [
        "[Herdr Commander Kickoff]",
        (
            f"Role: You are the Herdr Space Lead (tabs[0]) of {space.workspace_id} "
            f"({space.label}), delegated by Herdr Commander ({commander_ref})."
        ),
        (
            "Human-in-the-loop: The user may inspect or reply directly in Tab 1; keep Tab 1 "
            "summaries self-contained and decision-first for both the user and `hcmd digest`."
        ),
        (
            "Delegation: Use `hcmd tab` (max 8 sub-tabs) for heavy/parallel work, keep "
            "same-worktree writes single-writer, close created sub-tabs when done, and never "
            "close `tabs[0]`."
        ),
        (
            "Boundaries: Perform external mutations (GitHub/JIRA/push/merge) only when "
            "explicitly authorized in the Objective or user reply."
        ),
    ]
    clean_tracker = tracker_id.strip() if tracker_id else ""
    if clean_tracker:
        lines.append(f"Tracker: {clean_tracker}")
    for note in context_notes:
        clean_note = note.strip()
        if clean_note:
            lines.append(f"Context: {clean_note}")
    lines.append(
        "Reporting: Persist decisive evidence to files (not screen-only) and end your "
        "final/blocked Tab 1 response with concise Status / Summary / Evidence / Decision / Next "
        "(`- Status: <done | blocked>`, `- Summary:`, `- Evidence:`, `- Decision / Next:`)."
    )
    return "\n".join(lines)


def compose_send_prompt(
    prompt: str,
    *,
    space: SpaceSnapshot,
    kickoff: bool = False,
    commander_workspace_id: str | None = None,
    tracker_id: str | None = None,
    context_notes: Sequence[str] = (),
) -> str:
    """Return raw `prompt` unchanged unless `kickoff=True`, which prepends the envelope."""
    if not prompt.strip():
        raise CliError(
            "EMPTY_PROMPT",
            "Prompt text cannot be empty.",
            "Pass non-empty text via --prompt or stdin (`--prompt -`).",
            exit_code=EXIT_USAGE,
        )
    if not kickoff:
        return prompt
    envelope = build_commander_kickoff_envelope(
        space=space,
        commander_workspace_id=commander_workspace_id,
        tracker_id=tracker_id,
        context_notes=context_notes,
    )
    return f"{envelope}\n\n{prompt}"


def execute_send(
    space: SpaceSnapshot,
    *,
    prompt: str,
    wait: bool,
    timeout: float,
    force: bool,
    dry_run: bool,
    prompt_fn: PromptAgentFn,
    kickoff: bool = False,
) -> SendResult:
    """Validate and send a prompt to a workspace's first-tab agent."""
    validate_send(space, prompt=prompt, force=force)
    assert space.primary_pane is not None
    assert space.primary_pane.agent_kind is not None
    pane = space.primary_pane

    if dry_run:
        return SendResult(
            dry_run=True,
            workspace_id=space.workspace_id,
            label=space.label,
            pane_id=pane.pane_id,
            agent_kind=pane.agent_kind,
            agent_status=pane.effective_status,
            waited=wait,
            settled_status=None,
            prompt_chars=len(prompt),
            kickoff=kickoff,
        )

    timeout_ms = int(timeout * 1000) if wait else None
    result = prompt_fn(pane.pane_id, prompt, wait=wait, timeout_ms=timeout_ms)
    settled = result.get("agent_status") or result.get("status")
    return SendResult(
        dry_run=False,
        workspace_id=space.workspace_id,
        label=space.label,
        pane_id=pane.pane_id,
        agent_kind=pane.agent_kind,
        agent_status=pane.effective_status,
        waited=wait,
        settled_status=str(settled) if isinstance(settled, str) else None,
        prompt_chars=len(prompt),
        kickoff=kickoff,
    )


def match_wait_targets(
    spaces: Sequence[SpaceSnapshot],
    *,
    until: frozenset[str],
    workspace: str | None,
    workers_only: bool,
) -> tuple[list[SpaceSnapshot], list[SpaceSnapshot]]:
    """Return (watched_spaces, matched_spaces) for `wait`."""
    if workspace is not None:
        watched = [resolve_target(spaces, workspace)]
    elif workers_only:
        watched = [s for s in spaces if not s.is_self]
    else:
        watched = list(spaces)

    matched: list[SpaceSnapshot] = []
    for space in watched:
        pane = space.primary_pane
        effective = pane.effective_status if pane else space.attention
        if effective in until or space.attention in until:
            matched.append(space)
    return watched, matched


def _collect_watched_pane_ids(
    raw_snap: dict[str, Any],
    watched_spaces: Sequence[SpaceSnapshot],
    *,
    include_subtabs: bool,
) -> list[str]:
    if not include_subtabs:
        return [s.primary_pane.pane_id for s in watched_spaces if s.primary_pane is not None]
    watched_ids = {s.workspace_id for s in watched_spaces}
    pane_ids: list[str] = []
    for wid in watched_ids:
        for tab in parse_workspace_tabs(raw_snap, wid):
            for pane in tab.panes:
                pane_ids.append(pane.pane_id)
    return pane_ids


def _is_self_pane_update_event(event: Mapping[str, Any], self_workspace_id: str | None) -> bool:
    if not self_workspace_id:
        return False
    ev_type = event.get("event") or event.get("type") or ""
    if ev_type != "pane.updated":
        return False
    data = event.get("data") if isinstance(event.get("data"), Mapping) else event
    ev_ws = data.get("workspace_id")
    ev_pane = data.get("pane_id")
    if ev_ws == self_workspace_id:
        return True
    return isinstance(ev_pane, str) and ev_pane.startswith(f"{self_workspace_id}:")


def execute_wait(
    *,
    client: WaitClient,
    until: Sequence[str],
    workspace: str | None,
    workers_only: bool,
    timeout: float,
    verify_busy: bool,
    self_workspace_id: str | None,
) -> WaitResult:
    """Wait via Herdr socket events until any watched first-tab reaches target state."""
    result, _cursor, _warnings = execute_wait_with_cursor(
        client=client,
        until=until,
        workspace=workspace,
        workers_only=workers_only,
        timeout=timeout,
        verify_busy=verify_busy,
        self_workspace_id=self_workspace_id,
    )
    return result


def execute_wait_with_cursor(
    *,
    client: WaitClient,
    until: Sequence[str],
    workspace: str | None,
    workers_only: bool,
    timeout: float,
    verify_busy: bool,
    self_workspace_id: str | None,
    since_last: bool = False,
    prev_cursor: DigestCursor | None = None,
    cursor_scope: str = "default",
    changes_only: bool = False,
    include_subtabs: bool = False,
    stop_when_idle: bool = False,
    allow_timeout: bool = False,
    emit_digest: bool = False,
    compact: bool = False,
    peek_lines: int = 40,
    peek_source: str = "recent-unwrapped",
    max_peek_chars: int | None = None,
    read_pane: ReadPaneFn | None = None,
    tracker_items: Sequence[dict[str, Any]] | None = None,
    debounce_seconds: float = 0.25,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[WaitResult, DigestCursor | None, list[str]]:
    """Wait via Herdr socket events with optional delta cursor, sub-tab watch, and digest."""
    start = time.monotonic()
    deadline = start + timeout
    until_tuple = tuple(until)
    until_set = frozenset(until_tuple)
    warnings: list[str] = []
    delta_mode = bool(since_last or changes_only)
    effective_read_pane: ReadPaneFn = read_pane or (lambda pid, **_kw: {"pane_id": pid, "text": ""})
    effective_peek_lines = peek_lines if (emit_digest or read_pane is not None) else 0

    def evaluate_spaces() -> tuple[dict[str, Any], list[SpaceSnapshot], list[SpaceSnapshot]]:
        raw_snap = client.snapshot()
        spaces = parse_snapshot(raw_snap, self_workspace_id=self_workspace_id)
        if verify_busy:
            spaces = verify_claude_busy(spaces, client.read_detection)
            if include_subtabs or stop_when_idle:
                spaces = verify_subtabs_claude_busy(raw_snap, spaces, client.read_detection)
        if tracker_items:
            spaces = correlate_work_tracker(spaces, tracker_items)
        watched, matched = match_wait_targets(
            spaces,
            until=until_set,
            workspace=workspace,
            workers_only=workers_only,
        )
        if include_subtabs and "blocked" in until_set:
            matched_ids = {s.workspace_id for s in matched}
            for space in watched:
                if space.sub_tabs.any_blocked and space.workspace_id not in matched_ids:
                    matched.append(space)
                    matched_ids.add(space.workspace_id)
        return raw_snap, watched, matched

    def build_digest_for_spaces(
        watched: Sequence[SpaceSnapshot],
        baseline: DigestCursor | None,
        *,
        force_since_last: bool,
    ) -> tuple[DigestResult, DigestCursor]:
        res, next_cur, warn_list = execute_digest_with_cursor(
            watched,
            read_pane=effective_read_pane,
            include_all=False,
            include_idle=False,
            workspace=workspace,
            peek_lines=effective_peek_lines,
            peek_source=peek_source,
            compact=compact,
            max_peek_chars=max_peek_chars,
            since_last=force_since_last,
            prev_cursor=baseline,
            cursor_scope=cursor_scope,
        )
        warnings.extend(warn_list)
        return res, next_cur

    raw_snap, watched, matched = evaluate_spaces()
    active_cursor: DigestCursor | None = prev_cursor

    if delta_mode:
        if since_last and prev_cursor is not None:
            digest_res, next_cur = build_digest_for_spaces(
                watched, prev_cursor, force_since_last=True
            )
            if digest_res.changed:
                changed_spaces = tuple(entry.space for entry in digest_res.entries)
                return (
                    WaitResult(
                        until=until_tuple,
                        matched=changed_spaces,
                        elapsed_seconds=time.monotonic() - start,
                        from_initial_snapshot=True,
                        reason="changed",
                        timed_out=False,
                        digest=digest_res if emit_digest else None,
                    ),
                    next_cur,
                    warnings,
                )
            active_cursor = next_cur
        else:
            # Establish initial baseline without firing on already-settled workspaces.
            _, active_cursor = build_digest_for_spaces(watched, None, force_since_last=True)

        if stop_when_idle and not has_active_work(watched):
            idle_digest = None
            if emit_digest:
                idle_digest = DigestResult(
                    total_workspaces=len(watched),
                    counts={},
                    sub_tabs_blocked_count=sum(1 for s in watched if s.sub_tabs.any_blocked),
                    entries=(),
                    peek_lines=effective_peek_lines,
                    include_all=False,
                    has_active_work=False,
                    since_last=since_last,
                    changed=False,
                    compact=compact,
                    cursor_scope=cursor_scope if since_last else None,
                )
            return (
                WaitResult(
                    until=until_tuple,
                    matched=(),
                    elapsed_seconds=time.monotonic() - start,
                    from_initial_snapshot=True,
                    reason="no_active_work",
                    timed_out=False,
                    digest=idle_digest,
                ),
                active_cursor,
                warnings,
            )
    else:
        if matched:
            digest_res = None
            if emit_digest:
                digest_res, active_cursor = build_digest_for_spaces(
                    watched, prev_cursor, force_since_last=since_last
                )
            return (
                WaitResult(
                    until=until_tuple,
                    matched=tuple(matched),
                    elapsed_seconds=time.monotonic() - start,
                    from_initial_snapshot=True,
                    reason="matched",
                    timed_out=False,
                    digest=digest_res,
                ),
                active_cursor,
                warnings,
            )
        if stop_when_idle and not has_active_work(watched):
            return (
                WaitResult(
                    until=until_tuple,
                    matched=(),
                    elapsed_seconds=time.monotonic() - start,
                    from_initial_snapshot=True,
                    reason="no_active_work",
                    timed_out=False,
                    digest=None,
                ),
                active_cursor,
                warnings,
            )

    pane_ids = _collect_watched_pane_ids(raw_snap, watched, include_subtabs=include_subtabs)
    last_pane_update_eval = 0.0
    for event in client.subscribe_events(pane_ids, deadline=deadline):
        if _is_self_pane_update_event(event, self_workspace_id):
            continue
        ev_type = event.get("event") or event.get("type") or ""
        if ev_type == "pane.updated" and debounce_seconds > 0:
            now_mono = time.monotonic()
            since_eval = now_mono - last_pane_update_eval
            if last_pane_update_eval > 0.0 and since_eval < debounce_seconds:
                remaining_wait = min(debounce_seconds - since_eval, max(0.0, deadline - now_mono))
                if remaining_wait > 0:
                    sleep_fn(remaining_wait)
            last_pane_update_eval = time.monotonic()

        raw_snap, watched, matched = evaluate_spaces()
        if delta_mode:
            digest_res, next_cur = build_digest_for_spaces(
                watched, active_cursor, force_since_last=True
            )
            if digest_res.changed:
                changed_spaces = tuple(entry.space for entry in digest_res.entries)
                return (
                    WaitResult(
                        until=until_tuple,
                        matched=changed_spaces,
                        elapsed_seconds=time.monotonic() - start,
                        from_initial_snapshot=False,
                        reason="changed",
                        timed_out=False,
                        digest=digest_res if emit_digest else None,
                    ),
                    next_cur,
                    warnings,
                )
            active_cursor = next_cur
            if stop_when_idle and not has_active_work(watched):
                return (
                    WaitResult(
                        until=until_tuple,
                        matched=(),
                        elapsed_seconds=time.monotonic() - start,
                        from_initial_snapshot=False,
                        reason="no_active_work",
                        timed_out=False,
                        digest=digest_res if emit_digest else None,
                    ),
                    active_cursor,
                    warnings,
                )
        else:
            if matched:
                digest_res = None
                if emit_digest:
                    digest_res, active_cursor = build_digest_for_spaces(
                        watched, active_cursor, force_since_last=since_last
                    )
                return (
                    WaitResult(
                        until=until_tuple,
                        matched=tuple(matched),
                        elapsed_seconds=time.monotonic() - start,
                        from_initial_snapshot=False,
                        reason="matched",
                        timed_out=False,
                        digest=digest_res,
                    ),
                    active_cursor,
                    warnings,
                )
            if stop_when_idle and not has_active_work(watched):
                return (
                    WaitResult(
                        until=until_tuple,
                        matched=(),
                        elapsed_seconds=time.monotonic() - start,
                        from_initial_snapshot=False,
                        reason="no_active_work",
                        timed_out=False,
                        digest=None,
                    ),
                    active_cursor,
                    warnings,
                )

    if allow_timeout:
        timeout_digest = None
        if emit_digest:
            _, watched, _ = evaluate_spaces()
            timeout_digest, active_cursor = build_digest_for_spaces(
                watched, active_cursor, force_since_last=delta_mode or since_last
            )
        return (
            WaitResult(
                until=until_tuple,
                matched=(),
                elapsed_seconds=time.monotonic() - start,
                from_initial_snapshot=False,
                reason="timeout",
                timed_out=True,
                digest=timeout_digest,
            ),
            active_cursor,
            warnings,
        )

    raise CliError(
        "WAIT_TIMEOUT",
        f"Timed out after {timeout:.0f}s waiting for state(s): {', '.join(until_tuple)}.",
        "Run `hcmd status` to inspect current workspace states.",
        exit_code=EXIT_DOMAIN,
    )


def _validate_agent_quota_caps(
    *,
    spec: LauncherSpec,
    space_agents: int,
    max_space_agents: int,
    session_working_agents: int,
    max_session_agents: int,
) -> None:
    if not spec.launches_agent:
        return
    if max_space_agents > 0 and space_agents >= max_space_agents:
        raise CliError(
            "SPACE_AGENT_LIMIT_REACHED",
            (
                f"Workspace already has {space_agents} non-primary AI agent(s) "
                f"(cap: {max_space_agents})."
            ),
            (
                "Close or wait for finished worker tabs/panes, use `--launcher shell`, "
                "or raise `--max-space-agents` / `HCMD_MAX_SPACE_AGENTS`."
            ),
            details={"space_agents": space_agents, "max_space_agents": max_space_agents},
        )
    if max_session_agents > 0 and session_working_agents >= max_session_agents:
        raise CliError(
            "SESSION_AGENT_LIMIT_REACHED",
            (
                f"Herdr session already has {session_working_agents} working AI agent(s) "
                f"(cap: {max_session_agents})."
            ),
            (
                "Wait for active agents to settle, use `--launcher shell`, "
                "or raise `--max-session-agents` / `HCMD_MAX_SESSION_AGENTS`."
            ),
            details={
                "session_working_agents": session_working_agents,
                "max_session_agents": max_session_agents,
            },
        )


def _find_pane_in_workspace(
    raw: dict[str, Any],
    workspace_id: str,
    pane_id: str,
    *,
    self_tab_id: str | None = None,
    self_pane_id: str | None = None,
) -> PaneInfo | None:
    for tab in parse_workspace_tabs(
        raw,
        workspace_id,
        self_tab_id=self_tab_id,
        self_pane_id=self_pane_id,
    ):
        for pane in tab.panes:
            if pane.pane_id == pane_id:
                return pane
    return None


def _wait_for_pane_agent(
    *,
    client: SnapshotClient,
    workspace_id: str,
    pane_id: str,
    expected_agents: tuple[str, ...],
    timeout: float,
    poll_interval: float,
    sleep_fn: Callable[[float], None],
    read_pane: ReadPaneFn | None = None,
    send_keys: SendKeysFn | None = None,
) -> PaneInfo:
    deadline = time.monotonic() + timeout
    expected_label = " or ".join(expected_agents)
    while True:
        pane = _find_pane_in_workspace(client.snapshot(), workspace_id, pane_id)
        if pane is not None and pane.agent_kind in expected_agents:
            if pane.raw_status == "blocked":
                raise CliError(
                    "AGENT_STARTUP_BLOCKED",
                    f"Launched agent {pane.agent_kind} is blocked at startup in {pane.pane_id}.",
                    (
                        "Inspect it with `hcmd tab peek` / `hcmd pane peek` "
                        f"(or `hcmd peek {workspace_id} --pane {pane.pane_id}`)."
                    ),
                    details={"workspace_id": workspace_id, "pane_id": pane.pane_id},
                )
            if pane.interactive_ready or pane.raw_status in {"idle", "done"}:
                _dismiss_startup_overlay_if_present(
                    pane_id=pane.pane_id,
                    agent_kind=pane.agent_kind,
                    read_pane=read_pane,
                    send_keys=send_keys,
                    sleep_fn=sleep_fn,
                )
                return pane
        if time.monotonic() >= deadline:
            raise CliError(
                "AGENT_READY_TIMEOUT",
                f"Timed out after {timeout:.0f}s waiting for {expected_label} in {pane_id}.",
                f"Inspect pane {pane_id} before retrying.",
                details={"workspace_id": workspace_id, "pane_id": pane_id},
            )
        sleep_fn(poll_interval)


def _launch_agent_in_pane(
    *,
    spec: LauncherSpec,
    workspace_id: str,
    pane_id: str,
    agent_name: str,
    env: Mapping[str, str],
    client: SnapshotClient,
    run_pane: RunPaneFn,
    start_agent: StartAgentFn,
    ready_timeout: float,
    poll_interval: float,
    sleep_fn: Callable[[float], None],
    read_pane: ReadPaneFn | None = None,
    send_keys: SendKeysFn | None = None,
) -> PaneInfo | None:
    if spec.mode == "agent_start":
        assert spec.agent_kind is not None
        timeout_ms = max(4000, min(300000, int(ready_timeout * 1000)))
        start_agent(
            agent_name,
            spec.agent_kind,
            pane_id,
            build_agent_start_args(spec, env),
            timeout_ms=timeout_ms,
        )
        return _find_pane_in_workspace(client.snapshot(), workspace_id, pane_id)

    cmd = build_pane_run_command(spec, env)
    run_pane(pane_id, cmd)
    return _wait_for_pane_agent(
        client=client,
        workspace_id=workspace_id,
        pane_id=pane_id,
        expected_agents=spec.expected_agents,
        timeout=ready_timeout,
        poll_interval=poll_interval,
        sleep_fn=sleep_fn,
        read_pane=read_pane,
        send_keys=send_keys,
    )


def _send_kickoff_prompt_verified(
    *,
    workspace_id: str,
    pane_id: str,
    agent_kind: str | None,
    initial_revision: int,
    prompt: str,
    prompt_fn: PromptAgentFn,
    client: SnapshotClient,
    read_pane: ReadPaneFn | None = None,
    send_keys: SendKeysFn | None = None,
    verify_started: bool = False,
    verify_timeout: float = 5.0,
    poll_interval: float = 0.4,
    sleep_fn: Callable[[float], None] = time.sleep,
    self_tab_id: str | None = None,
    self_pane_id: str | None = None,
) -> PaneInfo | None:
    """Send a kickoff prompt, dismissing `ay` overlays and retrying once if swallowed."""
    if agent_kind == "agy":
        _dismiss_startup_overlay_if_present(
            pane_id=pane_id,
            agent_kind=agent_kind,
            read_pane=read_pane,
            send_keys=send_keys,
            sleep_fn=sleep_fn,
        )

    def poll_accepted(pre_rev: int) -> tuple[PaneInfo | None, bool]:
        deadline = time.monotonic() + verify_timeout
        last_pane: PaneInfo | None = None
        while True:
            pane = _find_pane_in_workspace(
                client.snapshot(),
                workspace_id,
                pane_id,
                self_tab_id=self_tab_id,
                self_pane_id=self_pane_id,
            )
            if pane is not None:
                last_pane = pane
                if pane.raw_status in {"working", "blocked", "done"}:
                    return pane, True
                if pane.revision > pre_rev:
                    if read_pane is not None and agent_kind == "agy":
                        try:
                            vis = str(
                                read_pane(pane_id, has_agent=True, source="visible", lines=25).get(
                                    "text"
                                )
                                or ""
                            )
                        except CliError:
                            vis = ""
                        if not detect_startup_overlay(vis):
                            return pane, True
                    else:
                        return pane, True
            if time.monotonic() >= deadline:
                return last_pane, False
            sleep_fn(poll_interval)

    prompt_fn(pane_id, prompt, wait=False)
    if not verify_started:
        return _find_pane_in_workspace(
            client.snapshot(),
            workspace_id,
            pane_id,
            self_tab_id=self_tab_id,
            self_pane_id=self_pane_id,
        )

    pane_after, accepted = poll_accepted(initial_revision)
    if accepted:
        return pane_after

    # Prompt may have been swallowed by a late startup overlay on `ay`; dismiss and retry once.
    if agent_kind == "agy":
        _dismiss_startup_overlay_if_present(
            pane_id=pane_id,
            agent_kind=agent_kind,
            read_pane=read_pane,
            send_keys=send_keys,
            sleep_fn=sleep_fn,
        )
    retry_rev = pane_after.revision if pane_after is not None else initial_revision
    prompt_fn(pane_id, prompt, wait=False)
    pane_after, accepted = poll_accepted(retry_rev)
    if accepted:
        return pane_after

    raise CliError(
        "PROMPT_NOT_ACCEPTED",
        (
            f"Kickoff prompt was not accepted by {agent_kind or 'agent'} in {pane_id} "
            "(pane remained idle)."
        ),
        f"Inspect pane {pane_id} with `hcmd peek` and resend manually.",
        details={
            "workspace_id": workspace_id,
            "pane_id": pane_id,
            "agent_kind": agent_kind,
        },
    )


def _send_to_pane(
    *,
    workspace_id: str,
    label: str,
    pane: PaneInfo | None,
    is_self: bool,
    prompt: str,
    wait: bool,
    timeout: float,
    force: bool,
    dry_run: bool,
    prompt_fn: PromptAgentFn,
    no_agent_message: str,
    no_agent_fix: str,
    peek_fix: str,
) -> SendResult:
    if not prompt.strip():
        raise CliError(
            "EMPTY_PROMPT",
            "Prompt text cannot be empty.",
            "Pass non-empty text via --prompt or stdin (`--prompt -`).",
            exit_code=EXIT_USAGE,
        )
    if is_self and not force:
        raise CliError(
            "CANNOT_SEND_TO_SELF",
            f"Refusing to send a prompt to the caller's own target ({label}).",
            "Target another tab/pane instead, or pass --force if intentional.",
        )
    if pane is None or pane.agent_kind is None:
        raise CliError("NO_AGENT", no_agent_message, no_agent_fix)
    if pane.effective_status == "blocked":
        raise CliError(
            "AGENT_BLOCKED",
            f"Agent in {label} ({pane.pane_id}) is blocked waiting for approval or input.",
            peek_fix,
            details={"workspace_id": workspace_id, "pane_id": pane.pane_id},
        )
    if pane.effective_status in {"working", "busy_bg"} and not force:
        raise CliError(
            "AGENT_BUSY",
            f"Agent in {label} ({pane.pane_id}) is currently {pane.effective_status}.",
            "Wait for completion first, or pass --force.",
            details={"workspace_id": workspace_id, "pane_id": pane.pane_id},
        )

    if dry_run:
        return SendResult(
            dry_run=True,
            workspace_id=workspace_id,
            label=label,
            pane_id=pane.pane_id,
            agent_kind=pane.agent_kind,
            agent_status=pane.effective_status,
            waited=wait,
            settled_status=None,
            prompt_chars=len(prompt),
        )

    timeout_ms = int(timeout * 1000) if wait else None
    result = prompt_fn(pane.pane_id, prompt, wait=wait, timeout_ms=timeout_ms)
    settled = result.get("agent_status") or result.get("status")
    return SendResult(
        dry_run=False,
        workspace_id=workspace_id,
        label=label,
        pane_id=pane.pane_id,
        agent_kind=pane.agent_kind,
        agent_status=pane.effective_status,
        waited=wait,
        settled_status=str(settled) if isinstance(settled, str) else None,
        prompt_chars=len(prompt),
    )


def verify_tabs_claude_busy(
    tabs: Sequence[WorkspaceTabInfo],
    read_detection: Callable[[str], str],
) -> list[WorkspaceTabInfo]:
    """Verify Claude background activity on each tab's panes and refresh tab agent_status."""
    updated: list[WorkspaceTabInfo] = []
    for tab in tabs:
        verified_panes = tuple(verify_panes_claude_busy(tab.panes, read_detection))
        primary = verified_panes[0] if verified_panes else None
        status = (
            primary.effective_status
            if primary and primary.agent_kind is not None
            else ("shell" if primary is not None else tab.agent_status)
        )
        updated.append(
            replace(tab, primary_pane=primary, panes=verified_panes, agent_status=status)
        )
    return updated


def plan_tab_spawn(
    *,
    caller: CallerContext,
    tabs: Sequence[WorkspaceTabInfo],
    label: str,
    cwd: str,
    launcher_name: str,
    prompt: str | None = None,
    max_sub_tabs: int = DEFAULT_MAX_SUB_TABS,
    max_space_agents: int = DEFAULT_MAX_SPACE_AGENTS,
    max_session_agents: int = DEFAULT_MAX_SESSION_AGENTS,
    session_working_agents: int = 0,
    existing_agent_names: set[str] | frozenset[str] = frozenset(),
    dir_exists: Callable[[str], bool] = os.path.isdir,
) -> TabSpawnPlan:
    """Create a validated plan for spawning a worker sub-tab inside the caller's Space."""
    if caller.role != "space_lead":
        raise CliError(
            "NOT_SPACE_LEAD",
            f"Only a Space Lead (tabs[0], panes[0]) may create tabs; caller is {caller.role}.",
            "Run `hcmd pane spawn` inside a Tab Lead, or report to Tab 1.",
            details=caller.to_dict(),
        )
    clean_label = label.strip()
    if not clean_label:
        raise CliError(
            "INVALID_LABEL",
            "Sub-tab --label cannot be empty.",
            "Pass a short role label such as `--label review` or `--label build`.",
            exit_code=EXIT_USAGE,
        )
    spec = LAUNCHERS.get(launcher_name)
    if spec is None:
        raise CliError(
            "INVALID_LAUNCHER",
            f"Unsupported launcher {launcher_name!r}.",
            f"Choose one of: {', '.join(LAUNCHERS)}.",
            exit_code=EXIT_USAGE,
        )
    if prompt is not None and not spec.launches_agent:
        raise CliError(
            "CONFLICTING_OPTIONS",
            "--prompt cannot be used with `--launcher shell`.",
            "Remove --prompt or choose an agent launcher such as `ai-agent`.",
            exit_code=EXIT_USAGE,
        )

    norm_cwd = os.path.normpath(os.path.abspath(os.path.expanduser(cwd)))
    if not dir_exists(norm_cwd):
        raise CliError(
            "CWD_NOT_FOUND",
            f"Target directory does not exist: {norm_cwd}",
            "Pass an existing directory to --cwd.",
        )

    current_sub_tabs = max(0, len(tabs) - 1)
    if max_sub_tabs > 0 and current_sub_tabs >= max_sub_tabs:
        raise CliError(
            "TAB_LIMIT_REACHED",
            (
                f"Workspace {caller.workspace_id} already has {current_sub_tabs} sub-tab(s) "
                f"(cap: {max_sub_tabs})."
            ),
            "Close finished sub-tabs with `hcmd tab close <tab>` before spawning another.",
            details={"current_sub_tabs": current_sub_tabs, "max_sub_tabs": max_sub_tabs},
        )

    space_agents = count_workspace_non_primary_agents(tabs)
    _validate_agent_quota_caps(
        spec=spec,
        space_agents=space_agents,
        max_space_agents=max_space_agents,
        session_working_agents=session_working_agents,
        max_session_agents=max_session_agents,
    )

    agent_name = (
        sanitize_agent_name(clean_label, existing_agent_names, fallback="worker")
        if spec.launches_agent
        else None
    )
    return TabSpawnPlan(
        workspace_id=caller.workspace_id,
        label=clean_label,
        cwd=norm_cwd,
        launcher=spec.name,
        expected_agent=_single_agent(spec),
        agent_name=agent_name,
        prompt=prompt,
        current_sub_tabs=current_sub_tabs,
        max_sub_tabs=max_sub_tabs,
        space_agents=space_agents,
        max_space_agents=max_space_agents,
        session_working_agents=session_working_agents,
        max_session_agents=max_session_agents,
    )


def execute_tab_spawn(
    plan: TabSpawnPlan,
    *,
    client: SnapshotClient,
    create_tab: CreateTabFn,
    run_pane: RunPaneFn,
    start_agent: StartAgentFn,
    prompt_fn: PromptAgentFn,
    env: Mapping[str, str],
    select_backend: SelectBackendFn | None = None,
    trust_workspace: TrustWorkspaceFn | None = None,
    read_pane: ReadPaneFn | None = None,
    send_keys: SendKeysFn | None = None,
    dry_run: bool = False,
    ready_timeout: float = 45.0,
    poll_interval: float = 0.5,
    verify_timeout: float = 5.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> TabSpawnResult:
    """Execute a validated TabSpawnPlan."""
    spec = LAUNCHERS[plan.launcher]
    predicted_agent = plan.expected_agent
    if spec.selects_backend and select_backend is not None:
        predicted_agent = resolve_backend_agent(select_backend())

    if dry_run:
        return TabSpawnResult(
            dry_run=True,
            workspace_id=plan.workspace_id,
            tab_id=None,
            pane_id=None,
            label=plan.label,
            cwd=plan.cwd,
            launcher=plan.launcher,
            agent_kind=predicted_agent,
            agent_status=None,
            agent_name=plan.agent_name,
            prompt_sent=False,
        )

    created = create_tab(
        plan.workspace_id,
        label=plan.label,
        cwd=plan.cwd,
        env_vars={"HERDR_TAB_ROLE": "worker"},
    )
    tab_id = str(created["tab_id"])
    pane_id = str(created["pane_id"])

    if not spec.launches_agent:
        return TabSpawnResult(
            dry_run=False,
            workspace_id=plan.workspace_id,
            tab_id=tab_id,
            pane_id=pane_id,
            label=plan.label,
            cwd=plan.cwd,
            launcher=plan.launcher,
            agent_kind=None,
            agent_status="shell",
            agent_name=None,
            prompt_sent=False,
        )

    if trust_workspace is not None:
        trust_workspace(plan.cwd, spec.expected_agents, env)

    pane = _launch_agent_in_pane(
        spec=spec,
        workspace_id=plan.workspace_id,
        pane_id=pane_id,
        agent_name=plan.agent_name or sanitize_agent_name(plan.label, fallback="worker"),
        env=env,
        client=client,
        run_pane=run_pane,
        start_agent=start_agent,
        ready_timeout=ready_timeout,
        poll_interval=poll_interval,
        sleep_fn=sleep_fn,
        read_pane=read_pane,
        send_keys=send_keys,
    )
    prompt_sent = False
    if plan.prompt and plan.prompt.strip():
        pane_kind = pane.agent_kind if pane else predicted_agent
        pane = (
            _send_kickoff_prompt_verified(
                workspace_id=plan.workspace_id,
                pane_id=pane_id,
                agent_kind=pane_kind,
                initial_revision=pane.revision if pane else 0,
                prompt=plan.prompt,
                prompt_fn=prompt_fn,
                client=client,
                read_pane=read_pane,
                send_keys=send_keys,
                verify_started=(
                    pane_kind == "agy" and (read_pane is not None or send_keys is not None)
                ),
                verify_timeout=verify_timeout,
                poll_interval=min(0.4, poll_interval) if poll_interval > 0 else 0.4,
                sleep_fn=sleep_fn,
            )
            or pane
        )
        prompt_sent = True

    return TabSpawnResult(
        dry_run=False,
        workspace_id=plan.workspace_id,
        tab_id=tab_id,
        pane_id=pane_id,
        label=plan.label,
        cwd=plan.cwd,
        launcher=plan.launcher,
        agent_kind=pane.agent_kind if pane else predicted_agent,
        agent_status=pane.effective_status if pane else None,
        agent_name=pane.agent_name if pane else plan.agent_name,
        prompt_sent=prompt_sent,
    )


def execute_tab_send(
    *,
    caller: CallerContext,
    tab: WorkspaceTabInfo,
    prompt: str,
    wait: bool,
    timeout: float,
    force: bool,
    dry_run: bool,
    prompt_fn: PromptAgentFn,
) -> SendResult:
    """Validate Space Lead authority and send a prompt to a sub-tab's primary pane."""
    if caller.role != "space_lead":
        raise CliError(
            "NOT_SPACE_LEAD",
            f"Only a Space Lead may send prompts across tabs; caller is {caller.role}.",
            "Report results in your own pane or use `hcmd pane send` if you are a Tab Lead.",
            details=caller.to_dict(),
        )
    return _send_to_pane(
        workspace_id=caller.workspace_id,
        label=f"{tab.base_label} ({tab.tab_id})",
        pane=tab.primary_pane,
        is_self=tab.is_self,
        prompt=prompt,
        wait=wait,
        timeout=timeout,
        force=force,
        dry_run=dry_run,
        prompt_fn=prompt_fn,
        no_agent_message=f"Tab {tab.tab_id} ({tab.base_label}) has no agent in its primary pane.",
        no_agent_fix="Spawn an agent tab or run commands via `herdr pane run`.",
        peek_fix=f"Inspect tab {tab.tab_id} with `hcmd tab peek {tab.tab_id}` first.",
    )


def execute_tab_wait(
    *,
    client: WaitClient,
    caller: CallerContext,
    until: Sequence[str],
    target: str | None,
    timeout: float,
    verify_busy: bool,
) -> TabWaitResult:
    """Wait via Herdr socket events until watched sub-tabs reach target states."""
    start = time.monotonic()
    deadline = start + timeout
    until_tuple = tuple(until)
    until_set = frozenset(until_tuple)

    def evaluate() -> tuple[list[WorkspaceTabInfo], list[WorkspaceTabInfo]]:
        tabs = parse_workspace_tabs(
            client.snapshot(),
            caller.workspace_id,
            self_tab_id=caller.tab_id,
            self_pane_id=caller.pane_id,
        )
        watched = (
            [resolve_tab_target(tabs, target)] if target else [t for t in tabs if not t.is_primary]
        )
        if verify_busy and watched:
            watched = verify_tabs_claude_busy(watched, client.read_detection)
        matched = [
            tab
            for tab in watched
            if (tab.primary_pane.effective_status if tab.primary_pane else tab.agent_status)
            in until_set
            or tab.agent_status in until_set
        ]
        return watched, matched

    watched, matched = evaluate()
    if matched:
        return TabWaitResult(
            workspace_id=caller.workspace_id,
            until=until_tuple,
            matched=tuple(matched),
            elapsed_seconds=time.monotonic() - start,
            from_initial_snapshot=True,
        )

    pane_ids = [pane.pane_id for tab in watched for pane in tab.panes]
    for _event in client.subscribe_events(pane_ids, deadline=deadline):
        watched, matched = evaluate()
        if matched:
            return TabWaitResult(
                workspace_id=caller.workspace_id,
                until=until_tuple,
                matched=tuple(matched),
                elapsed_seconds=time.monotonic() - start,
                from_initial_snapshot=False,
            )

    raise CliError(
        "WAIT_TIMEOUT",
        f"Timed out after {timeout:.0f}s waiting for tab state(s): {', '.join(until_tuple)}.",
        "Run `hcmd tab list` to inspect sub-tabs in this workspace.",
    )


def execute_tab_close(
    *,
    caller: CallerContext,
    tab: WorkspaceTabInfo,
    dry_run: bool,
    close_tab: CloseTabFn,
) -> TabCloseResult:
    """Close a non-primary sub-tab in the caller's Space."""
    if caller.role != "space_lead":
        raise CliError(
            "NOT_SPACE_LEAD",
            f"Only a Space Lead may close tabs; caller is {caller.role}.",
            "Leave tab lifecycle management to Tab 1.",
            details=caller.to_dict(),
        )
    if tab.is_primary:
        raise CliError(
            "CANNOT_CLOSE_PRIMARY_TAB",
            f"Refusing to close primary tab {tab.tab_id} ({tab.base_label}).",
            "Pass a worker sub-tab ID or label instead.",
        )
    if not dry_run:
        close_tab(tab.tab_id)
    return TabCloseResult(
        dry_run=dry_run,
        workspace_id=caller.workspace_id,
        tab_id=tab.tab_id,
        label=tab.base_label,
    )


def choose_split_target(panes: Sequence[PaneInfo], direction: str) -> tuple[str, str]:
    """Select (target_pane_id, direction) for up to a 2x2 worker pane layout."""
    if not panes:
        raise CliError(
            "NO_PRIMARY_PANE",
            "Current tab has no panes to split.",
            "Inspect the tab in Herdr.",
        )
    if direction in {"right", "down"}:
        return panes[0].pane_id, direction
    if len(panes) == 1:
        return panes[0].pane_id, "right"
    if len(panes) == 2:
        return panes[1].pane_id, "down"
    return panes[0].pane_id, "down"


def plan_pane_spawn(
    *,
    caller: CallerContext,
    tabs: Sequence[WorkspaceTabInfo],
    cwd: str,
    launcher_name: str,
    direction: str = "auto",
    shell_cmd: str | None = None,
    prompt: str | None = None,
    max_worker_panes: int = DEFAULT_MAX_WORKER_PANES,
    max_space_agents: int = DEFAULT_MAX_SPACE_AGENTS,
    max_session_agents: int = DEFAULT_MAX_SESSION_AGENTS,
    session_working_agents: int = 0,
    existing_agent_names: set[str] | frozenset[str] = frozenset(),
    dir_exists: Callable[[str], bool] = os.path.isdir,
) -> PaneSpawnPlan:
    """Create a validated plan for splitting a worker pane inside the caller's tab."""
    if caller.role == "pane_worker":
        raise CliError(
            "NOT_TAB_LEAD",
            "Pane workers cannot split new panes.",
            "Report back to this tab's primary pane (panes[0]).",
            details=caller.to_dict(),
        )
    if direction not in VALID_SPLIT_DIRECTIONS:
        raise CliError(
            "INVALID_DIRECTION",
            f"Unsupported split direction {direction!r}.",
            f"Choose one of: {', '.join(VALID_SPLIT_DIRECTIONS)}.",
            exit_code=EXIT_USAGE,
        )
    spec = LAUNCHERS.get(launcher_name)
    if spec is None:
        raise CliError(
            "INVALID_LAUNCHER",
            f"Unsupported launcher {launcher_name!r}.",
            f"Choose one of: {', '.join(LAUNCHERS)}.",
            exit_code=EXIT_USAGE,
        )
    if shell_cmd is not None and spec.launches_agent:
        raise CliError(
            "CONFLICTING_OPTIONS",
            "--cmd can only be used with `--launcher shell`.",
            "Pass `--launcher shell --cmd ...`, or use --prompt for an agent launcher.",
            exit_code=EXIT_USAGE,
        )
    if prompt is not None and not spec.launches_agent:
        raise CliError(
            "CONFLICTING_OPTIONS",
            "--prompt cannot be used with `--launcher shell`.",
            "Remove --prompt or choose an agent launcher such as `ai-agent`.",
            exit_code=EXIT_USAGE,
        )

    norm_cwd = os.path.normpath(os.path.abspath(os.path.expanduser(cwd)))
    if not dir_exists(norm_cwd):
        raise CliError(
            "CWD_NOT_FOUND",
            f"Target directory does not exist: {norm_cwd}",
            "Pass an existing directory to --cwd.",
        )

    caller_tab = next((t for t in tabs if t.tab_id == caller.tab_id), None)
    if caller_tab is None:
        raise CliError(
            "TAB_NOT_FOUND",
            f"Caller tab {caller.tab_id} was not found in workspace {caller.workspace_id}.",
            "Verify the Herdr session state.",
        )

    current_worker_panes = max(0, len(caller_tab.panes) - 1)
    if max_worker_panes > 0 and current_worker_panes >= max_worker_panes:
        raise CliError(
            "PANE_LIMIT_REACHED",
            (
                f"Tab {caller.tab_id} already has {current_worker_panes} worker pane(s) "
                f"(cap: {max_worker_panes})."
            ),
            "Close finished worker panes with `hcmd pane close <pane>` before splitting another.",
            details={
                "current_worker_panes": current_worker_panes,
                "max_worker_panes": max_worker_panes,
            },
        )

    space_agents = count_workspace_non_primary_agents(tabs)
    _validate_agent_quota_caps(
        spec=spec,
        space_agents=space_agents,
        max_space_agents=max_space_agents,
        session_working_agents=session_working_agents,
        max_session_agents=max_session_agents,
    )

    split_target_pane_id, resolved_dir = choose_split_target(caller_tab.panes, direction)
    name_base = f"{caller_tab.base_label}-p{len(caller_tab.panes) + 1}"
    agent_name = (
        sanitize_agent_name(name_base, existing_agent_names, fallback="worker")
        if spec.launches_agent
        else None
    )
    return PaneSpawnPlan(
        workspace_id=caller.workspace_id,
        tab_id=caller.tab_id,
        split_target_pane_id=split_target_pane_id,
        direction=resolved_dir,
        cwd=norm_cwd,
        launcher=spec.name,
        expected_agent=_single_agent(spec),
        agent_name=agent_name,
        shell_cmd=shell_cmd,
        prompt=prompt,
        current_worker_panes=current_worker_panes,
        max_worker_panes=max_worker_panes,
        space_agents=space_agents,
        max_space_agents=max_space_agents,
        session_working_agents=session_working_agents,
        max_session_agents=max_session_agents,
    )


def execute_pane_spawn(
    plan: PaneSpawnPlan,
    *,
    client: SnapshotClient,
    split_pane: SplitPaneFn,
    run_pane: RunPaneFn,
    start_agent: StartAgentFn,
    prompt_fn: PromptAgentFn,
    env: Mapping[str, str],
    select_backend: SelectBackendFn | None = None,
    trust_workspace: TrustWorkspaceFn | None = None,
    read_pane: ReadPaneFn | None = None,
    send_keys: SendKeysFn | None = None,
    dry_run: bool = False,
    ready_timeout: float = 45.0,
    poll_interval: float = 0.5,
    verify_timeout: float = 5.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> PaneSpawnResult:
    """Execute a validated PaneSpawnPlan."""
    spec = LAUNCHERS[plan.launcher]
    predicted_agent = plan.expected_agent
    if spec.selects_backend and select_backend is not None:
        predicted_agent = resolve_backend_agent(select_backend())

    if dry_run:
        return PaneSpawnResult(
            dry_run=True,
            workspace_id=plan.workspace_id,
            tab_id=plan.tab_id,
            pane_id=None,
            split_target_pane_id=plan.split_target_pane_id,
            direction=plan.direction,
            cwd=plan.cwd,
            launcher=plan.launcher,
            agent_kind=predicted_agent,
            agent_status=None,
            agent_name=plan.agent_name,
            shell_cmd=plan.shell_cmd,
            prompt_sent=False,
        )

    created = split_pane(
        plan.split_target_pane_id,
        direction=plan.direction,
        cwd=plan.cwd,
        env_vars={"HERDR_PANE_ROLE": "worker"},
    )
    pane_id = str(created["pane_id"])

    if not spec.launches_agent:
        if plan.shell_cmd and plan.shell_cmd.strip():
            run_pane(pane_id, plan.shell_cmd)
        return PaneSpawnResult(
            dry_run=False,
            workspace_id=plan.workspace_id,
            tab_id=plan.tab_id,
            pane_id=pane_id,
            split_target_pane_id=plan.split_target_pane_id,
            direction=plan.direction,
            cwd=plan.cwd,
            launcher=plan.launcher,
            agent_kind=None,
            agent_status="shell",
            agent_name=None,
            shell_cmd=plan.shell_cmd,
            prompt_sent=False,
        )

    if trust_workspace is not None:
        trust_workspace(plan.cwd, spec.expected_agents, env)

    pane = _launch_agent_in_pane(
        spec=spec,
        workspace_id=plan.workspace_id,
        pane_id=pane_id,
        agent_name=plan.agent_name or sanitize_agent_name("worker", fallback="worker"),
        env=env,
        client=client,
        run_pane=run_pane,
        start_agent=start_agent,
        ready_timeout=ready_timeout,
        poll_interval=poll_interval,
        sleep_fn=sleep_fn,
        read_pane=read_pane,
        send_keys=send_keys,
    )
    prompt_sent = False
    if plan.prompt and plan.prompt.strip():
        pane_kind = pane.agent_kind if pane else predicted_agent
        pane = (
            _send_kickoff_prompt_verified(
                workspace_id=plan.workspace_id,
                pane_id=pane_id,
                agent_kind=pane_kind,
                initial_revision=pane.revision if pane else 0,
                prompt=plan.prompt,
                prompt_fn=prompt_fn,
                client=client,
                read_pane=read_pane,
                send_keys=send_keys,
                verify_started=(
                    pane_kind == "agy" and (read_pane is not None or send_keys is not None)
                ),
                verify_timeout=verify_timeout,
                poll_interval=min(0.4, poll_interval) if poll_interval > 0 else 0.4,
                sleep_fn=sleep_fn,
            )
            or pane
        )
        prompt_sent = True

    return PaneSpawnResult(
        dry_run=False,
        workspace_id=plan.workspace_id,
        tab_id=plan.tab_id,
        pane_id=pane_id,
        split_target_pane_id=plan.split_target_pane_id,
        direction=plan.direction,
        cwd=plan.cwd,
        launcher=plan.launcher,
        agent_kind=pane.agent_kind if pane else predicted_agent,
        agent_status=pane.effective_status if pane else None,
        agent_name=pane.agent_name if pane else plan.agent_name,
        shell_cmd=None,
        prompt_sent=prompt_sent,
    )


def execute_pane_send(
    *,
    caller: CallerContext,
    pane: PaneInfo,
    prompt: str,
    wait: bool,
    timeout: float,
    force: bool,
    dry_run: bool,
    prompt_fn: PromptAgentFn,
) -> SendResult:
    """Validate Tab Lead authority and send a prompt to a worker pane inside the same tab."""
    if caller.role == "pane_worker":
        raise CliError(
            "NOT_TAB_LEAD",
            "Pane workers cannot send prompts to sibling panes.",
            "Report back to this tab's primary pane (panes[0]).",
            details=caller.to_dict(),
        )
    return _send_to_pane(
        workspace_id=caller.workspace_id,
        label=pane.pane_id,
        pane=pane,
        is_self=pane.is_self,
        prompt=prompt,
        wait=wait,
        timeout=timeout,
        force=force,
        dry_run=dry_run,
        prompt_fn=prompt_fn,
        no_agent_message=f"Pane {pane.pane_id} has no running agent.",
        no_agent_fix=f"Use `herdr pane run {pane.pane_id} ...` for shell panes.",
        peek_fix=f"Inspect pane {pane.pane_id} with `hcmd pane peek {pane.pane_id}` first.",
    )


def execute_pane_wait(
    *,
    client: WaitClient,
    caller: CallerContext,
    until: Sequence[str],
    target: str | None,
    timeout: float,
    verify_busy: bool,
) -> PaneWaitResult:
    """Wait via Herdr socket events until watched panes in the caller's tab reach target states."""
    start = time.monotonic()
    deadline = start + timeout
    until_tuple = tuple(until)
    until_set = frozenset(until_tuple)

    def evaluate() -> tuple[list[PaneInfo], list[PaneInfo]]:
        tabs = parse_workspace_tabs(
            client.snapshot(),
            caller.workspace_id,
            self_tab_id=caller.tab_id,
            self_pane_id=caller.pane_id,
        )
        tab = next((t for t in tabs if t.tab_id == caller.tab_id), None)
        if tab is None:
            raise CliError(
                "TAB_NOT_FOUND",
                f"Caller tab {caller.tab_id} was not found in workspace {caller.workspace_id}.",
                "Verify the Herdr session state.",
            )
        watched = (
            [resolve_pane_target(tab.panes, target)]
            if target
            else [p for p in tab.panes if not p.is_primary]
        )
        if verify_busy and watched:
            watched = verify_panes_claude_busy(watched, client.read_detection)
        matched = [p for p in watched if p.effective_status in until_set]
        return watched, matched

    watched, matched = evaluate()
    if matched:
        return PaneWaitResult(
            workspace_id=caller.workspace_id,
            tab_id=caller.tab_id,
            until=until_tuple,
            matched=tuple(matched),
            elapsed_seconds=time.monotonic() - start,
            from_initial_snapshot=True,
        )

    pane_ids = [p.pane_id for p in watched]
    for _event in client.subscribe_events(pane_ids, deadline=deadline):
        watched, matched = evaluate()
        if matched:
            return PaneWaitResult(
                workspace_id=caller.workspace_id,
                tab_id=caller.tab_id,
                until=until_tuple,
                matched=tuple(matched),
                elapsed_seconds=time.monotonic() - start,
                from_initial_snapshot=False,
            )

    raise CliError(
        "WAIT_TIMEOUT",
        f"Timed out after {timeout:.0f}s waiting for pane state(s): {', '.join(until_tuple)}.",
        "Run `hcmd pane list` to inspect panes in this tab.",
    )


def execute_pane_close(
    *,
    caller: CallerContext,
    pane: PaneInfo,
    dry_run: bool,
    close_pane: ClosePaneFn,
) -> PaneCloseResult:
    """Close a non-primary worker pane inside the caller's tab."""
    if caller.role == "pane_worker":
        raise CliError(
            "NOT_TAB_LEAD",
            "Pane workers cannot close sibling panes.",
            "Report back to this tab's primary pane (panes[0]).",
            details=caller.to_dict(),
        )
    if pane.is_primary:
        raise CliError(
            "CANNOT_CLOSE_PRIMARY_PANE",
            f"Refusing to close primary pane {pane.pane_id} of tab {caller.tab_id}.",
            "Pass a worker pane ID or index instead.",
        )
    if not dry_run:
        close_pane(pane.pane_id)
    return PaneCloseResult(
        dry_run=dry_run,
        workspace_id=caller.workspace_id,
        tab_id=caller.tab_id,
        pane_id=pane.pane_id,
    )


def normalize_peek_text(text: str) -> str:
    """Normalize pane output by removing ANSI escapes and volatile footer/chrome lines."""
    cleaned = strip_ansi(text)
    lines = [line.rstrip() for line in cleaned.splitlines()]
    lines = [line for line in lines if not _STATUSLINE_FOOTER_RE.search(line)]
    while lines and (
        not lines[-1].strip()
        or _BOX_RULE_LINE_RE.match(lines[-1])
        or _EMPTY_PROMPT_LINE_RE.match(lines[-1])
    ):
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines)


def hash_peek_text(text: str) -> str:
    """Return deterministic 16-hex digest of normalized pane output."""
    normalized = normalize_peek_text(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def compact_peek_text(text: str, *, max_chars: int | None) -> tuple[str, bool]:
    """Extract clean reporting tail (if present) and bound length to `max_chars`."""
    normalized = normalize_peek_text(text)
    if not normalized:
        return "", False

    lines = normalized.splitlines()
    # Prefer the structured Commander reporting block near the tail when present.
    for idx in range(len(lines) - 1, -1, -1):
        if _REPORT_STATUS_TAIL_RE.match(lines[idx]):
            tail_block = "\n".join(lines[idx:])
            if any(key in tail_block for key in ("Summary:", "Evidence:", "Decision:", "Blockers")):
                normalized = tail_block
            break

    if max_chars is not None and max_chars > 0 and len(normalized) > max_chars:
        prefix = "...(truncated)...\n"
        if max_chars <= len(prefix) + 8:
            return normalized[-max_chars:], True
        keep = max_chars - len(prefix)
        return prefix + normalized[-keep:], True
    return normalized, False


def _make_digest_peek(
    *,
    tab_id: str,
    tab_label: str,
    pane_id: str,
    agent_kind: str | None,
    agent_status: str,
    is_primary: bool,
    lines: int,
    raw_text: str,
    compact: bool,
    max_chars: int | None,
) -> DigestPeek:
    if compact or max_chars is not None:
        text, truncated = compact_peek_text(raw_text, max_chars=max_chars)
    else:
        text, truncated = raw_text, False
    return DigestPeek(
        tab_id=tab_id,
        tab_label=tab_label,
        pane_id=pane_id,
        agent_kind=agent_kind,
        agent_status=agent_status,
        is_primary=is_primary,
        lines=lines,
        text=text,
        truncated=truncated,
    )


def execute_digest(
    spaces: Sequence[SpaceSnapshot],
    *,
    read_pane: ReadPaneFn,
    include_all: bool = False,
    workspace: str | None = None,
    peek_lines: int = 40,
    peek_source: str = "recent-unwrapped",
) -> tuple[DigestResult, list[str]]:
    """Build a non-blocking status digest and harvest attention/target pane snippets."""
    result, _cursor, warnings = execute_digest_with_cursor(
        spaces,
        read_pane=read_pane,
        include_all=include_all,
        workspace=workspace,
        peek_lines=peek_lines,
        peek_source=peek_source,
        since_last=False,
    )
    return result, warnings


def execute_digest_with_cursor(
    spaces: Sequence[SpaceSnapshot],
    *,
    read_pane: ReadPaneFn,
    include_all: bool = False,
    include_idle: bool = False,
    workspace: str | None = None,
    peek_lines: int = 40,
    peek_source: str = "recent-unwrapped",
    compact: bool = False,
    max_peek_chars: int | None = None,
    since_last: bool = False,
    prev_cursor: DigestCursor | None = None,
    cursor_scope: str = "default",
    now: float | None = None,
) -> tuple[DigestResult, DigestCursor, list[str]]:
    """Build a non-blocking digest with optional delta cursor deduplication and compact peeks."""
    counts: dict[str, int] = {}
    sub_tabs_blocked_count = 0
    for space in spaces:
        counts[space.attention] = counts.get(space.attention, 0) + 1
        if space.sub_tabs.any_blocked:
            sub_tabs_blocked_count += 1

    active_work = has_active_work(spaces)
    max_chars = (
        max_peek_chars
        if max_peek_chars is not None
        else (DEFAULT_MAX_PEEK_CHARS if compact else None)
    )
    prev_map = prev_cursor.entries_map() if prev_cursor is not None else {}

    entries: list[DigestEntry] = []
    warnings: list[str] = []
    still_blocked: list[dict[str, str]] = []
    next_cursor_entries: dict[str, DigestCursorEntry] = {}

    for space in spaces:
        pane = space.primary_pane
        curr_pane_id = pane.pane_id if pane else None
        curr_agent_kind = pane.agent_kind if pane else None
        curr_raw = pane.raw_status if pane else "unknown"
        curr_eff = pane.effective_status if pane else space.attention
        curr_rev = pane.revision if pane else 0
        is_idle_agent = space.attention == "idle" and curr_agent_kind is not None
        is_settled_agent = space.attention in {"blocked", "done"} or is_idle_agent

        prev = prev_map.get(space.workspace_id)
        cached_primary_raw: str | None = None
        curr_peek_hash = (
            prev.peek_hash
            if (
                prev is not None
                and prev.primary_pane_id == curr_pane_id
                and prev.revision == curr_rev
            )
            else None
        )

        # Track sub-tab blocked changes and hashes.
        prev_sub_map = {pid: (rev, ph) for pid, rev, ph in (prev.blocked_subtabs if prev else ())}
        curr_blocked_subtabs: list[tuple[str, int, str | None]] = []
        subtab_reads: list[tuple[dict[str, Any], str | None]] = []

        if since_last:
            primary_reason: str | None = None
            if prev_cursor is None:
                if (
                    space.attention in {"blocked", "done"}
                    or space.sub_tabs.any_blocked
                    or (include_idle and is_idle_agent)
                ):
                    primary_reason = "initial"
            elif prev is None:
                if (
                    space.attention in {"blocked", "done"}
                    or space.sub_tabs.any_blocked
                    or (include_idle and is_idle_agent)
                ):
                    primary_reason = "new_workspace"
            else:
                if space.attention == "blocked" and prev.attention != "blocked":
                    primary_reason = "newly_blocked"
                elif space.attention == "done" and prev.attention != "done":
                    primary_reason = "settled_done"
                elif is_idle_agent and prev.effective_status in {
                    "working",
                    "busy_bg",
                    "unknown",
                    "blocked",
                }:
                    primary_reason = "settled_idle"
                elif curr_pane_id != prev.primary_pane_id and is_settled_agent:
                    primary_reason = "pane_changed"
                elif (
                    is_settled_agent
                    and curr_rev != prev.revision
                    and pane is not None
                    and peek_lines > 0
                ):
                    try:
                        read = read_pane(
                            pane.pane_id,
                            has_agent=pane.agent_kind is not None,
                            source=peek_source,
                            lines=peek_lines,
                        )
                        raw_text = str(read["text"])
                        curr_peek_hash = hash_peek_text(raw_text)
                        if prev.peek_hash is None or curr_peek_hash != prev.peek_hash:
                            primary_reason = "output_updated"
                            cached_primary_raw = raw_text
                    except CliError as exc:
                        warnings.append(f"{space.workspace_id} ({pane.pane_id}): {exc.message}")

            for bp in space.sub_tabs.blocked_panes:
                bp_pid = str(bp["pane_id"])
                bp_rev = int(bp.get("revision") or 0)
                prev_sub = prev_sub_map.get(bp_pid)
                if prev_cursor is None or prev_sub is None:
                    subtab_reads.append((bp, None))
                elif bp_rev != prev_sub[0] and peek_lines > 0:
                    try:
                        read = read_pane(
                            bp_pid,
                            has_agent=True,
                            source=peek_source,
                            lines=peek_lines,
                        )
                        bp_raw = str(read["text"])
                        bp_hash = hash_peek_text(bp_raw)
                        if prev_sub[1] is None or bp_hash != prev_sub[1]:
                            subtab_reads.append((bp, bp_raw))
                        else:
                            curr_blocked_subtabs.append((bp_pid, bp_rev, prev_sub[1]))
                    except CliError as exc:
                        warnings.append(f"{space.workspace_id} ({bp_pid}): {exc.message}")
                        curr_blocked_subtabs.append((bp_pid, bp_rev, prev_sub[1]))
                else:
                    curr_blocked_subtabs.append((bp_pid, bp_rev, prev_sub[1]))

            if primary_reason is not None:
                change_reason: str | None = primary_reason
            elif subtab_reads:
                change_reason = "initial" if prev_cursor is None else "subtab_blocked"
            else:
                change_reason = None

            needs_attention = (
                space.attention in {"blocked", "done"}
                or space.sub_tabs.any_blocked
                or (include_idle and is_idle_agent)
            )
            include_entry = change_reason is not None or include_all or workspace is not None
            if not include_entry and (space.attention == "blocked" or space.sub_tabs.any_blocked):
                still_blocked.append(
                    {
                        "workspace_id": space.workspace_id,
                        "label": space.label,
                        "reason": (
                            "primary_blocked" if space.attention == "blocked" else "subtab_blocked"
                        ),
                    }
                )
        else:
            change_reason = None
            needs_attention = (
                space.attention in {"blocked", "done"}
                or space.sub_tabs.any_blocked
                or (include_idle and is_idle_agent)
            )
            include_entry = needs_attention or include_all or workspace is not None
            subtab_reads = [(bp, None) for bp in space.sub_tabs.blocked_panes]

        peeks: list[DigestPeek] = []
        if include_entry and peek_lines > 0:
            should_peek_primary = pane is not None and (
                (
                    not since_last
                    and (
                        space.attention in {"blocked", "done"}
                        or (include_idle and is_idle_agent)
                        or workspace is not None
                    )
                )
                or (
                    since_last
                    and change_reason is not None
                    and (
                        space.attention in {"blocked", "done"}
                        or is_idle_agent
                        or workspace is not None
                    )
                )
            )
            if should_peek_primary and pane is not None:
                try:
                    if cached_primary_raw is None:
                        read = read_pane(
                            pane.pane_id,
                            has_agent=pane.agent_kind is not None,
                            source=peek_source,
                            lines=peek_lines,
                        )
                        cached_primary_raw = str(read["text"])
                    curr_peek_hash = hash_peek_text(cached_primary_raw)
                    peeks.append(
                        _make_digest_peek(
                            tab_id=space.primary_tab_id or "",
                            tab_label=strip_tab_decorations(space.primary_tab_label or "Tab 1"),
                            pane_id=pane.pane_id,
                            agent_kind=pane.agent_kind,
                            agent_status=pane.effective_status,
                            is_primary=True,
                            lines=peek_lines,
                            raw_text=cached_primary_raw,
                            compact=compact,
                            max_chars=max_chars,
                        )
                    )
                except CliError as exc:
                    warnings.append(f"{space.workspace_id} ({pane.pane_id}): {exc.message}")

            for bp, pre_raw in subtab_reads:
                bp_pane_id = str(bp["pane_id"])
                bp_rev = int(bp.get("revision") or 0)
                try:
                    if pre_raw is None:
                        read = read_pane(
                            bp_pane_id,
                            has_agent=True,
                            source=peek_source,
                            lines=peek_lines,
                        )
                        bp_raw = str(read["text"])
                    else:
                        bp_raw = pre_raw
                    curr_blocked_subtabs.append((bp_pane_id, bp_rev, hash_peek_text(bp_raw)))
                    peeks.append(
                        _make_digest_peek(
                            tab_id=str(bp["tab_id"]),
                            tab_label=str(bp["tab_label"]),
                            pane_id=bp_pane_id,
                            agent_kind=None,
                            agent_status="blocked",
                            is_primary=False,
                            lines=peek_lines,
                            raw_text=bp_raw,
                            compact=compact,
                            max_chars=max_chars,
                        )
                    )
                except CliError as exc:
                    warnings.append(f"{space.workspace_id} ({bp_pane_id}): {exc.message}")
                    curr_blocked_subtabs.append((bp_pane_id, bp_rev, None))
        elif since_last:
            for bp, pre_raw in subtab_reads:
                bp_pane_id = str(bp["pane_id"])
                bp_rev = int(bp.get("revision") or 0)
                bp_hash = hash_peek_text(pre_raw) if pre_raw is not None else None
                curr_blocked_subtabs.append((bp_pane_id, bp_rev, bp_hash))

        # Deduplicate & sort blocked_subtabs in cursor entry.
        subtab_by_pid = {pid: (pid, rev, ph) for pid, rev, ph in curr_blocked_subtabs}
        next_cursor_entries[space.workspace_id] = DigestCursorEntry(
            workspace_id=space.workspace_id,
            label=space.label,
            primary_pane_id=curr_pane_id,
            agent_kind=curr_agent_kind,
            raw_status=curr_raw,
            effective_status=curr_eff,
            attention=space.attention,
            revision=curr_rev,
            peek_hash=curr_peek_hash,
            blocked_subtabs=tuple(sorted(subtab_by_pid.values(), key=lambda t: t[0])),
            deleted_hint=space.deleted_hint,
        )

        if include_entry:
            entries.append(
                DigestEntry(
                    space=space,
                    needs_attention=needs_attention,
                    peeks=tuple(peeks),
                    change_reason=change_reason,
                )
            )

    closed_workspaces: list[dict[str, str]] = []
    if since_last and prev_cursor is not None:
        current_ids = {s.workspace_id for s in spaces}
        for wid, prev_entry in sorted(prev_map.items()):
            if wid not in current_ids:
                closed_workspaces.append({"workspace_id": wid, "label": prev_entry.label})

    changed_flag: bool | None = None
    if since_last:
        changed_flag = bool(
            any(entry.change_reason is not None for entry in entries) or closed_workspaces
        )

    next_cursor = DigestCursor(
        schema_version=1,
        scope=cursor_scope,
        updated_at=now if now is not None else time.time(),
        workspaces=next_cursor_entries,
    )
    return (
        DigestResult(
            total_workspaces=len(spaces),
            counts=counts,
            sub_tabs_blocked_count=sub_tabs_blocked_count,
            entries=tuple(entries),
            peek_lines=peek_lines,
            include_all=include_all,
            has_active_work=active_work,
            since_last=since_last,
            changed=changed_flag,
            compact=compact,
            cursor_scope=cursor_scope if since_last else None,
            still_blocked=tuple(still_blocked),
            closed_workspaces=tuple(closed_workspaces),
        ),
        next_cursor,
        warnings,
    )


def render_handoff_markdown(payload: Mapping[str, Any]) -> str:
    """Render a compact human- and agent-readable Markdown handoff note."""
    ws_id = payload.get("workspace_id", "")
    tab_id = payload.get("tab_id", "")
    old_pane = payload.get("old_pane_id", "")
    new_pane = payload.get("new_pane_id", "")
    gen = payload.get("generation", 1)
    cursor_name = payload.get("cursor_name") or "(auto)"
    rotate_ctx = payload.get("rotate_ctx", DEFAULT_ROTATE_CTX)
    auto_config = payload.get("auto_config") or "default"
    decisions = payload.get("pending_decisions") or []
    note = payload.get("note") or ""
    audit_tail = payload.get("recent_audit") or []

    lines = [
        f"# Herdr Commander Handoff — {ws_id} (Generation {gen})",
        "",
        f"- **Workspace / Tab**: `{ws_id}` / `{tab_id}`",
        f"- **Retiring Pane (`--ack`)**: `{old_pane}`",
        f"- **Successor Pane**: `{new_pane}`",
        f"- **Digest Cursor**: `{cursor_name}`",
        f"- **Auto Config**: `{auto_config}` (`--rotate-ctx {rotate_ctx}`)",
        "",
        "## Resume Checklist",
        "",
        (
            f"1. Run `hcmd digest --since-last --no-advance --compact --json` "
            f"(or `--cursor {cursor_name}` if named)."
        ),
        (
            "2. Complete `/herdr-commander-init` (or `/hcmd-init`) reconciliation, "
            f"then retire the old pane with `hcmd rotate --ack {old_pane} --json`."
        ),
        f"3. Resume `/hcmd-auto` with `--rotate-ctx {rotate_ctx}`.",
    ]
    if note:
        lines.extend(["", "## Commander Note", "", str(note)])
    lines.extend(["", "## Pending Human Decisions", ""])
    if decisions:
        lines.extend(f"- {item}" for item in decisions)
    else:
        lines.append("- None recorded.")
    lines.extend(["", "## Recent Auto-Audit Tail", ""])
    if audit_tail:
        lines.extend(["```jsonl", *(str(line) for line in audit_tail), "```"])
    else:
        lines.append("- No recent audit entries.")
    return "\n".join(lines) + "\n"


def build_rotate_bootstrap_prompt(
    *,
    handoff_md_path: str,
    old_pane_id: str,
    cursor_name: str | None,
    rotate_ctx: int,
) -> str:
    """Build the successor pane's bootstrap prompt for 2-phase same-tab rotation."""
    cursor_hint = f" --cursor {cursor_name}" if cursor_name else ""
    return (
        f"Commander context rotation handoff: read `{handoff_md_path}` first.\n"
        "1. Follow `/herdr-commander-init` (alias `/hcmd-init`) handoff resume procedure.\n"
        f"2. Run `hcmd digest --since-last --no-advance{cursor_hint} --compact --json` "
        "to inspect live Spaces without dropping deltas.\n"
        "3. ONLY AFTER init completes cleanly, retire the previous pane with "
        f"`hcmd rotate --ack {old_pane_id} --json`.\n"
        f"4. Resume `/hcmd-auto` monitoring (context rotation threshold "
        f"`--rotate-ctx {rotate_ctx}`{cursor_hint})."
    )


@dataclass(frozen=True)
class RotatePlan:
    """Validated plan for initiating a 2-phase same-tab pane rotation."""

    workspace_id: str
    tab_id: str
    old_pane_id: str
    cwd: str
    launcher: str
    direction: str
    rotate_ctx: int
    cooldown: float
    generation: int
    cursor_name: str | None
    auto_config: str | None
    pending_decisions: tuple[str, ...]
    note: str | None


def plan_rotate(
    *,
    caller: CallerContext,
    tabs: Sequence[WorkspaceTabInfo],
    launcher_name: str = "ay",
    direction: str = "right",
    rotate_ctx: int = DEFAULT_ROTATE_CTX,
    cooldown: float = DEFAULT_ROTATE_COOLDOWN,
    prev_handoff: Mapping[str, Any] | None = None,
    cursor_name: str | None = None,
    auto_config: str | None = None,
    pending_decisions: Sequence[str] = (),
    note: str | None = None,
    force: bool = False,
    now: float | None = None,
) -> RotatePlan:
    """Validate authority, launcher, direction, and cooldown before same-tab rotation."""
    if caller.role == "pane_worker" and not force:
        raise CliError(
            "NOT_TAB_LEAD",
            "Pane workers cannot initiate pane rotation without `--force`.",
            "Run `hcmd rotate` from the primary Commander pane (`panes[0]`).",
            details=caller.to_dict(),
        )
    spec = LAUNCHERS.get(launcher_name)
    if spec is None or not spec.launches_agent:
        valid = ", ".join(k for k, v in LAUNCHERS.items() if v.launches_agent)
        raise CliError(
            "INVALID_LAUNCHER",
            f"Rotation requires an AI agent launcher, got {launcher_name!r}.",
            f"Choose one of: {valid}.",
            exit_code=EXIT_USAGE,
        )
    if direction not in VALID_SPLIT_DIRECTIONS:
        raise CliError(
            "INVALID_DIRECTION",
            f"Unsupported split direction {direction!r}.",
            f"Choose one of: {', '.join(VALID_SPLIT_DIRECTIONS)}.",
            exit_code=EXIT_USAGE,
        )
    if not (1 <= rotate_ctx <= 100):
        raise CliError(
            "INVALID_ROTATE_CTX",
            f"--rotate-ctx must be between 1 and 100, got {rotate_ctx}.",
            "Pass e.g. `--rotate-ctx 30`.",
            exit_code=EXIT_USAGE,
        )
    if cooldown < 0:
        raise CliError(
            "INVALID_COOLDOWN",
            f"--cooldown cannot be negative, got {cooldown}.",
            "Pass 0 or a positive number of seconds (default: 300).",
            exit_code=EXIT_USAGE,
        )

    current_time = now if now is not None else time.time()
    prev_gen = 0
    if prev_handoff is not None:
        prev_gen = int(prev_handoff.get("generation") or 0)
        last_ts = float(prev_handoff.get("created_at") or 0.0)
        elapsed = current_time - last_ts
        if not force and cooldown > 0 and last_ts > 0 and elapsed < cooldown:
            rem = cooldown - elapsed
            raise CliError(
                "ROTATE_COOLDOWN_ACTIVE",
                (
                    f"Rotation cooldown active ({rem:.0f}s remaining of {cooldown:.0f}s); "
                    "refusing potential rotation loop."
                ),
                "Wait for cooldown to expire or pass `--force` to override.",
                details={
                    "cooldown_remaining_seconds": round(rem, 1),
                    "cooldown_seconds": cooldown,
                    "generation": prev_gen,
                    "last_rotated_at": last_ts,
                },
            )

    caller_tab = next((t for t in tabs if t.tab_id == caller.tab_id), None)
    caller_pane = (
        next((p for p in caller_tab.panes if p.pane_id == caller.pane_id), None)
        if caller_tab
        else None
    )
    cwd = caller_pane.cwd if caller_pane and caller_pane.cwd else os.getcwd()
    resolved_dir = "right" if direction == "auto" else direction

    return RotatePlan(
        workspace_id=caller.workspace_id,
        tab_id=caller.tab_id,
        old_pane_id=caller.pane_id,
        cwd=cwd,
        launcher=spec.name,
        direction=resolved_dir,
        rotate_ctx=rotate_ctx,
        cooldown=cooldown,
        generation=prev_gen + 1,
        cursor_name=cursor_name,
        auto_config=auto_config,
        pending_decisions=tuple(d for d in pending_decisions if d.strip()),
        note=note,
    )


def execute_rotate(
    plan: RotatePlan,
    *,
    client: SnapshotClient,
    split_pane: SplitPaneFn,
    run_pane: RunPaneFn,
    start_agent: StartAgentFn,
    prompt_fn: PromptAgentFn,
    env: Mapping[str, str],
    handoff_json_path: str,
    handoff_md_path: str,
    save_handoff: Callable[[Mapping[str, Any], str], None],
    recent_audit: Sequence[str] = (),
    select_backend: SelectBackendFn | None = None,
    trust_workspace: TrustWorkspaceFn | None = None,
    read_pane: ReadPaneFn | None = None,
    send_keys: SendKeysFn | None = None,
    dry_run: bool = False,
    ready_timeout: float = 45.0,
    verify_timeout: float = 10.0,
    poll_interval: float = 0.5,
    sleep_fn: Callable[[float], None] = time.sleep,
    now: float | None = None,
) -> RotateResult:
    """Split caller tab, launch successor agent, save handoff, and verify startup prompt."""
    spec = LAUNCHERS[plan.launcher]
    predicted_agent = _single_agent(spec)
    if spec.selects_backend and select_backend is not None:
        predicted_agent = resolve_backend_agent(select_backend())

    if dry_run:
        return RotateResult(
            mode="rotate",
            dry_run=True,
            workspace_id=plan.workspace_id,
            tab_id=plan.tab_id,
            old_pane_id=plan.old_pane_id,
            new_pane_id=None,
            launcher=plan.launcher,
            agent_kind=predicted_agent,
            agent_status=None,
            generation=plan.generation,
            status="dry_run",
            handoff_json_path=handoff_json_path,
            handoff_md_path=handoff_md_path,
            old_pane_closed=False,
            prompt_sent=False,
        )

    created = split_pane(
        plan.old_pane_id,
        direction=plan.direction,
        cwd=plan.cwd,
        env_vars=None,
    )
    new_pane_id = str(created["pane_id"])

    if trust_workspace is not None and plan.cwd:
        trust_workspace(plan.cwd, spec.expected_agents, env)

    agent_name = sanitize_agent_name(
        f"cmd-g{plan.generation}",
        collect_live_agent_names(client.snapshot()),
        fallback="commander",
    )
    pane = _launch_agent_in_pane(
        spec=spec,
        workspace_id=plan.workspace_id,
        pane_id=new_pane_id,
        agent_name=agent_name,
        env=env,
        client=client,
        run_pane=run_pane,
        start_agent=start_agent,
        ready_timeout=ready_timeout,
        poll_interval=poll_interval,
        sleep_fn=sleep_fn,
        read_pane=read_pane,
        send_keys=send_keys,
    )

    created_ts = now if now is not None else time.time()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": "pending_ack",
        "workspace_id": plan.workspace_id,
        "tab_id": plan.tab_id,
        "old_pane_id": plan.old_pane_id,
        "new_pane_id": new_pane_id,
        "launcher": plan.launcher,
        "generation": plan.generation,
        "created_at": round(created_ts, 3),
        "cursor_name": plan.cursor_name,
        "rotate_ctx": plan.rotate_ctx,
        "auto_config": plan.auto_config,
        "pending_decisions": list(plan.pending_decisions),
        "note": plan.note,
        "recent_audit": list(recent_audit),
    }
    markdown = render_handoff_markdown(payload)
    save_handoff(payload, markdown)

    pane_kind = pane.agent_kind if pane else predicted_agent
    bootstrap_prompt = build_rotate_bootstrap_prompt(
        handoff_md_path=handoff_md_path,
        old_pane_id=plan.old_pane_id,
        cursor_name=plan.cursor_name,
        rotate_ctx=plan.rotate_ctx,
    )
    pane = (
        _send_kickoff_prompt_verified(
            workspace_id=plan.workspace_id,
            pane_id=new_pane_id,
            agent_kind=pane_kind,
            initial_revision=pane.revision if pane else 0,
            prompt=bootstrap_prompt,
            prompt_fn=prompt_fn,
            client=client,
            read_pane=read_pane,
            send_keys=send_keys,
            verify_started=True,
            verify_timeout=verify_timeout,
            poll_interval=min(0.4, poll_interval) if poll_interval > 0 else 0.4,
            sleep_fn=sleep_fn,
        )
        or pane
    )

    return RotateResult(
        mode="rotate",
        dry_run=False,
        workspace_id=plan.workspace_id,
        tab_id=plan.tab_id,
        old_pane_id=plan.old_pane_id,
        new_pane_id=new_pane_id,
        launcher=plan.launcher,
        agent_kind=pane_kind,
        agent_status=pane.effective_status if pane else None,
        generation=plan.generation,
        status="pending_ack",
        handoff_json_path=handoff_json_path,
        handoff_md_path=handoff_md_path,
        old_pane_closed=False,
        prompt_sent=True,
    )


def execute_rotate_ack(
    *,
    caller: CallerContext,
    tabs: Sequence[WorkspaceTabInfo],
    ack_pane_id: str,
    prev_handoff: Mapping[str, Any] | None,
    handoff_json_path: str,
    handoff_md_path: str,
    save_handoff: Callable[[Mapping[str, Any], str], None],
    close_pane: ClosePaneFn,
    force: bool = False,
    dry_run: bool = False,
    now: float | None = None,
) -> RotateResult:
    """Complete 2-phase rotation by verifying successor authority and closing old pane."""
    clean_ack = ack_pane_id.strip()
    if not clean_ack:
        raise CliError(
            "INVALID_TARGET",
            "--ack pane ID cannot be empty.",
            "Pass the retiring pane ID from the handoff file, e.g. `hcmd rotate --ack w1:p1`.",
            exit_code=EXIT_USAGE,
        )
    if clean_ack == caller.pane_id:
        raise CliError(
            "CANNOT_ACK_SELF",
            f"Refusing to ACK/close the calling pane itself ({caller.pane_id}).",
            "Run `hcmd rotate --ack <old_pane_id>` from the newly launched successor pane.",
        )

    if not force:
        if prev_handoff is None:
            raise CliError(
                "HANDOFF_NOT_FOUND",
                f"No rotation handoff record found for workspace {caller.workspace_id}.",
                "Check the handoff directory or pass `--force` if manual cleanup is intentional.",
            )
        if (
            prev_handoff.get("workspace_id") != caller.workspace_id
            or prev_handoff.get("tab_id") != caller.tab_id
            or prev_handoff.get("old_pane_id") != clean_ack
            or prev_handoff.get("new_pane_id") != caller.pane_id
        ):
            raise CliError(
                "HANDOFF_MISMATCH",
                (
                    f"Handoff record expects successor {prev_handoff.get('new_pane_id')!r} "
                    f"to retire {prev_handoff.get('old_pane_id')!r} "
                    f"in {prev_handoff.get('tab_id')!r}; "
                    f"caller is {caller.pane_id!r} retiring {clean_ack!r}."
                ),
                (
                    "Run `--ack` from the designated successor pane with the matching old pane ID, "
                    "or pass `--force`."
                ),
                details={
                    "expected": {
                        "workspace_id": prev_handoff.get("workspace_id"),
                        "tab_id": prev_handoff.get("tab_id"),
                        "old_pane_id": prev_handoff.get("old_pane_id"),
                        "new_pane_id": prev_handoff.get("new_pane_id"),
                    },
                    "caller": caller.to_dict(),
                },
            )

    caller_tab = next((t for t in tabs if t.tab_id == caller.tab_id), None)
    target_exists = bool(
        caller_tab is not None and any(p.pane_id == clean_ack for p in caller_tab.panes)
    )
    generation = int((prev_handoff or {}).get("generation") or 1)
    launcher = (prev_handoff or {}).get("launcher")

    old_pane_closed = False
    if not dry_run:
        if target_exists:
            close_pane(clean_ack)
            old_pane_closed = True
        updated_payload = dict(prev_handoff or {})
        updated_payload.update(
            {
                "schema_version": 1,
                "status": "completed",
                "workspace_id": caller.workspace_id,
                "tab_id": caller.tab_id,
                "old_pane_id": clean_ack,
                "new_pane_id": caller.pane_id,
                "generation": generation,
                "acked_at": round(now if now is not None else time.time(), 3),
            }
        )
        save_handoff(updated_payload, render_handoff_markdown(updated_payload))

    return RotateResult(
        mode="ack",
        dry_run=dry_run,
        workspace_id=caller.workspace_id,
        tab_id=caller.tab_id,
        old_pane_id=clean_ack,
        new_pane_id=caller.pane_id,
        launcher=str(launcher) if isinstance(launcher, str) else None,
        agent_kind=None,
        agent_status=None,
        generation=generation,
        status="dry_run" if dry_run else "completed",
        handoff_json_path=handoff_json_path,
        handoff_md_path=handoff_md_path,
        old_pane_closed=old_pane_closed,
        prompt_sent=False,
    )


def collect_tab_spawn_inputs(
    raw: dict[str, Any],
    *,
    caller: CallerContext,
) -> tuple[list[WorkspaceTabInfo], int, set[str]]:
    """Extract tabs, session working count, and live agent names from a raw snapshot."""
    tabs = parse_workspace_tabs(
        raw,
        caller.workspace_id,
        self_tab_id=caller.tab_id,
        self_pane_id=caller.pane_id,
    )
    return tabs, count_session_working_agents(raw), collect_live_agent_names(raw)
