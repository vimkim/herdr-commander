# Show available hss recipes.
[private]
default:
    @just --list hss

# Install herdr-spaces-status and hss CLI tools.
install:
    uv tool install --reinstall .
    herdr-spaces-status --help >/dev/null
    hss --help >/dev/null

# Uninstall herdr-spaces-status and hss CLI tools.
uninstall:
    uv tool uninstall herdr-spaces-status

# Show local and installed hss versions and path.
version:
    @printf 'local:     '
    @uv run hss --version
    @if command -v hss >/dev/null 2>&1; then \
        printf 'installed: %s (%s)\n' "$(hss --version)" "$(command -v hss)"; \
    else \
        echo 'installed: not found on PATH'; \
    fi

# Show hss CLI help from the local checkout.
help:
    uv run hss --help

# Show all workspaces first-tab status.
status:
    uv run hss status

# Show all workspaces with full tab and pane inventory.
status-all:
    uv run hss status --all-tabs

# Show worker workspaces with busy verification and tracker correlation.
workers:
    uv run hss status --workers-only --verify-busy --with-tracker

# Show only worker workspaces needing attention.
attention:
    uv run hss status --workers-only --attention-only --verify-busy --with-tracker

# Show compact tab-separated status summary.
plain:
    uv run hss status --plain --fields number,workspace_id,attention,agent_kind,label

# Show commander preflight JSON status for worker workspaces.
status-json:
    uv run hss status --workers-only --verify-busy --with-tracker --json

# Wait until any worker first tab becomes blocked or done.
wait:
    uv run hss wait --until blocked,done --timeout 300 --verify-busy
