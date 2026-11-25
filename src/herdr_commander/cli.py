"""CLI entry point for herdr-commander (hcmd)."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace

from . import __version__
from .formatters import (
    STATUS_FIELDS,
    OutputMode,
    format_ensure,
    format_error,
    format_find,
    format_pane_close,
    format_pane_list,
    format_pane_spawn,
    format_pane_wait,
    format_peek,
    format_send,
    format_status,
    format_tab_close,
    format_tab_list,
    format_tab_spawn,
    format_tab_wait,
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
    pane_close,
    pane_run,
    pane_split,
    resolve_socket_path,
    tab_close,
    tab_create,
    workspace_env_overrides,
)
from .models import (
    DEFAULT_MAX_SESSION_AGENTS,
    DEFAULT_MAX_SPACE_AGENTS,
    DEFAULT_MAX_SUB_TABS,
    DEFAULT_MAX_WORKER_PANES,
    DEFAULT_WAIT_STATES,
    EXIT_DOMAIN,
    EXIT_OK,
    EXIT_SIGINT,
    EXIT_USAGE,
    VALID_SORT_MODES,
    VALID_SPLIT_DIRECTIONS,
    VALID_WAIT_STATES,
    CliError,
    PaneInfo,
    PeekResult,
)
from .orchestrate import (
    LAUNCHERS,
    execute_ensure,
    execute_pane_close,
    execute_pane_send,
    execute_pane_spawn,
    execute_pane_wait,
    execute_send,
    execute_tab_close,
    execute_tab_send,
    execute_tab_spawn,
    execute_tab_wait,
    execute_wait,
    plan_ensure,
    plan_pane_spawn,
    plan_tab_spawn,
    verify_tabs_claude_busy,
)
from .snapshot import (
    collect_live_agent_names,
    collect_workspace_panes,
    correlate_work_tracker,
    count_session_working_agents,
    filter_spaces,
    find_matching_spaces,
    parse_snapshot,
    parse_workspace_tabs,
    resolve_caller_context,
    resolve_pane_target,
    resolve_tab_target,
    resolve_target,
    verify_claude_busy,
    verify_panes_claude_busy,
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
  hcmd status --workers-only --verify-busy --with-tracker
  hcmd status --plain --fields number,workspace_id,attention,label
  hcmd find --label my-repo --cwd ~/gh/my-repo --verify-busy --json
  hcmd peek w1 --lines 40
  hcmd wait --until blocked,done --timeout 300 --verify-busy --json
  hcmd ensure --ticket CBRD-10002 --launcher ai-agent --json
  hcmd send w1 --prompt "Summarize findings in tab 1." --json
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
        prog="herdr-commander",
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

    p_find = subparsers.add_parser(
        "find",
        parents=[common],
        help=(
            "Check whether existing workspaces already match a name, label, cwd/pwd, ticket, or PR"
        ),
    )
    p_find.add_argument(
        "query",
        nargs="?",
        default=None,
        help="Optional free-form name, label, path, ticket, or workspace ID to match",
    )
    p_find.add_argument("--name", help="Match workspace label, agent name, or directory basename")
    p_find.add_argument("--label", help="Match workspace label")
    p_find.add_argument(
        "--cwd",
        "--pwd",
        dest="cwd",
        help="Match primary or sub-pane working directory (or parent directory)",
    )
    p_find.add_argument("--ticket", help="Match CUBRID JIRA ticket key (e.g. CBRD-10002)")
    p_find.add_argument("--pr", help="Match CUBRID PR number (e.g. 1234)")
    p_find.add_argument(
        "--verify-busy",
        action="store_true",
        help="Inspect Claude detection buffer on matched workspaces to flag busy_bg",
    )
    p_find.add_argument(
        "--with-tracker",
        action="store_true",
        help="Correlate matched workspaces with open work-tracker items",
    )
    p_find.add_argument(
        "--include-self",
        action="store_true",
        help="Include the caller's own HERDR_WORKSPACE_ID in matches",
    )
    p_find.add_argument(
        "--include-deleted",
        action="store_true",
        help="Include workspaces marked deleted or whose cwd is missing",
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
        "--new",
        action="store_true",
        help="Create a new cwd workspace even if a matching workspace already exists",
    )
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

    p_tab = subparsers.add_parser(
        "tab",
        parents=[common],
        help="Manage sub-tabs inside the caller's Space (Space Leader only)",
    )
    tab_sub = p_tab.add_subparsers(dest="tab_command", metavar="SUBCOMMAND")

    p_tab_list = tab_sub.add_parser(
        "list",
        parents=[common],
        help="List tabs inside the caller's Space",
    )
    p_tab_list.add_argument(
        "--verify-busy",
        action="store_true",
        help="Inspect Claude detection buffer to flag background-busy panes as busy_bg",
    )

    p_tab_spawn = tab_sub.add_parser(
        "spawn",
        parents=[common],
        help="Create a worker sub-tab inside the caller's Space (up to 8 sub-tabs)",
    )
    p_tab_spawn.add_argument(
        "--label",
        required=True,
        help="Short role label for the sub-tab (e.g. build, test, review, ci)",
    )
    p_tab_spawn.add_argument("--cwd", help="Working directory (defaults to caller tab cwd)")
    p_tab_spawn.add_argument(
        "--launcher",
        default="ai-agent",
        choices=tuple(LAUNCHERS.keys()),
        help="Worker launcher (default: ai-agent)",
    )
    p_tab_spawn.add_argument("--prompt", help="Optional kickoff prompt (or `-` for stdin)")
    p_tab_spawn.add_argument(
        "--ready-timeout",
        type=float,
        default=45.0,
        help="Seconds to wait for launched agent readiness (default: 45)",
    )
    p_tab_spawn.add_argument(
        "--max-sub-tabs",
        type=int,
        default=None,
        help=(
            f"Max non-primary sub-tabs in this Space (default: {DEFAULT_MAX_SUB_TABS}, 0=unlimited)"
        ),
    )
    p_tab_spawn.add_argument(
        "--max-space-agents",
        type=int,
        default=None,
        help=(
            "Max live non-primary AI agents in this Space "
            f"(default: {DEFAULT_MAX_SPACE_AGENTS}, 0=unlimited)"
        ),
    )
    p_tab_spawn.add_argument(
        "--max-session-agents",
        type=int,
        default=None,
        help=(
            "Max working AI agents across the Herdr session "
            f"(default: {DEFAULT_MAX_SESSION_AGENTS}, 0=unlimited)"
        ),
    )
    p_tab_spawn.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate limits and preflight launcher without creating a tab",
    )

    p_tab_peek = tab_sub.add_parser(
        "peek",
        parents=[common],
        help="Read recent output from a tab's primary pane in the caller's Space",
    )
    p_tab_peek.add_argument("target", help="Tab ID (w1:t2 or t2), pane ID, or role label")
    p_tab_peek.add_argument("--lines", type=int, default=40, help="Lines to read (default: 40)")
    p_tab_peek.add_argument(
        "--source",
        choices=("visible", "recent", "recent-unwrapped", "detection"),
        default="recent-unwrapped",
        help="Herdr read source (default: recent-unwrapped)",
    )

    p_tab_send = tab_sub.add_parser(
        "send",
        parents=[common],
        help="Send a prompt to a sub-tab's primary agent in the caller's Space",
    )
    p_tab_send.add_argument("target", help="Tab ID (w1:t2 or t2), pane ID, or role label")
    p_tab_send.add_argument(
        "--prompt",
        required=True,
        help="Prompt text, or `-` to read from stdin",
    )
    p_tab_send.add_argument(
        "--wait",
        action="store_true",
        help="Wait for agent completion via `herdr agent prompt --wait`",
    )
    p_tab_send.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Wait timeout in seconds when --wait is set (default: 120)",
    )
    p_tab_send.add_argument(
        "--force",
        action="store_true",
        help="Allow sending to self or busy agent",
    )
    p_tab_send.add_argument("--dry-run", action="store_true", help="Validate without sending input")

    p_tab_wait = tab_sub.add_parser(
        "wait",
        parents=[common],
        help="Wait for sub-tabs in the caller's Space to enter target states",
    )
    p_tab_wait.add_argument(
        "--until",
        default=",".join(DEFAULT_WAIT_STATES),
        help="Comma-separated target states (default: blocked,done)",
    )
    p_tab_wait.add_argument("--target", help="Watch only one tab ID, pane ID, or role label")
    p_tab_wait.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Timeout in seconds (default: 300)",
    )
    p_tab_wait.add_argument(
        "--verify-busy",
        action="store_true",
        help="Verify Claude background tasks",
    )

    p_tab_close = tab_sub.add_parser(
        "close",
        parents=[common],
        help="Close a non-primary worker sub-tab in the caller's Space",
    )
    p_tab_close.add_argument("target", help="Sub-tab ID (w1:t2 or t2), pane ID, or role label")
    p_tab_close.add_argument("--dry-run", action="store_true", help="Validate without closing tab")

    p_pane = subparsers.add_parser(
        "pane",
        parents=[common],
        help="Manage worker panes inside the caller's tab (Space/Tab Leader only)",
    )
    pane_sub = p_pane.add_subparsers(dest="pane_command", metavar="SUBCOMMAND")

    p_pane_list = pane_sub.add_parser(
        "list",
        parents=[common],
        help="List panes inside the caller's tab",
    )
    p_pane_list.add_argument(
        "--verify-busy",
        action="store_true",
        help="Inspect Claude detection buffer to flag background-busy panes as busy_bg",
    )

    p_pane_spawn = pane_sub.add_parser(
        "spawn",
        parents=[common],
        help="Split a worker pane inside the caller's tab (up to 3 worker panes / 2x2)",
    )
    p_pane_spawn.add_argument(
        "--direction",
        default="auto",
        choices=VALID_SPLIT_DIRECTIONS,
        help="Split direction (default: auto for 2x2 layout)",
    )
    p_pane_spawn.add_argument("--cwd", help="Working directory (defaults to caller pane cwd)")
    p_pane_spawn.add_argument(
        "--launcher",
        default="ai-agent",
        choices=tuple(LAUNCHERS.keys()),
        help="Worker launcher (default: ai-agent)",
    )
    p_pane_spawn.add_argument(
        "--cmd",
        dest="shell_cmd",
        help="Command to run with `--launcher shell`",
    )
    p_pane_spawn.add_argument("--prompt", help="Optional kickoff prompt (or `-` for stdin)")
    p_pane_spawn.add_argument(
        "--ready-timeout",
        type=float,
        default=45.0,
        help="Seconds to wait for launched agent readiness (default: 45)",
    )
    p_pane_spawn.add_argument(
        "--max-worker-panes",
        type=int,
        default=None,
        help=(
            "Max non-primary worker panes in this tab "
            f"(default: {DEFAULT_MAX_WORKER_PANES}, 0=unlimited)"
        ),
    )
    p_pane_spawn.add_argument(
        "--max-space-agents",
        type=int,
        default=None,
        help=(
            "Max live non-primary AI agents in this Space "
            f"(default: {DEFAULT_MAX_SPACE_AGENTS}, 0=unlimited)"
        ),
    )
    p_pane_spawn.add_argument(
        "--max-session-agents",
        type=int,
        default=None,
        help=(
            "Max working AI agents across the Herdr session "
            f"(default: {DEFAULT_MAX_SESSION_AGENTS}, 0=unlimited)"
        ),
    )
    p_pane_spawn.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate limits and preflight launcher without splitting a pane",
    )

    p_pane_peek = pane_sub.add_parser(
        "peek",
        parents=[common],
        help="Read recent output from a pane inside the caller's tab",
    )
    p_pane_peek.add_argument("target", help="Pane ID (w1:p2 or p2), 1-based index, or agent name")
    p_pane_peek.add_argument("--lines", type=int, default=40, help="Lines to read (default: 40)")
    p_pane_peek.add_argument(
        "--source",
        choices=("visible", "recent", "recent-unwrapped", "detection"),
        default="recent-unwrapped",
        help="Herdr read source (default: recent-unwrapped)",
    )

    p_pane_send = pane_sub.add_parser(
        "send",
        parents=[common],
        help="Send a prompt to a worker pane inside the caller's tab",
    )
    p_pane_send.add_argument("target", help="Pane ID (w1:p2 or p2), 1-based index, or agent name")
    p_pane_send.add_argument(
        "--prompt",
        required=True,
        help="Prompt text, or `-` to read from stdin",
    )
    p_pane_send.add_argument(
        "--wait",
        action="store_true",
        help="Wait for agent completion via `herdr agent prompt --wait`",
    )
    p_pane_send.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Wait timeout in seconds when --wait is set (default: 120)",
    )
    p_pane_send.add_argument(
        "--force",
        action="store_true",
        help="Allow sending to self or busy agent",
    )
    p_pane_send.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate without sending input",
    )

    p_pane_wait = pane_sub.add_parser(
        "wait",
        parents=[common],
        help="Wait for worker panes in the caller's tab to enter target states",
    )
    p_pane_wait.add_argument(
        "--until",
        default=",".join(DEFAULT_WAIT_STATES),
        help="Comma-separated target states (default: blocked,done)",
    )
    p_pane_wait.add_argument("--target", help="Watch only one pane ID, index, or agent name")
    p_pane_wait.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Timeout in seconds (default: 300)",
    )
    p_pane_wait.add_argument(
        "--verify-busy",
        action="store_true",
        help="Verify Claude background tasks",
    )

    p_pane_close = pane_sub.add_parser(
        "close",
        parents=[common],
        help="Close a non-primary worker pane inside the caller's tab",
    )
    p_pane_close.add_argument("target", help="Worker pane ID (w1:p2 or p2), index, or agent name")
    p_pane_close.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate without closing pane",
    )

    parser._subcommand_help_map = {"tab": p_tab, "pane": p_pane}  # type: ignore[attr-defined]
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

    if ns.command in {"tab", "pane"}:
        sub_attr = f"{ns.command}_command"
        if not getattr(ns, sub_attr, None):
            help_map = getattr(parser, "_subcommand_help_map", {})
            if ns.command in help_map:
                help_map[ns.command].print_help(sys.stdout)
            else:
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

    if cmd == "find":
        raw_snap = client.snapshot()
        spaces = parse_snapshot(raw_snap, self_workspace_id=self_workspace_id)
        panes_by_workspace = collect_workspace_panes(raw_snap)
        result = find_matching_spaces(
            spaces,
            query=ns.query,
            name=ns.name,
            label=ns.label,
            cwd=ns.cwd,
            ticket=ns.ticket,
            pr_number=ns.pr,
            include_self=bool(ns.include_self),
            include_deleted=bool(ns.include_deleted),
            panes_by_workspace=panes_by_workspace,
        )
        warnings: list[str] = []
        if result.matches:
            matched_spaces = [m.space for m in result.matches]
            if ns.verify_busy:
                matched_spaces = verify_claude_busy(matched_spaces, client.read_detection)
            if ns.with_tracker:
                items, warn = fetch_work_tracker_items()
                if warn:
                    warnings.append(warn)
                    if mode.fmt != "json":
                        sys.stderr.write(f"Warning: {warn}\n")
                matched_spaces = correlate_work_tracker(matched_spaces, items)
            result = replace(
                result,
                matches=tuple(
                    replace(match, space=space)
                    for match, space in zip(result.matches, matched_spaces, strict=True)
                ),
            )
        return format_find(result, mode, warnings=warnings)

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
            force_new=bool(ns.new),
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

    if cmd == "tab":
        return _dispatch_tab(ns, client, env, mode)

    if cmd == "pane":
        return _dispatch_pane(ns, client, env, mode)

    raise CliError(
        "UNKNOWN_COMMAND",
        f"Unsupported command {cmd!r}.",
        "Run `hcmd --help`.",
        exit_code=EXIT_USAGE,
    )


def _resolve_cap(
    explicit: int | None,
    env: Mapping[str, str],
    env_vars: tuple[str, ...],
    default: int,
) -> int:
    if explicit is not None:
        if explicit < 0:
            raise CliError(
                "INVALID_CAP",
                f"Cap limit cannot be negative: {explicit}.",
                "Pass 0 for unlimited or a positive integer.",
                exit_code=EXIT_USAGE,
            )
        return explicit
    for env_var in env_vars:
        raw_val = env.get(env_var, "").strip()
        if not raw_val:
            continue
        try:
            parsed = int(raw_val)
        except ValueError as exc:
            raise CliError(
                "INVALID_CAP",
                f"Environment variable {env_var}={raw_val!r} must be an integer.",
                f"Set {env_var} to 0 or a positive integer.",
                exit_code=EXIT_USAGE,
            ) from exc
        if parsed < 0:
            raise CliError(
                "INVALID_CAP",
                f"Environment variable {env_var}={parsed} cannot be negative.",
                f"Set {env_var} to 0 or a positive integer.",
                exit_code=EXIT_USAGE,
            )
        return parsed
    return default


def _resolve_default_cwd(explicit_cwd: str | None, fallback_pane: PaneInfo | None) -> str:
    if explicit_cwd:
        return explicit_cwd
    if fallback_pane and fallback_pane.cwd:
        return fallback_pane.cwd
    return os.getcwd()


def _dispatch_tab(
    ns: argparse.Namespace,
    client: HerdrSocketClient,
    env: dict[str, str],
    mode: OutputMode,
) -> str:
    sub = ns.tab_command
    raw_snap = client.snapshot()
    caller = resolve_caller_context(raw_snap, env)
    tabs = parse_workspace_tabs(
        raw_snap,
        caller.workspace_id,
        self_tab_id=caller.tab_id,
        self_pane_id=caller.pane_id,
    )

    if sub == "list":
        if ns.verify_busy:
            tabs = verify_tabs_claude_busy(tabs, client.read_detection)
        return format_tab_list(caller, tabs, mode)

    if sub == "spawn":
        if ns.ready_timeout <= 0:
            raise CliError(
                "INVALID_TIMEOUT",
                "--ready-timeout must be positive.",
                "Pass e.g. `--ready-timeout 45`.",
                exit_code=EXIT_USAGE,
            )
        prompt = _read_prompt_arg(ns.prompt, sys.stdin.isatty()) if ns.prompt is not None else None
        caller_tab = next((t for t in tabs if t.tab_id == caller.tab_id), None)
        caller_pane = (
            next((p for p in caller_tab.panes if p.pane_id == caller.pane_id), None)
            if caller_tab
            else None
        )
        cwd = _resolve_default_cwd(ns.cwd, caller_pane)
        plan = plan_tab_spawn(
            caller=caller,
            tabs=tabs,
            label=ns.label,
            cwd=cwd,
            launcher_name=ns.launcher,
            prompt=prompt,
            max_sub_tabs=_resolve_cap(
                ns.max_sub_tabs,
                env,
                ("HCMD_MAX_SUB_TABS", "HSS_MAX_SUB_TABS"),
                DEFAULT_MAX_SUB_TABS,
            ),
            max_space_agents=_resolve_cap(
                ns.max_space_agents,
                env,
                ("HCMD_MAX_SPACE_AGENTS", "HSS_MAX_SPACE_AGENTS"),
                DEFAULT_MAX_SPACE_AGENTS,
            ),
            max_session_agents=_resolve_cap(
                ns.max_session_agents,
                env,
                ("HCMD_MAX_SESSION_AGENTS", "HSS_MAX_SESSION_AGENTS"),
                DEFAULT_MAX_SESSION_AGENTS,
            ),
            session_working_agents=count_session_working_agents(raw_snap),
            existing_agent_names=collect_live_agent_names(raw_snap),
        )
        result = execute_tab_spawn(
            plan,
            client=client,
            create_tab=tab_create,
            run_pane=pane_run,
            start_agent=agent_start,
            prompt_fn=agent_prompt,
            env=env,
            select_backend=ai_agent_select_backend,
            dry_run=bool(ns.dry_run),
            ready_timeout=float(ns.ready_timeout),
        )
        return format_tab_spawn(result, mode)

    if sub == "peek":
        if ns.lines <= 0:
            raise CliError(
                "INVALID_LINES",
                "--lines must be a positive integer.",
                "Pass e.g. `--lines 40`.",
                exit_code=EXIT_USAGE,
            )
        tab = resolve_tab_target(tabs, ns.target)
        if tab.primary_pane is None:
            raise CliError(
                "NO_PRIMARY_PANE",
                f"Tab {tab.tab_id} ({tab.base_label}) has no primary pane.",
                "Inspect the tab in Herdr.",
            )
        pane = tab.primary_pane
        read = client.read_pane(
            pane.pane_id,
            has_agent=pane.agent_kind is not None,
            source=ns.source,
            lines=ns.lines,
        )
        return format_peek(
            PeekResult(
                workspace_id=caller.workspace_id,
                label=tab.base_label,
                tab_id=tab.tab_id,
                pane_id=pane.pane_id,
                agent_kind=pane.agent_kind,
                agent_status=pane.effective_status,
                source=ns.source,
                lines=ns.lines,
                text=str(read["text"]),
            ),
            mode,
        )

    if sub == "send":
        if ns.timeout <= 0:
            raise CliError(
                "INVALID_TIMEOUT",
                "--timeout must be positive.",
                "Pass e.g. `--timeout 120`.",
                exit_code=EXIT_USAGE,
            )
        prompt = _read_prompt_arg(ns.prompt, sys.stdin.isatty())
        tab = resolve_tab_target(tabs, ns.target)
        if tab.primary_pane and tab.primary_pane.agent_kind == "claude":
            tab = verify_tabs_claude_busy([tab], client.read_detection)[0]
        result = execute_tab_send(
            caller=caller,
            tab=tab,
            prompt=prompt,
            wait=bool(ns.wait),
            timeout=float(ns.timeout),
            force=bool(ns.force),
            dry_run=bool(ns.dry_run),
            prompt_fn=agent_prompt,
        )
        return format_send(result, mode)

    if sub == "wait":
        if ns.timeout <= 0:
            raise CliError(
                "INVALID_TIMEOUT",
                "--timeout must be positive.",
                "Pass e.g. `--timeout 300`.",
                exit_code=EXIT_USAGE,
            )
        until = _parse_until(ns.until)
        result = execute_tab_wait(
            client=client,
            caller=caller,
            until=until,
            target=ns.target,
            timeout=float(ns.timeout),
            verify_busy=bool(ns.verify_busy),
        )
        return format_tab_wait(result, mode)

    if sub == "close":
        tab = resolve_tab_target(tabs, ns.target)
        result = execute_tab_close(
            caller=caller,
            tab=tab,
            dry_run=bool(ns.dry_run),
            close_tab=tab_close,
        )
        return format_tab_close(result, mode)

    raise CliError(
        "UNKNOWN_COMMAND",
        f"Unsupported tab command {sub!r}.",
        "Run `hcmd tab --help`.",
        exit_code=EXIT_USAGE,
    )


def _dispatch_pane(
    ns: argparse.Namespace,
    client: HerdrSocketClient,
    env: dict[str, str],
    mode: OutputMode,
) -> str:
    sub = ns.pane_command
    raw_snap = client.snapshot()
    caller = resolve_caller_context(raw_snap, env)
    tabs = parse_workspace_tabs(
        raw_snap,
        caller.workspace_id,
        self_tab_id=caller.tab_id,
        self_pane_id=caller.pane_id,
    )
    tab = next(t for t in tabs if t.tab_id == caller.tab_id)

    if sub == "list":
        panes = list(tab.panes)
        if ns.verify_busy:
            panes = verify_panes_claude_busy(panes, client.read_detection)
        return format_pane_list(caller, tab, panes, mode)

    if sub == "spawn":
        if ns.ready_timeout <= 0:
            raise CliError(
                "INVALID_TIMEOUT",
                "--ready-timeout must be positive.",
                "Pass e.g. `--ready-timeout 45`.",
                exit_code=EXIT_USAGE,
            )
        prompt = _read_prompt_arg(ns.prompt, sys.stdin.isatty()) if ns.prompt is not None else None
        caller_pane = next((p for p in tab.panes if p.pane_id == caller.pane_id), None)
        cwd = _resolve_default_cwd(ns.cwd, caller_pane)
        plan = plan_pane_spawn(
            caller=caller,
            tabs=tabs,
            cwd=cwd,
            launcher_name=ns.launcher,
            direction=ns.direction,
            shell_cmd=ns.shell_cmd,
            prompt=prompt,
            max_worker_panes=_resolve_cap(
                ns.max_worker_panes,
                env,
                ("HCMD_MAX_WORKER_PANES", "HSS_MAX_WORKER_PANES"),
                DEFAULT_MAX_WORKER_PANES,
            ),
            max_space_agents=_resolve_cap(
                ns.max_space_agents,
                env,
                ("HCMD_MAX_SPACE_AGENTS", "HSS_MAX_SPACE_AGENTS"),
                DEFAULT_MAX_SPACE_AGENTS,
            ),
            max_session_agents=_resolve_cap(
                ns.max_session_agents,
                env,
                ("HCMD_MAX_SESSION_AGENTS", "HSS_MAX_SESSION_AGENTS"),
                DEFAULT_MAX_SESSION_AGENTS,
            ),
            session_working_agents=count_session_working_agents(raw_snap),
            existing_agent_names=collect_live_agent_names(raw_snap),
        )
        result = execute_pane_spawn(
            plan,
            client=client,
            split_pane=pane_split,
            run_pane=pane_run,
            start_agent=agent_start,
            prompt_fn=agent_prompt,
            env=env,
            select_backend=ai_agent_select_backend,
            dry_run=bool(ns.dry_run),
            ready_timeout=float(ns.ready_timeout),
        )
        return format_pane_spawn(result, mode)

    if sub == "peek":
        if ns.lines <= 0:
            raise CliError(
                "INVALID_LINES",
                "--lines must be a positive integer.",
                "Pass e.g. `--lines 40`.",
                exit_code=EXIT_USAGE,
            )
        pane = resolve_pane_target(tab.panes, ns.target)
        read = client.read_pane(
            pane.pane_id,
            has_agent=pane.agent_kind is not None,
            source=ns.source,
            lines=ns.lines,
        )
        return format_peek(
            PeekResult(
                workspace_id=caller.workspace_id,
                label=tab.base_label,
                tab_id=tab.tab_id,
                pane_id=pane.pane_id,
                agent_kind=pane.agent_kind,
                agent_status=pane.effective_status,
                source=ns.source,
                lines=ns.lines,
                text=str(read["text"]),
            ),
            mode,
        )

    if sub == "send":
        if ns.timeout <= 0:
            raise CliError(
                "INVALID_TIMEOUT",
                "--timeout must be positive.",
                "Pass e.g. `--timeout 120`.",
                exit_code=EXIT_USAGE,
            )
        prompt = _read_prompt_arg(ns.prompt, sys.stdin.isatty())
        pane = resolve_pane_target(tab.panes, ns.target)
        if pane.agent_kind == "claude":
            pane = verify_panes_claude_busy([pane], client.read_detection)[0]
        result = execute_pane_send(
            caller=caller,
            pane=pane,
            prompt=prompt,
            wait=bool(ns.wait),
            timeout=float(ns.timeout),
            force=bool(ns.force),
            dry_run=bool(ns.dry_run),
            prompt_fn=agent_prompt,
        )
        return format_send(result, mode)

    if sub == "wait":
        if ns.timeout <= 0:
            raise CliError(
                "INVALID_TIMEOUT",
                "--timeout must be positive.",
                "Pass e.g. `--timeout 300`.",
                exit_code=EXIT_USAGE,
            )
        until = _parse_until(ns.until)
        result = execute_pane_wait(
            client=client,
            caller=caller,
            until=until,
            target=ns.target,
            timeout=float(ns.timeout),
            verify_busy=bool(ns.verify_busy),
        )
        return format_pane_wait(result, mode)

    if sub == "close":
        pane = resolve_pane_target(tab.panes, ns.target)
        result = execute_pane_close(
            caller=caller,
            pane=pane,
            dry_run=bool(ns.dry_run),
            close_pane=pane_close,
        )
        return format_pane_close(result, mode)

    raise CliError(
        "UNKNOWN_COMMAND",
        f"Unsupported pane command {sub!r}.",
        "Run `hcmd pane --help`.",
        exit_code=EXIT_USAGE,
    )


if __name__ == "__main__":
    raise SystemExit(main())
