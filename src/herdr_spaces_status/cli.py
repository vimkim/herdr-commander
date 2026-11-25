"""CLI entry point for herdr-spaces-status (hss)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__
from .formatters import (
    STATUS_FIELDS,
    OutputMode,
    format_ensure,
    format_error,
    format_peek,
    format_send,
    format_status,
    format_wait,
    parse_fields,
    resolve_output_mode,
)
from .herdr_client import (
    HerdrSocketClient,
    agent_prompt,
    agent_start,
    ai_agent_select_backend,
    create_workspace_cubrid_pr,
    create_workspace_cubrid_ticket,
    create_workspace_cwd,
    fetch_work_tracker_items,
    pane_run,
    resolve_socket_path,
    workspace_env_overrides,
)
from .models import (
    DEFAULT_WAIT_STATES,
    EXIT_DOMAIN,
    EXIT_OK,
    EXIT_SIGINT,
    EXIT_USAGE,
    VALID_SORT_MODES,
    VALID_WAIT_STATES,
    CliError,
    PeekResult,
)
from .orchestrate import (
    LAUNCHERS,
    execute_ensure,
    execute_send,
    execute_wait,
    plan_ensure,
)
from .snapshot import (
    collect_live_agent_names,
    correlate_work_tracker,
    filter_spaces,
    parse_snapshot,
    resolve_target,
    verify_claude_busy,
)

_EPILOG = """
Exit codes:
  0    Success
  1    Domain failure (target missing, occupied pane, blocked/busy agent, timeout)
  2    Invalid usage or flags
  75   Temporary retry-safe socket or connection failure
  78   Configuration or environment error (e.g. HERDR_ENV != 1)
  130  Interrupted (SIGINT)

Examples:
  hss status --workers-only --verify-busy --with-tracker
  hss status --plain --fields number,workspace_id,attention,label
  hss peek w1 --lines 40
  hss wait --until blocked,done --timeout 300 --verify-busy --json
  hss ensure --ticket CBRD-10002 --launcher ai-agent --json
  hss send w1 --prompt "Summarize findings in tab 1." --json
