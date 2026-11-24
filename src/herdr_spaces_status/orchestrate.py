"""Pure orchestration planning and execution for ensure, send, and wait."""

from __future__ import annotations

import os
import re
import shlex
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .models import (
    EXIT_DOMAIN,
    EXIT_USAGE,
    CliError,
    EnsurePlan,
    EnsureResult,
    SendResult,
    SpaceSnapshot,
    WaitResult,
)
from .snapshot import (
    collect_live_agent_names,
    parse_snapshot,
    resolve_target,
    verify_claude_busy,
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


@dataclass(frozen=True)
class LauncherSpec:
    """Definition of a supported first-tab agent or shell launcher."""

    name: str
    expected_agent: str | None
    mode: str  # "pane_run" | "agent_start" | "none"
    command: str | None = None
    agent_kind: str | None = None
    forward_claude_env: bool = False


LAUNCHERS: dict[str, LauncherSpec] = {
    "ai-round": LauncherSpec(
        name="ai-round",
        expected_agent="claude",
        mode="pane_run",
        command="ai-round",
        forward_claude_env=True,
    ),
    "ai-best": LauncherSpec(
        name="ai-best",
        expected_agent="claude",
        mode="pane_run",
        command="ai-best",
        forward_claude_env=True,
    ),
    "ay": LauncherSpec(
        name="ay",
        expected_agent="agy",
        mode="pane_run",
        command="ay",
        forward_claude_env=False,
    ),
    "codex": LauncherSpec(
        name="codex",
        expected_agent="codex",
        mode="agent_start",
        agent_kind="codex",
    ),
    "claude": LauncherSpec(
        name="claude",
        expected_agent="claude",
        mode="agent_start",
        agent_kind="claude",
    ),
    "shell": LauncherSpec(
        name="shell",
        expected_agent=None,
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
    if not launcher.forward_claude_env:
        return launcher.command

    assignments: list[str] = []
    for var in _CLAUDE_ENV_VARS:
        value = env.get(var, "").strip()
        if value:
            if var in {"CLAUDE_ACCOUNTS_DIR", "CLAUDE_CONFIG_DIR", "XDG_STATE_HOME"}:
                value = str(Path(value).expanduser().resolve())
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


def _find_matching_space(
    spaces: Sequence[SpaceSnapshot],
    *,
    label: str | None,
    norm_cwd: str | None,
    norm_ticket: str | None,
) -> SpaceSnapshot | None:
    if label:
        for space in spaces:
            if space.label == label:
                return space
    if norm_ticket:
        for space in spaces:
            if space.ticket_key == norm_ticket and space.label == norm_ticket:
                return space
        for space in spaces:
            if space.ticket_key == norm_ticket:
                return space
    if norm_cwd:
        for space in spaces:
            if space.primary_pane and space.primary_pane.cwd:
                pane_cwd = os.path.normpath(os.path.abspath(space.primary_pane.cwd))
                if pane_cwd == norm_cwd:
                    return space
    return None


def plan_ensure(
    spaces: Sequence[SpaceSnapshot],
    *,
    label: str | None,
    cwd: str | None,
    ticket: str | None,
    pr_number: str | None,
    launcher_name: str,
    base_ref: str | None = None,
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
            "Specify --label, --cwd, --ticket, or --pr.",
            "Provide a target workspace or directory, e.g. `hss ensure --ticket CBRD-10002`.",
            exit_code=EXIT_USAGE,
        )

    norm_cwd: str | None = None
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

    derived_label = label or norm_ticket or (f"PR-{norm_pr}" if norm_pr else Path(norm_cwd).name)
    existing = _find_matching_space(
        spaces,
        label=label,
        norm_cwd=norm_cwd,
        norm_ticket=norm_ticket,
    )

    if existing is not None:
        pane = existing.primary_pane
        existing_pane_id = pane.pane_id if pane else None
        if spec.expected_agent is None:
            agent_action = "none"
        elif pane is None:
            raise CliError(
                "NO_PRIMARY_PANE",
                f"Workspace {existing.workspace_id} ({existing.label}) has no primary pane.",
                "Inspect the workspace tabs in Herdr before retrying.",
            )
        elif pane.agent_kind == spec.expected_agent:
            agent_action = "reuse"
        elif pane.agent_kind is None:
            agent_action = "launch"
        else:
            raise CliError(
                "OCCUPIED_BY_OTHER_AGENT",
                (
                    f"Primary pane {pane.pane_id} in {existing.label} already runs "
                    f"{pane.agent_kind!r}, not {spec.expected_agent!r}."
                ),
                "Reuse the existing agent, or open a sibling tab manually if intentional.",
                details={
                    "workspace_id": existing.workspace_id,
                    "pane_id": pane.pane_id,
                    "running_agent": pane.agent_kind,
                    "expected_agent": spec.expected_agent,
                },
            )
        return EnsurePlan(
            workspace_action="reuse",
            agent_action=agent_action,
            launcher=spec.name,
            expected_agent=spec.expected_agent,
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
        agent_action="none" if spec.expected_agent is None else "launch",
        launcher=spec.name,
        expected_agent=spec.expected_agent,
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
    self_workspace_id: str | None = None,
    dry_run: bool = False,
    ready_timeout: float = 45.0,
    poll_interval: float = 0.5,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> EnsureResult:
    """Execute an EnsurePlan idempotently, converging workspace and first-tab agent."""
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
            agent_kind=plan.expected_agent,
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
            "Run `herdr-spaces-status status` to inspect created workspaces.",
        )

    pane = target_space.primary_pane
    spec = LAUNCHERS[plan.launcher]
    if spec.expected_agent is None:
        return _build_ensure_result(plan, target_space, agent_action="none")

    if pane.agent_kind == spec.expected_agent:
        return _build_ensure_result(plan, target_space, agent_action="reuse")

    if pane.agent_kind is not None:
        raise CliError(
            "OCCUPIED_BY_OTHER_AGENT",
            (
                f"Primary pane {pane.pane_id} already runs {pane.agent_kind!r}, "
                f"expected {spec.expected_agent!r}."
            ),
            "Inspect the pane before launching another agent.",
            details={"workspace_id": target_space.workspace_id, "pane_id": pane.pane_id},
        )

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
        expected_agent=spec.expected_agent,
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
    return _find_matching_space(
        spaces,
        label=plan.label,
        norm_cwd=norm_cwd,
        norm_ticket=norm_ticket,
    )


def _wait_for_launched_agent(
    *,
    client: SnapshotClient,
    workspace_id: str,
    expected_agent: str,
    self_workspace_id: str | None,
    timeout: float,
    poll_interval: float,
    sleep_fn: Callable[[float], None],
) -> SpaceSnapshot:
    deadline = time.monotonic() + timeout
    last_space: SpaceSnapshot | None = None
    while True:
        spaces = parse_snapshot(client.snapshot(), self_workspace_id=self_workspace_id)
        space = next((s for s in spaces if s.workspace_id == workspace_id), None)
        if space is not None and space.primary_pane is not None:
            last_space = space
            pane = space.primary_pane
            if pane.agent_kind == expected_agent:
                if pane.raw_status == "blocked":
                    raise CliError(
                        "AGENT_STARTUP_BLOCKED",
                        f"Launched agent {expected_agent} is blocked at startup in {pane.pane_id}.",
                        f"Inspect it with `herdr-spaces-status peek {workspace_id}`.",
                        details={"workspace_id": workspace_id, "pane_id": pane.pane_id},
                    )
                if pane.interactive_ready or pane.raw_status in {"idle", "done"}:
                    return space
        if time.monotonic() >= deadline:
            raise CliError(
                "AGENT_READY_TIMEOUT",
                (
                    f"Timed out after {timeout:.0f}s waiting for {expected_agent} "
                    f"in workspace {workspace_id}."
                ),
                f"Inspect the pane with `herdr-spaces-status peek {workspace_id}` before retrying.",
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
            "Launch an agent first with `herdr-spaces-status ensure`.",
        )
    if pane.effective_status == "blocked":
        raise CliError(
            "AGENT_BLOCKED",
            f"Agent in {space.label} ({pane.pane_id}) is blocked waiting for approval or input.",
            f"Inspect the dialog with `herdr-spaces-status peek {space.workspace_id}` first.",
            details={"workspace_id": space.workspace_id, "pane_id": pane.pane_id},
        )
    if pane.effective_status in {"working", "busy_bg"} and not force:
        raise CliError(
            "AGENT_BUSY",
            (f"Agent in {space.label} ({pane.pane_id}) is currently {pane.effective_status}."),
            "Wait for completion with `herdr-spaces-status wait`, or pass --force.",
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
        "Run `herdr-spaces-status status` to inspect current workspace states.",
        exit_code=EXIT_DOMAIN,
    )
