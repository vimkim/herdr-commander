# Show available hcmd recipes.
[private]
default:
    @just --list hcmd

# Install herdr-commander, hcmd, and legacy hss CLI tools.
install:
    uv tool install --reinstall .
    herdr-commander --help >/dev/null
    hcmd --help >/dev/null
    hss --help >/dev/null

# Uninstall herdr-commander, hcmd, and hss CLI tools.
uninstall:
    uv tool uninstall herdr-commander

# Show local and installed hcmd versions and path.
version:
    @printf 'local:     '
    @uv run hcmd --version
    @if command -v hcmd >/dev/null 2>&1; then \
        printf 'installed: %s (%s)\n' "$(hcmd --version)" "$(command -v hcmd)"; \
    else \
        echo 'installed: not found on PATH'; \
    fi

# Show hcmd CLI help from the local checkout.
help:
    uv run hcmd --help

# Show all workspaces first-tab status.
status:
    uv run hcmd status

# Show all workspaces with full tab and pane inventory.
status-all:
    uv run hcmd status --all-tabs

# Show worker workspaces with busy verification and tracker correlation.
workers:
    uv run hcmd status --workers-only --verify-busy --with-tracker

# Show only worker workspaces needing attention.
attention:
    uv run hcmd status --workers-only --attention-only --verify-busy --with-tracker

# Show compact tab-separated status summary.
plain:
    uv run hcmd status --plain --fields number,workspace_id,attention,agent_kind,label

# Show commander preflight JSON status for worker workspaces.
status-json:
    uv run hcmd status --workers-only --verify-busy --with-tracker --json

# Show non-blocking worker digest with attention/done peek snippets.
digest:
    uv run hcmd digest

# Show compact delta digest since last cursor watermark.
digest-delta:
    uv run hcmd digest --since-last --compact

# Inspect calling pane context-window telemetry.
self-context:
    uv run hcmd self-context --rotate-ctx 30

# Opt-in blocking wait until any worker first tab becomes blocked or done.
wait:
    uv run hcmd wait --until blocked,done --timeout 300 --verify-busy
