"""Pure orchestration planning and execution for ensure, send, and wait."""

from __future__ import annotations

import os
import re
import shlex
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from .models import (
    DEFAULT_MAX_SESSION_AGENTS,
    DEFAULT_MAX_SPACE_AGENTS,
    DEFAULT_MAX_SUB_TABS,
    DEFAULT_MAX_WORKER_PANES,
    EXIT_DOMAIN,
    EXIT_USAGE,
    VALID_ON_EXISTING_POLICIES,
    VALID_SPLIT_DIRECTIONS,
    CallerContext,
    CliError,
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
    count_session_working_agents,
    count_workspace_non_primary_agents,
    find_matching_spaces,
    is_primary_exact_match,
    parse_snapshot,
    parse_workspace_tabs,
    resolve_pane_target,
    resolve_tab_target,
    resolve_target,
    strip_tab_decorations,
    verify_claude_busy,
    verify_panes_claude_busy,
)

_CLAUDE_ENV_VARS = ("CLAUDE_ACCOUNTS_DIR", "CLAUDE_CONFIG_DIR", "XDG_STATE_HOME")


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


def _wait_for_launched_agent(
    *,
    client: SnapshotClient,
    workspace_id: str,
    expected_agents: tuple[str, ...],
    self_workspace_id: str | None,
    timeout: float,
    poll_interval: float,
    sleep_fn: Callable[[float], None],
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


def execute_send(
    space: SpaceSnapshot,
    *,
    prompt: str,
    wait: bool,
    timeout: float,
    force: bool,
    dry_run: bool,
    prompt_fn: PromptAgentFn,
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
    start = time.monotonic()
    deadline = start + timeout
    until_tuple = tuple(until)
    until_set = frozenset(until_tuple)

    def evaluate() -> tuple[list[SpaceSnapshot], list[SpaceSnapshot]]:
        spaces = parse_snapshot(client.snapshot(), self_workspace_id=self_workspace_id)
        if verify_busy:
            spaces = verify_claude_busy(spaces, client.read_detection)
        return match_wait_targets(
            spaces,
            until=until_set,
            workspace=workspace,
            workers_only=workers_only,
        )

    watched, matched = evaluate()
    if matched:
        return WaitResult(
            until=until_tuple,
            matched=tuple(matched),
            elapsed_seconds=time.monotonic() - start,
            from_initial_snapshot=True,
        )

    pane_ids = [s.primary_pane.pane_id for s in watched if s.primary_pane is not None]
    for _event in client.subscribe_events(pane_ids, deadline=deadline):
        watched, matched = evaluate()
        if matched:
            return WaitResult(
                until=until_tuple,
                matched=tuple(matched),
                elapsed_seconds=time.monotonic() - start,
                from_initial_snapshot=False,
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
    dry_run: bool = False,
    ready_timeout: float = 45.0,
    poll_interval: float = 0.5,
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
    )
    prompt_sent = False
    if plan.prompt and plan.prompt.strip():
        prompt_fn(pane_id, plan.prompt, wait=False)
        prompt_sent = True
        pane = _find_pane_in_workspace(client.snapshot(), plan.workspace_id, pane_id) or pane

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
    dry_run: bool = False,
    ready_timeout: float = 45.0,
    poll_interval: float = 0.5,
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
    )
    prompt_sent = False
    if plan.prompt and plan.prompt.strip():
        prompt_fn(pane_id, plan.prompt, wait=False)
        prompt_sent = True
        pane = _find_pane_in_workspace(client.snapshot(), plan.workspace_id, pane_id) or pane

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
    counts: dict[str, int] = {}
    sub_tabs_blocked_count = 0
    for space in spaces:
        counts[space.attention] = counts.get(space.attention, 0) + 1
        if space.sub_tabs.any_blocked:
            sub_tabs_blocked_count += 1

    entries: list[DigestEntry] = []
    warnings: list[str] = []
    for space in spaces:
        needs_attention = space.attention in {"blocked", "done"} or space.sub_tabs.any_blocked
        if not include_all and workspace is None and not needs_attention:
            continue

        peeks: list[DigestPeek] = []
        if peek_lines > 0:
            pane = space.primary_pane
            if pane is not None and (
                space.attention in {"blocked", "done"} or workspace is not None
            ):
                try:
                    read = read_pane(
                        pane.pane_id,
                        has_agent=pane.agent_kind is not None,
                        source=peek_source,
                        lines=peek_lines,
                    )
                    peeks.append(
                        DigestPeek(
                            tab_id=space.primary_tab_id or "",
                            tab_label=strip_tab_decorations(space.primary_tab_label or "Tab 1"),
                            pane_id=pane.pane_id,
                            agent_kind=pane.agent_kind,
                            agent_status=pane.effective_status,
                            is_primary=True,
                            lines=peek_lines,
                            text=str(read["text"]),
                        )
                    )
                except CliError as exc:
                    warnings.append(f"{space.workspace_id} ({pane.pane_id}): {exc.message}")

            for bp in space.sub_tabs.blocked_panes:
                bp_pane_id = bp["pane_id"]
                try:
                    read = read_pane(
                        bp_pane_id,
                        has_agent=True,
                        source=peek_source,
                        lines=peek_lines,
                    )
                    peeks.append(
                        DigestPeek(
                            tab_id=bp["tab_id"],
                            tab_label=bp["tab_label"],
                            pane_id=bp_pane_id,
                            agent_kind=None,
                            agent_status="blocked",
                            is_primary=False,
                            lines=peek_lines,
                            text=str(read["text"]),
                        )
                    )
                except CliError as exc:
                    warnings.append(f"{space.workspace_id} ({bp_pane_id}): {exc.message}")

        entries.append(
            DigestEntry(
                space=space,
                needs_attention=needs_attention,
                peeks=tuple(peeks),
            )
        )

    return (
        DigestResult(
            total_workspaces=len(spaces),
            counts=counts,
            sub_tabs_blocked_count=sub_tabs_blocked_count,
            entries=tuple(entries),
            peek_lines=peek_lines,
            include_all=include_all,
        ),
        warnings,
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
