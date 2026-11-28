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
    format_digest,
    format_ensure,
    format_error,
    format_find,
    format_pane_close,
    format_pane_list,
    format_pane_spawn,
    format_pane_wait,
    format_peek,
    format_rotate,
    format_self_context,
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
    agent_send_keys,
    agent_start,
    ai_agent_select_backend,
    create_workspace_cubrid_pr,
    create_workspace_cubrid_ticket,
    create_workspace_cwd,
    delete_digest_cursor,
    ensure_trusted_workspace,
    fetch_work_tracker_items,
    load_digest_cursor,
    load_handoff_state,
    pane_close,
    pane_run,
    pane_split,
    read_recent_audit_lines,
    resolve_cursor_path,
    resolve_cursor_scope,
    resolve_handoff_paths,
    resolve_socket_path,
    resolve_state_dir,
    save_digest_cursor,
    save_handoff_state,
    tab_close,
    tab_create,
    workspace_env_overrides,
)
from .models import (
    DEFAULT_MAX_SESSION_AGENTS,
    DEFAULT_MAX_SPACE_AGENTS,
    DEFAULT_MAX_SUB_TABS,
    DEFAULT_MAX_WORKER_PANES,
    DEFAULT_ROTATE_COOLDOWN,
    DEFAULT_ROTATE_CTX,
    DEFAULT_WAIT_STATES,
    EXIT_DOMAIN,
    EXIT_OK,
    EXIT_SIGINT,
    EXIT_USAGE,
    VALID_ON_EXISTING_POLICIES,
    VALID_SORT_MODES,
    VALID_SPLIT_DIRECTIONS,
    VALID_WAIT_STATES,
    CliError,
    PaneInfo,
    PeekResult,
    SelfContextResult,
)
from .orchestrate import (
    LAUNCHERS,
    TrustWorkspaceFn,
    compose_send_prompt,
    execute_digest_with_cursor,
    execute_ensure,
    execute_pane_close,
    execute_pane_send,
    execute_pane_spawn,
    execute_pane_wait,
    execute_rotate,
    execute_rotate_ack,
    execute_send,
    execute_tab_close,
    execute_tab_send,
    execute_tab_spawn,
    execute_tab_wait,
    execute_wait_with_cursor,
    plan_ensure,
    plan_pane_spawn,
    plan_rotate,
    plan_tab_spawn,
    verify_tabs_claude_busy,
)
from .snapshot import (
    collect_live_agent_names,
    collect_workspace_panes,
    compute_find_recommendation,
    correlate_work_tracker,
    count_session_working_agents,
    filter_spaces,
    find_matching_spaces,
    parse_self_context_statusline,
    parse_snapshot,
    parse_workspace_tabs,
    resolve_caller_context,
    resolve_pane_target,
    resolve_peek_target,
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
  hcmd digest --json
  hcmd status --workers-only --verify-busy --with-tracker
  hcmd status --plain --fields number,workspace_id,attention,label
  hcmd find --label my-repo --cwd ~/gh/my-repo --verify-busy --json
  hcmd peek w1 --lines 40
  hcmd peek w1 --tab review --lines 60
  hcmd ensure --ticket CBRD-10002 --on-existing fail --launcher ai-agent --json
  hcmd send w1 --prompt "Summarize findings in tab 1." --json
  hcmd wait --until blocked,done --timeout 300 --verify-busy --json  # opt-in only
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

    p_digest = subparsers.add_parser(
        "digest",
        parents=[common],
        help="Non-blocking summary and harvested snippets for attention/done worker Spaces",
    )
    p_digest.add_argument(
        "--all",
        action="store_true",
        help="Include all watched workspaces instead of only blocked/done/blocked-subtabs",
    )
    p_digest.add_argument(
        "--include-idle",
        action="store_true",
        help="Include idle agent workspaces (and peek them) in non-delta or initial baseline runs",
    )
    p_digest.add_argument(
        "--since-last",
        action="store_true",
        help="Only surface workspaces changed since the persisted cursor (including working->idle)",
    )
    p_digest.add_argument(
        "--cursor",
        default=None,
        metavar="NAME",
        help="Named cursor slot for --since-last (default: scoped by socket + caller workspace)",
    )
    p_digest.add_argument(
        "--no-advance",
        action="store_true",
        help="Read --since-last delta without updating the persisted cursor",
    )
    p_digest.add_argument(
        "--reset-cursor",
        action="store_true",
        help="Delete the persisted cursor for this scope before running",
    )
    p_digest.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact workspace metadata and bounded reporting-tail peeks",
    )
    p_digest.add_argument(
        "--max-peek-chars",
        type=int,
        default=None,
        help="Bound each peek snippet to at most N characters (default under --compact: 1500)",
    )
    p_digest.add_argument("--workspace", help="Digest one workspace ID, label, or ticket")
    p_digest.add_argument(
        "--include-self",
        action="store_true",
        help="Include the caller's own HERDR_WORKSPACE_ID",
    )
    p_digest.add_argument(
        "--no-verify-busy",
        dest="verify_busy",
        action="store_false",
        default=True,
        help="Skip Claude detection-buffer check for background busy panes",
    )
    p_digest.add_argument(
        "--no-tracker",
        dest="with_tracker",
        action="store_false",
        default=True,
        help="Skip work-tracker correlation",
    )
    p_digest.add_argument(
        "--peek-lines",
        type=int,
        default=None,
        help=(
            "Lines to peek for attention/target workspaces "
            "(default: 40, or 20 with --compact; 0 disables)"
        ),
    )
    p_digest.add_argument(
        "--source",
        choices=("visible", "recent", "recent-unwrapped", "detection"),
        default="recent-unwrapped",
        help="Herdr read source (default: recent-unwrapped)",
    )

    p_peek = subparsers.add_parser(
        "peek",
        parents=[common],
        help="Read recent output from a workspace pane (default: first-tab pane)",
    )
    p_peek.add_argument("target", help="Workspace ID, label, or ticket key")
    p_peek.add_argument(
        "--tab",
        default=None,
        help="Optional tab ID (w1:t2 or t2), 1-based tab index, or role label",
    )
    p_peek.add_argument(
        "--pane",
        default=None,
        help="Optional pane ID (w1:p2 or p2), 1-based pane index, or agent name",
    )
    p_peek.add_argument("--lines", type=int, default=40, help="Lines to read (default: 40)")
    p_peek.add_argument(
        "--source",
        choices=("visible", "recent", "recent-unwrapped", "detection"),
        default="recent-unwrapped",
        help="Herdr read source (default: recent-unwrapped)",
    )
    p_peek.add_argument(
        "--verify-busy",
        action="store_true",
        help="Inspect Claude detection buffer to flag background-busy panes as busy_bg",
    )

    p_wait = subparsers.add_parser(
        "wait",
        parents=[common],
        help=(
            "Opt-in blocking wait until watched first-tab agents enter target states "
            "(prefer async status/digest by default)"
        ),
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
    p_wait.add_argument(
        "--since-last",
        action="store_true",
        help="Wait for state/output transitions relative to persisted digest cursor",
    )
    p_wait.add_argument(
        "--cursor",
        default=None,
        metavar="NAME",
        help="Named cursor slot when --since-last is enabled",
    )
    p_wait.add_argument(
        "--no-advance",
        action="store_true",
        help="Do not update the persisted cursor on --since-last completion",
    )
    p_wait.add_argument(
        "--changes-only",
        action="store_true",
        help="Ignore already-settled workspaces at startup and wake only on new transitions",
    )
    p_wait.add_argument(
        "--include-subtabs",
        action="store_true",
        help="Watch all sub-tab/split panes in addition to Tab 1 and wake on blocked sub-tabs",
    )
    p_wait.add_argument(
        "--stop-when-idle",
        action="store_true",
        help="Exit immediately (reason=no_active_work) when no watched agent is working/busy_bg",
    )
    p_wait.add_argument(
        "--allow-timeout",
        action="store_true",
        help="Return exit code 0 with timed_out=true instead of WAIT_TIMEOUT error on timeout",
    )
    p_wait.add_argument(
        "--emit-digest",
        action="store_true",
        help="Attach a delta/attention digest payload when waking up",
    )
    p_wait.add_argument(
        "--compact",
        action="store_true",
        help="Use compact mode for the embedded --emit-digest payload",
    )
    p_wait.add_argument(
        "--peek-lines",
        type=int,
        default=None,
        help="Lines to peek when --emit-digest or --since-last is enabled",
    )
    p_wait.add_argument(
        "--max-peek-chars",
        type=int,
        default=None,
        help="Max characters per peek in embedded --emit-digest output",
    )
    p_wait.add_argument(
        "--source",
        choices=("visible", "recent", "recent-unwrapped", "detection"),
        default="recent-unwrapped",
        help="Herdr read source for embedded digest (default: recent-unwrapped)",
    )
    p_wait.add_argument(
        "--with-tracker",
        action="store_true",
        help="Correlate watched workspaces with open work-tracker items for embedded digest",
    )

    p_ensure = subparsers.add_parser(
        "ensure",
        parents=[common],
        help="Idempotently ensure a workspace and first-tab agent are running",
    )
    p_ensure.add_argument("--label", help="Workspace label")
    p_ensure.add_argument(
        "--cwd",
        "--pwd",
        dest="cwd",
        help="Existing directory for a non-CUBRID workspace (alias: --pwd)",
    )
    p_ensure.add_argument("--ticket", help="CUBRID JIRA ticket key (e.g. CBRD-10002)")
    p_ensure.add_argument("--pr", help="CUBRID PR number for review worktree")
    p_ensure.add_argument("--base", help="Base ref for `cubrid-worktree create --base`")
    p_ensure.add_argument(
        "--on-existing",
        choices=VALID_ON_EXISTING_POLICIES,
        default=None,
        help=(
            "Policy when a matching workspace exists: reuse (default), fail "
            "(return EXISTING_WORKSPACE_FOUND with candidates), or new "
            "(create a fresh cwd workspace)"
        ),
    )
    p_ensure.add_argument(
        "--reuse",
        metavar="TARGET",
        help="Explicitly reuse an existing workspace ID, label, or ticket and ensure its agent",
    )
    p_ensure.add_argument(
        "--new",
        action="store_true",
        help="Shorthand for `--on-existing new`",
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
        "--no-trust",
        dest="auto_trust",
        action="store_false",
        default=True,
        help="Skip pre-trusting the workspace directory in Claude/Codex/agy configs",
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
        "--kickoff",
        action="store_true",
        help=(
            "Prepend Minimal Commander Context Envelope for initial Space Lead dispatch "
            "(omit on ordinary follow-ups)"
        ),
    )
    p_send.add_argument(
        "--tracker",
        default=None,
        metavar="ID",
        help="Optional work-tracker ID included in the --kickoff envelope",
    )
    p_send.add_argument(
        "--context",
        action="append",
        default=[],
        metavar="TEXT",
        help="Repeatable guardrail note (shared locks, sibling ownership) for --kickoff",
    )
    p_send.add_argument(
        "--wait",
        action="store_true",
        help="Opt-in blocking wait for agent completion (default is async fire-and-return)",
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
        help="Manage sub-tabs inside the caller's Space (Space Lead only)",
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
        "--no-trust",
        dest="auto_trust",
        action="store_false",
        default=True,
        help="Skip pre-trusting the target directory in Claude/Codex/agy configs",
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
        help="Manage worker panes inside the caller's tab (Space/Tab Lead only)",
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
        "--no-trust",
        dest="auto_trust",
        action="store_false",
        default=True,
        help="Skip pre-trusting the target directory in Claude/Codex/agy configs",
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

    p_self_ctx = subparsers.add_parser(
        "self-context",
        parents=[common],
        help="Inspect the calling pane's statusline context percentage and token telemetry",
    )
    p_self_ctx.add_argument(
        "--rotate-ctx",
        type=int,
        default=None,
        help="Optional context percentage threshold (1-100) to compute should_rotate",
    )
    p_self_ctx.add_argument(
        "--lines",
        type=int,
        default=15,
        help="Terminal lines to read from the calling pane (default: 15)",
    )

    p_rotate = subparsers.add_parser(
        "rotate",
        parents=[common],
        help="2-phase same-tab Commander pane rotation with handoff state and --ack retirement",
    )
    p_rotate.add_argument(
        "--ack",
        default=None,
        metavar="PANE_ID",
        help="Phase 2: acknowledge takeover from successor pane and close the old pane",
    )
    p_rotate.add_argument(
        "--launcher",
        default="ay",
        choices=tuple(LAUNCHERS.keys()),
        help="Successor pane agent launcher (default: ay)",
    )
    p_rotate.add_argument(
        "--direction",
        default="right",
        choices=VALID_SPLIT_DIRECTIONS,
        help="Pane split direction inside caller tab (default: right)",
    )
    p_rotate.add_argument(
        "--cooldown",
        type=float,
        default=DEFAULT_ROTATE_COOLDOWN,
        help=f"Minimum seconds between rotations (default: {int(DEFAULT_ROTATE_COOLDOWN)})",
    )
    p_rotate.add_argument(
        "--rotate-ctx",
        type=int,
        default=DEFAULT_ROTATE_CTX,
        help=f"Threshold propagated into handoff for /hcmd-auto (default: {DEFAULT_ROTATE_CTX})",
    )
    p_rotate.add_argument(
        "--cursor",
        default=None,
        metavar="NAME",
        help="Digest cursor name carried in handoff state",
    )
    p_rotate.add_argument(
        "--auto-config",
        default=None,
        help="Optional /hcmd-auto policy/config summary recorded in handoff",
    )
    p_rotate.add_argument(
        "--decision",
        action="append",
        default=[],
        help="Pending human decision to record in handoff (repeatable)",
    )
    p_rotate.add_argument(
        "--note",
        default=None,
        help="Optional freeform handoff note for the successor pane",
    )
    p_rotate.add_argument(
        "--ready-timeout",
        type=float,
        default=45.0,
        help="Seconds to wait for successor agent startup readiness (default: 45)",
    )
    p_rotate.add_argument(
        "--verify-timeout",
        type=float,
        default=10.0,
        help="Seconds to verify successor accepted bootstrap prompt (default: 10)",
    )
    p_rotate.add_argument(
        "--no-trust",
        dest="auto_trust",
        action="store_false",
        default=True,
        help="Skip pre-trusting the workspace directory before launch",
    )
    p_rotate.add_argument(
        "--force",
        action="store_true",
        help="Bypass rotation cooldown or handoff matching guards",
    )
    p_rotate.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate rotation plan or ACK without splitting or closing panes",
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
            updated_matches = tuple(
                replace(match, space=space)
                for match, space in zip(result.matches, matched_spaces, strict=True)
            )
            result = replace(
                result,
                matches=updated_matches,
                recommendation=compute_find_recommendation(updated_matches),
            )
        return format_find(result, mode, warnings=warnings)

    if cmd == "digest":
        if ns.no_advance and not ns.since_last:
            raise CliError(
                "INVALID_FLAGS",
                "--no-advance requires --since-last.",
                "Add `--since-last` or remove `--no-advance`.",
                exit_code=EXIT_USAGE,
            )
        peek_lines = ns.peek_lines if ns.peek_lines is not None else (20 if ns.compact else 40)
        if peek_lines < 0:
            raise CliError(
                "INVALID_LINES",
                "--peek-lines must be 0 or a positive integer.",
                "Pass e.g. `--peek-lines 40` or `--peek-lines 0`.",
                exit_code=EXIT_USAGE,
            )
        if ns.max_peek_chars is not None and ns.max_peek_chars <= 0:
            raise CliError(
                "INVALID_MAX_PEEK_CHARS",
                "--max-peek-chars must be a positive integer.",
                "Pass e.g. `--max-peek-chars 1500`.",
                exit_code=EXIT_USAGE,
            )

        cursor_scope = resolve_cursor_scope(client.socket_path, self_workspace_id, ns.cursor)
        state_dir = resolve_state_dir(env)
        cursor_path = resolve_cursor_path(state_dir, cursor_scope)
        if ns.reset_cursor:
            delete_digest_cursor(cursor_path)

        prev_cursor = None
        warnings: list[str] = []
        if ns.since_last:
            prev_cursor, cursor_warn = load_digest_cursor(cursor_path)
            if cursor_warn:
                warnings.append(cursor_warn)
                if mode.fmt != "json":
                    sys.stderr.write(f"Warning: {cursor_warn}\n")

        spaces = parse_snapshot(client.snapshot(), self_workspace_id=self_workspace_id)
        if ns.verify_busy:
            spaces = verify_claude_busy(spaces, client.read_detection)
        if ns.with_tracker:
            items, warn = fetch_work_tracker_items()
            if warn:
                warnings.append(warn)
                if mode.fmt != "json":
                    sys.stderr.write(f"Warning: {warn}\n")
            spaces = correlate_work_tracker(spaces, items)
        filtered = filter_spaces(
            spaces,
            workers_only=not bool(ns.include_self),
            workspace=ns.workspace,
            sort="attention",
        )
        result, next_cursor, digest_warnings = execute_digest_with_cursor(
            filtered,
            read_pane=client.read_pane,
            include_all=bool(ns.all),
            include_idle=bool(ns.include_idle),
            workspace=ns.workspace,
            peek_lines=peek_lines,
            peek_source=ns.source,
            compact=bool(ns.compact),
            max_peek_chars=ns.max_peek_chars,
            since_last=bool(ns.since_last),
            prev_cursor=prev_cursor,
            cursor_scope=cursor_scope,
        )
        if ns.since_last and not ns.no_advance:
            save_digest_cursor(cursor_path, next_cursor)
        for warn in digest_warnings:
            warnings.append(warn)
            if mode.fmt != "json":
                sys.stderr.write(f"Warning: {warn}\n")
        return format_digest(result, mode, warnings=warnings)

    if cmd == "peek":
        if ns.lines <= 0:
            raise CliError(
                "INVALID_LINES",
                "--lines must be a positive integer.",
                "Pass e.g. `--lines 40`.",
                exit_code=EXIT_USAGE,
            )
        raw_snap = client.snapshot()
        spaces = parse_snapshot(raw_snap, self_workspace_id=self_workspace_id)
        space, resolved_tab_id, pane = resolve_peek_target(
            raw_snap,
            spaces,
            ns.target,
            tab_selector=ns.tab,
            pane_selector=ns.pane,
        )
        if ns.verify_busy and pane.agent_kind == "claude" and pane.raw_status in {"idle", "done"}:
            pane = verify_panes_claude_busy([pane], client.read_detection)[0]
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
                tab_id=resolved_tab_id,
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
        if ns.no_advance and not ns.since_last:
            raise CliError(
                "INVALID_FLAGS",
                "--no-advance requires --since-last.",
                "Add `--since-last` or remove `--no-advance`.",
                exit_code=EXIT_USAGE,
            )
        peek_lines = ns.peek_lines if ns.peek_lines is not None else (20 if ns.compact else 40)
        if peek_lines < 0:
            raise CliError(
                "INVALID_LINES",
                "--peek-lines must be 0 or a positive integer.",
                "Pass e.g. `--peek-lines 20` or `--peek-lines 0`.",
                exit_code=EXIT_USAGE,
            )
        if ns.max_peek_chars is not None and ns.max_peek_chars <= 0:
            raise CliError(
                "INVALID_MAX_PEEK_CHARS",
                "--max-peek-chars must be a positive integer.",
                "Pass e.g. `--max-peek-chars 1500`.",
                exit_code=EXIT_USAGE,
            )

        until = _parse_until(ns.until)
        cursor_scope = resolve_cursor_scope(client.socket_path, self_workspace_id, ns.cursor)
        state_dir = resolve_state_dir(env)
        cursor_path = resolve_cursor_path(state_dir, cursor_scope)
        prev_cursor = None
        if ns.since_last:
            prev_cursor, cursor_warn = load_digest_cursor(cursor_path)
            if cursor_warn and mode.fmt != "json":
                sys.stderr.write(f"Warning: {cursor_warn}\n")

        tracker_items: list[dict[str, object]] = []
        if ns.with_tracker:
            loaded_items, warn = fetch_work_tracker_items()
            if warn and mode.fmt != "json":
                sys.stderr.write(f"Warning: {warn}\n")
            tracker_items = loaded_items

        result, next_cursor, wait_warnings = execute_wait_with_cursor(
            client=client,
            until=until,
            workspace=ns.workspace,
            workers_only=not bool(ns.all_workspaces),
            timeout=float(ns.timeout),
            verify_busy=bool(ns.verify_busy),
            self_workspace_id=self_workspace_id,
            since_last=bool(ns.since_last),
            prev_cursor=prev_cursor,
            cursor_scope=cursor_scope,
            changes_only=bool(ns.changes_only),
            include_subtabs=bool(ns.include_subtabs),
            stop_when_idle=bool(ns.stop_when_idle),
            allow_timeout=bool(ns.allow_timeout),
            emit_digest=bool(ns.emit_digest),
            compact=bool(ns.compact),
            peek_lines=peek_lines,
            peek_source=ns.source,
            max_peek_chars=ns.max_peek_chars,
            read_pane=getattr(client, "read_pane", None),
            tracker_items=tracker_items,
        )
        if ns.since_last and not ns.no_advance and next_cursor is not None:
            save_digest_cursor(cursor_path, next_cursor)
        if mode.fmt != "json":
            for warn in wait_warnings:
                sys.stderr.write(f"Warning: {warn}\n")
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
        panes_by_workspace = collect_workspace_panes(raw_snap)
        plan = plan_ensure(
            spaces,
            label=ns.label,
            cwd=ns.cwd,
            ticket=ns.ticket,
            pr_number=ns.pr,
            launcher_name=ns.launcher,
            base_ref=ns.base,
            on_existing=ns.on_existing,
            force_new=bool(ns.new),
            reuse_target=ns.reuse,
            panes_by_workspace=panes_by_workspace,
            read_detection=client.read_detection,
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
            trust_workspace=_make_trust_callback(bool(ns.auto_trust), mode),
            read_pane=getattr(client, "read_pane", None),
            send_keys=agent_send_keys,
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
        if (ns.tracker is not None or ns.context) and not ns.kickoff:
            raise CliError(
                "INVALID_FLAGS",
                "--tracker and --context require --kickoff.",
                (
                    "Add `--kickoff` for initial Space Lead dispatch, "
                    "or omit `--tracker`/`--context` for plain follow-ups."
                ),
                exit_code=EXIT_USAGE,
            )
        raw_prompt = _read_prompt_arg(ns.prompt, sys.stdin.isatty())
        spaces = parse_snapshot(client.snapshot(), self_workspace_id=self_workspace_id)
        space = resolve_target(spaces, ns.target)
        if space.primary_pane and space.primary_pane.agent_kind == "claude":
            verified = verify_claude_busy([space], client.read_detection)
            space = verified[0]
        final_prompt = compose_send_prompt(
            raw_prompt,
            space=space,
            kickoff=bool(ns.kickoff),
            commander_workspace_id=self_workspace_id,
            tracker_id=ns.tracker,
            context_notes=ns.context,
        )
        result = execute_send(
            space,
            prompt=final_prompt,
            wait=bool(ns.wait),
            timeout=float(ns.timeout),
            force=bool(ns.force),
            dry_run=bool(ns.dry_run),
            prompt_fn=agent_prompt,
            kickoff=bool(ns.kickoff),
        )
        return format_send(result, mode)

    if cmd == "self-context":
        if ns.lines <= 0:
            raise CliError(
                "INVALID_LINES",
                "--lines must be a positive integer.",
                "Pass e.g. `--lines 15`.",
                exit_code=EXIT_USAGE,
            )
        if ns.rotate_ctx is not None and not (1 <= ns.rotate_ctx <= 100):
            raise CliError(
                "INVALID_ROTATE_CTX",
                f"--rotate-ctx must be between 1 and 100, got {ns.rotate_ctx}.",
                "Pass e.g. `--rotate-ctx 30`.",
                exit_code=EXIT_USAGE,
            )
        raw_snap = client.snapshot()
        caller = resolve_caller_context(raw_snap, env)
        tabs = parse_workspace_tabs(
            raw_snap,
            caller.workspace_id,
            self_tab_id=caller.tab_id,
            self_pane_id=caller.pane_id,
        )
        caller_tab = next(t for t in tabs if t.tab_id == caller.tab_id)
        caller_pane = next(p for p in caller_tab.panes if p.pane_id == caller.pane_id)

        source_used = "visible"
        read = client.read_pane(
            caller.pane_id,
            has_agent=caller_pane.agent_kind is not None,
            source="visible",
            lines=ns.lines,
        )
        parsed_ctx = parse_self_context_statusline(str(read["text"]))
        if not parsed_ctx["parsed"]:
            recent_read = client.read_pane(
                caller.pane_id,
                has_agent=caller_pane.agent_kind is not None,
                source="recent",
                lines=max(ns.lines, 25),
            )
            recent_parsed = parse_self_context_statusline(str(recent_read["text"]))
            if recent_parsed["parsed"]:
                parsed_ctx = recent_parsed
                source_used = "recent"

        should_rotate: bool | None = None
        if ns.rotate_ctx is not None:
            should_rotate = bool(
                parsed_ctx["parsed"]
                and parsed_ctx["ctx_percent"] is not None
                and parsed_ctx["ctx_percent"] >= ns.rotate_ctx
            )

        return format_self_context(
            SelfContextResult(
                workspace_id=caller.workspace_id,
                tab_id=caller.tab_id,
                pane_id=caller.pane_id,
                agent_kind=caller_pane.agent_kind,
                source=source_used,
                parsed=bool(parsed_ctx["parsed"]),
                model_display=parsed_ctx["model_display"],
                ctx_percent=parsed_ctx["ctx_percent"],
                tokens_used=parsed_ctx["tokens_used"],
                tokens_limit=parsed_ctx["tokens_limit"],
                tokens_display=parsed_ctx["tokens_display"],
                rate_limits=parsed_ctx["rate_limits"],
                statusline=parsed_ctx["statusline"],
                rotate_ctx=ns.rotate_ctx,
                should_rotate=should_rotate,
            ),
            mode,
        )

    if cmd == "rotate":
        if ns.ready_timeout <= 0 or ns.verify_timeout <= 0:
            raise CliError(
                "INVALID_TIMEOUT",
                "--ready-timeout and --verify-timeout must be positive.",
                "Pass positive durations such as `--ready-timeout 45 --verify-timeout 10`.",
                exit_code=EXIT_USAGE,
            )
        raw_snap = client.snapshot()
        caller = resolve_caller_context(raw_snap, env)
        tabs = parse_workspace_tabs(
            raw_snap,
            caller.workspace_id,
            self_tab_id=caller.tab_id,
            self_pane_id=caller.pane_id,
        )
        state_dir = resolve_state_dir(env)
        handoff_json_path, handoff_md_path = resolve_handoff_paths(state_dir, caller.workspace_id)
        prev_handoff = load_handoff_state(handoff_json_path)

        def _save_handoff(payload: Mapping[str, object], markdown: str) -> None:
            save_handoff_state(handoff_json_path, handoff_md_path, payload, markdown)

        if ns.ack is not None:
            result = execute_rotate_ack(
                caller=caller,
                tabs=tabs,
                ack_pane_id=ns.ack,
                prev_handoff=prev_handoff,
                handoff_json_path=str(handoff_json_path),
                handoff_md_path=str(handoff_md_path),
                save_handoff=_save_handoff,
                close_pane=pane_close,
                force=bool(ns.force),
                dry_run=bool(ns.dry_run),
            )
            return format_rotate(result, mode)

        plan = plan_rotate(
            caller=caller,
            tabs=tabs,
            launcher_name=ns.launcher,
            direction=ns.direction,
            rotate_ctx=ns.rotate_ctx,
            cooldown=float(ns.cooldown),
            prev_handoff=prev_handoff,
            cursor_name=ns.cursor,
            auto_config=ns.auto_config,
            pending_decisions=ns.decision,
            note=ns.note,
            force=bool(ns.force),
        )
        recent_audit = read_recent_audit_lines(state_dir, limit=10)
        result = execute_rotate(
            plan,
            client=client,
            split_pane=pane_split,
            run_pane=pane_run,
            start_agent=agent_start,
            prompt_fn=agent_prompt,
            env=env,
            handoff_json_path=str(handoff_json_path),
            handoff_md_path=str(handoff_md_path),
            save_handoff=_save_handoff,
            recent_audit=recent_audit,
            select_backend=ai_agent_select_backend,
            trust_workspace=_make_trust_callback(bool(ns.auto_trust), mode),
            read_pane=client.read_pane,
            send_keys=agent_send_keys,
            dry_run=bool(ns.dry_run),
            ready_timeout=float(ns.ready_timeout),
            verify_timeout=float(ns.verify_timeout),
        )
        return format_rotate(result, mode)

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


def _make_trust_callback(
    auto_trust: bool,
    mode: OutputMode,
) -> TrustWorkspaceFn | None:
    if not auto_trust:
        return None

    def _trust(cwd: str, agent_kinds: Sequence[str], env: Mapping[str, str]) -> list[str]:
        warnings = ensure_trusted_workspace(cwd, agent_kinds, env)
        if mode.fmt != "json":
            for warn in warnings:
                sys.stderr.write(f"Warning: {warn}\n")
        return warnings

    return _trust


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
            trust_workspace=_make_trust_callback(bool(ns.auto_trust), mode),
            read_pane=getattr(client, "read_pane", None),
            send_keys=agent_send_keys,
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
            trust_workspace=_make_trust_callback(bool(ns.auto_trust), mode),
            read_pane=getattr(client, "read_pane", None),
            send_keys=agent_send_keys,
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