"""


def _build_common_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit structured JSON envelope on stdout (errors on stderr)",
    )
    parent.add_argument(
        "--plain",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit stable tab-separated rows with no color",
    )
    parent.add_argument(
        "--no-color",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Disable ANSI color in human output",
    )
    parent.add_argument(
        "--socket",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="Explicit Herdr Unix socket path (overrides HERDR_ENV check)",
    )
    return parent


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level CLI argument parser."""
    common = _build_common_parent()
    parser = argparse.ArgumentParser(
        prog="herdr-spaces-status",
        description="Inspect and orchestrate Herdr workspaces by their first-tab agent.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--json", action="store_true", help="Structured JSON output")
    parser.add_argument("--plain", action="store_true", help="Stable tab-separated output")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color")
    parser.add_argument("--socket", default=None, metavar="PATH", help="Herdr socket path")

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    p_status = subparsers.add_parser(
        "status",
        parents=[common],
        help="Show first-tab agent status and sub-tab rollups across workspaces",
    )
    p_status.add_argument(
        "--workers-only",
        action="store_true",
        help="Exclude the caller's own HERDR_WORKSPACE_ID",
    )
    p_status.add_argument(
        "--attention-only",
        action="store_true",
        help="Show only blocked, done, shell, or idle workspaces",
    )
    p_status.add_argument("--workspace", help="Filter to one workspace ID, label, or ticket")
    p_status.add_argument(
        "--verify-busy",
        action="store_true",
        help="Inspect Claude detection buffer to flag background-busy panes as busy_bg",
    )
    p_status.add_argument(
        "--all-tabs",
        action="store_true",
        help="Include full tab inventory in human and JSON output",
    )
    p_status.add_argument(
        "--with-tracker",
        action="store_true",
        help="Correlate workspaces with open work-tracker items",
    )
    p_status.add_argument(
        "--sort",
        choices=VALID_SORT_MODES,
        default="attention",
        help="Sort order (default: attention)",
    )
    p_status.add_argument(
        "--fields",
        metavar="COLS",
        help=f"Comma-separated columns ({', '.join(STATUS_FIELDS)})",
    )

    p_peek = subparsers.add_parser(
        "peek",
        parents=[common],
        help="Read recent output from a workspace's first-tab pane",
    )
    p_peek.add_argument("target", help="Workspace ID, label, or ticket key")
    p_peek.add_argument("--lines", type=int, default=40, help="Lines to read (default: 40)")
    p_peek.add_argument(
        "--source",
        choices=("visible", "recent", "recent-unwrapped", "detection"),
        default="recent-unwrapped",
        help="Herdr read source (default: recent-unwrapped)",
    )

    p_wait = subparsers.add_parser(
        "wait",
        parents=[common],
        help="Wait for watched first-tab agents to enter target states",
    )
    p_wait.add_argument(
        "--until",
        default=",".join(DEFAULT_WAIT_STATES),
        help="Comma-separated target states (default: blocked,done)",
    )
    p_wait.add_argument("--workspace", help="Watch only one workspace ID, label, or ticket")
    p_wait.add_argument(
        "--all-workspaces",
        action="store_true",
        help="Include caller workspace when watching all workspaces",
    )
    p_wait.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Timeout in seconds (default: 300)",
    )
    p_wait.add_argument(
        "--verify-busy",
        action="store_true",
        help="Apply Claude background-busy verification before matching",
    )

    p_ensure = subparsers.add_parser(
        "ensure",
        parents=[common],
        help="Idempotently ensure a workspace and first-tab agent are running",
    )
    p_ensure.add_argument("--label", help="Workspace label")
    p_ensure.add_argument("--cwd", help="Existing directory for a non-CUBRID workspace")
    p_ensure.add_argument("--ticket", help="CUBRID JIRA ticket key (e.g. CBRD-10002)")
    p_ensure.add_argument("--pr", help="CUBRID PR number for review worktree")
    p_ensure.add_argument("--base", help="Base ref for `cubrid-worktree create --base`")
    p_ensure.add_argument(
        "--launcher",
        default="ai-agent",
        choices=tuple(LAUNCHERS.keys()),
        help=(
            "First-tab launcher (default: ai-agent, which picks `ay` (agy) or "
            "`ai-round` (claude) by remaining quota)"
        ),
    )
    p_ensure.add_argument(
        "--ready-timeout",
        type=float,
        default=45.0,
        help="Seconds to wait for launched agent readiness (default: 45)",
    )
    p_ensure.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan changes without creating workspaces or starting agents",
    )

    p_send = subparsers.add_parser(
        "send",
        parents=[common],
        help="Safely send a prompt to a workspace's first-tab agent",
    )
    p_send.add_argument("target", help="Workspace ID, label, or ticket key")
    p_send.add_argument(
        "--prompt",
        required=True,
        help="Prompt text, or `-` to read from stdin",
    )
    p_send.add_argument(
        "--wait",
        action="store_true",
        help="Wait for agent completion via `herdr agent prompt --wait`",
    )
    p_send.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Wait timeout in seconds when --wait is set (default: 120)",
    )
    p_send.add_argument(
        "--force",
        action="store_true",
        help="Allow sending to self or currently working agent (never overrides blocked)",
    )
    p_send.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate target and readiness without sending input",
    )

    return parser


def _parse_until(raw: str) -> tuple[str, ...]:
    items = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not items:
        raise CliError(
            "INVALID_UNTIL",
            "--until cannot be empty.",
            f"Pass one or more states from: {', '.join(sorted(VALID_WAIT_STATES))}.",
            exit_code=EXIT_USAGE,
        )
    invalid = [item for item in items if item not in VALID_WAIT_STATES]
    if invalid:
        raise CliError(
            "INVALID_UNTIL",
            f"Unsupported wait state(s): {', '.join(invalid)}.",
            f"Valid states: {', '.join(sorted(VALID_WAIT_STATES))}.",
            exit_code=EXIT_USAGE,
        )
    return items


