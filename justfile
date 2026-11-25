mod hss

# Show available recipes.
default:
    @just --list --list-submodules

alias install := sync

# Install CLI tools and link the herdr-commander skill from main.
sync:
    @test -d .git || { echo "Run just sync from the primary checkout, not a task worktree." >&2; exit 1; }
    @test "$(git branch --show-current)" = main || { echo "Run just sync from main." >&2; exit 1; }
    just hss::install
    just install-skill

# Symlink skill/herdr-commander for shared agents, Claude, Codex, and Antigravity (agy).
install-skill:
    #!/usr/bin/env bash
    set -euo pipefail
    src="{{justfile_directory()}}/skill/herdr-commander"
    agents_dir="$HOME/.agents/skills"
    claude_dir="$HOME/.claude/skills"
    codex_dir="${CODEX_HOME:-$HOME/.codex}/skills"
    agy_dir="$HOME/.gemini/config/skills"
    mkdir -p "$agents_dir" "$claude_dir" "$codex_dir" "$agy_dir"

    link_safe() {
        local target="$1"
        local link_path="$2"
        if [ -e "$link_path" ] && [ ! -L "$link_path" ]; then
            echo "Refusing to overwrite non-symlink path: $link_path" >&2
            exit 1
        fi
        ln -sfn "$target" "$link_path"
        printf 'Linked %s -> %s\n' "$link_path" "$target"
    }

    link_safe "$src" "$agents_dir/herdr-commander"
    link_safe "../../.agents/skills/herdr-commander" "$claude_dir/herdr-commander"
    link_safe "$agents_dir/herdr-commander" "$codex_dir/herdr-commander"
    link_safe "$agents_dir/herdr-commander" "$agy_dir/herdr-commander"

# Check lint and formatting.
lint:
    uv run ruff check .
    uv run ruff format --check .

# Auto-fix lint and formatting.
fmt:
    uv run ruff check --fix .
    uv run ruff format .

# Run unit and CLI contract tests.
test:
    uv run python -m unittest discover -s tests -v

# Build distribution artifacts.
build:
    uv build

# Run all verification checks.
check: lint test build