def _read_prompt_arg(raw_prompt: str, is_tty_stdin: bool) -> str:
    if raw_prompt != "-":
        return raw_prompt
    if is_tty_stdin:
        raise CliError(
            "STDIN_IS_TTY",
            "`--prompt -` requires piped stdin rather than an interactive terminal.",
            'Pipe prompt text via stdin or pass `--prompt "..."` directly.',
            exit_code=EXIT_USAGE,
        )
    return sys.stdin.read()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; only place permitted to write streams or return exit status."""
    parser = build_parser()
    args_list = list(sys.argv[1:] if argv is None else argv)

    if not args_list:
        parser.print_help(sys.stdout)
        return EXIT_OK

    try:
        ns = parser.parse_args(args_list)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE

    env = workspace_env_overrides()
    try:
        mode = resolve_output_mode(
            json_flag=bool(getattr(ns, "json", False)),
            plain_flag=bool(getattr(ns, "plain", False)),
            no_color_flag=bool(getattr(ns, "no_color", False)),
            env=env,
            is_tty_stdout=sys.stdout.isatty(),
            is_tty_stderr=sys.stderr.isatty(),
        )
    except CliError as err:
        sys.stderr.write(format_error(err, resolve_fallback_mode(args_list)) + "\n")
        return err.exit_code

    if not getattr(ns, "command", None):
        parser.print_help(sys.stdout)
        return EXIT_OK

    self_workspace_id = env.get("HERDR_WORKSPACE_ID") or None

    try:
        socket_path = resolve_socket_path(getattr(ns, "socket", None), env)
        client = HerdrSocketClient(socket_path)
        output = _dispatch(ns, client, env, self_workspace_id, mode)
    except KeyboardInterrupt:
        err = CliError(
            "INTERRUPTED",
            "Operation cancelled by SIGINT.",
            "Re-run the command if needed.",
            exit_code=EXIT_SIGINT,
        )
        sys.stderr.write(format_error(err, mode) + "\n")
        return EXIT_SIGINT
    except CliError as err:
        sys.stderr.write(format_error(err, mode) + "\n")
        return err.exit_code
    except Exception as exc:
        err = CliError(
            "INTERNAL_ERROR",
            f"Unexpected failure: {exc}",
            "Re-run with `herdr status` checked and report this bug if reproducible.",
            exit_code=EXIT_DOMAIN,
        )
        sys.stderr.write(format_error(err, mode) + "\n")
        return EXIT_DOMAIN

    if output:
        sys.stdout.write(output + "\n")
    return EXIT_OK


def resolve_fallback_mode(args_list: Sequence[str]) -> OutputMode:
    """Resolve minimal OutputMode when flag validation fails early."""
    return OutputMode(
        fmt="json" if "--json" in args_list else "human",
        color=False,
        is_tty_stdout=sys.stdout.isatty(),
        is_tty_stderr=sys.stderr.isatty(),
    )


def _dispatch(
    ns: argparse.Namespace,
    client: HerdrSocketClient,
    env: dict[str, str],
    self_workspace_id: str | None,
    mode: OutputMode,
) -> str:
    cmd = ns.command

    if cmd == "status":
        fields = parse_fields(ns.fields)
        spaces = parse_snapshot(client.snapshot(), self_workspace_id=self_workspace_id)
        if ns.verify_busy:
            spaces = verify_claude_busy(spaces, client.read_detection)
        warnings: list[str] = []
        if ns.with_tracker:
            items, warn = fetch_work_tracker_items()
            if warn:
                warnings.append(warn)
                if mode.fmt != "json":
                    sys.stderr.write(f"Warning: {warn}\n")
            spaces = correlate_work_tracker(spaces, items)
        filtered = filter_spaces(
            spaces,
            workers_only=bool(ns.workers_only),
            attention_only=bool(ns.attention_only),
            workspace=ns.workspace,
            sort=ns.sort,
        )
        return format_status(
            filtered,
            mode,
            include_tabs=bool(ns.all_tabs),
            fields=fields,
            warnings=warnings,
        )

    if cmd == "peek":
        if ns.lines <= 0:
            raise CliError(
                "INVALID_LINES",
                "--lines must be a positive integer.",
                "Pass e.g. `--lines 40`.",
                exit_code=EXIT_USAGE,
            )
        spaces = parse_snapshot(client.snapshot(), self_workspace_id=self_workspace_id)
        space = resolve_target(spaces, ns.target)
        if space.primary_pane is None or space.primary_tab_id is None:
            raise CliError(
                "NO_PRIMARY_PANE",
                f"Workspace {space.workspace_id} ({space.label}) has no primary pane.",
                "Inspect the workspace tabs in Herdr.",
            )
        pane = space.primary_pane
        read = client.read_pane(
            pane.pane_id,
            has_agent=pane.agent_kind is not None,
            source=ns.source,
            lines=ns.lines,
        )
        return format_peek(
            PeekResult(
                workspace_id=space.workspace_id,
                label=space.label,
                tab_id=space.primary_tab_id,
                pane_id=pane.pane_id,
                agent_kind=pane.agent_kind,
                agent_status=pane.effective_status,
                source=ns.source,
                lines=ns.lines,
                text=str(read["text"]),
            ),
            mode,
        )

    if cmd == "wait":
        if ns.timeout <= 0:
            raise CliError(
                "INVALID_TIMEOUT",
                "--timeout must be positive.",
                "Pass e.g. `--timeout 300`.",
                exit_code=EXIT_USAGE,
            )
        until = _parse_until(ns.until)
        result = execute_wait(
            client=client,
            until=until,
            workspace=ns.workspace,
            workers_only=not bool(ns.all_workspaces),
            timeout=float(ns.timeout),
            verify_busy=bool(ns.verify_busy),
            self_workspace_id=self_workspace_id,
        )
        return format_wait(result, mode)

    if cmd == "ensure":
        if ns.ready_timeout <= 0:
            raise CliError(
                "INVALID_TIMEOUT",
                "--ready-timeout must be positive.",
                "Pass e.g. `--ready-timeout 45`.",
                exit_code=EXIT_USAGE,
            )
        raw_snap = client.snapshot()
        spaces = parse_snapshot(raw_snap, self_workspace_id=self_workspace_id)
        plan = plan_ensure(
            spaces,
            label=ns.label,
            cwd=ns.cwd,
            ticket=ns.ticket,
            pr_number=ns.pr,
            launcher_name=ns.launcher,
            base_ref=ns.base,
            existing_agent_names=collect_live_agent_names(raw_snap),
        )
        result = execute_ensure(
            plan,
            client=client,
            create_cwd=create_workspace_cwd,
            create_ticket=create_workspace_cubrid_ticket,
            create_pr=create_workspace_cubrid_pr,
            run_pane=pane_run,
            start_agent=agent_start,
            env=env,
            select_backend=ai_agent_select_backend,
            self_workspace_id=self_workspace_id,
            dry_run=bool(ns.dry_run),
            ready_timeout=float(ns.ready_timeout),
        )
        return format_ensure(result, mode)

    if cmd == "send":
        if ns.timeout <= 0:
            raise CliError(
                "INVALID_TIMEOUT",
                "--timeout must be positive.",
                "Pass e.g. `--timeout 120`.",
                exit_code=EXIT_USAGE,
            )
        prompt = _read_prompt_arg(ns.prompt, sys.stdin.isatty())
        spaces = parse_snapshot(client.snapshot(), self_workspace_id=self_workspace_id)
        space = resolve_target(spaces, ns.target)
        if space.primary_pane and space.primary_pane.agent_kind == "claude":
            verified = verify_claude_busy([space], client.read_detection)
            space = verified[0]
        result = execute_send(
            space,
            prompt=prompt,
            wait=bool(ns.wait),
            timeout=float(ns.timeout),
            force=bool(ns.force),
            dry_run=bool(ns.dry_run),
            prompt_fn=agent_prompt,
        )
        return format_send(result, mode)

    raise CliError(
        "UNKNOWN_COMMAND",
        f"Unsupported command {cmd!r}.",
        "Run `herdr-spaces-status --help`.",
        exit_code=EXIT_USAGE,
    )


if __name__ == "__main__":
    raise SystemExit(main())
