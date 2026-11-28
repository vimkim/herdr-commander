mod hcmd

# Show available recipes.
default:
    @just --list --list-submodules

alias install := sync

# Install CLI tools and copy bundled skills from main.
sync:
    @test -d .git || { echo "Run just sync from the primary checkout, not a task worktree." >&2; exit 1; }
    @test "$(git branch --show-current)" = main || { echo "Run just sync from main." >&2; exit 1; }
    just hcmd::install
    "{{just_executable()}}" install-skills

alias install-skill := install-skills

# Install bundled skills (copy to ~/.agents/skills and link for Claude, Codex, and Antigravity).
install-skills:
    #!/usr/bin/env bash
    set -euo pipefail
    skill_root="{{justfile_directory()}}/skills"
    agents_dir="$HOME/.agents/skills"
    claude_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
    codex_dir="${CODEX_HOME:-$HOME/.codex}/skills"
    agy_dir="$HOME/.gemini/config/skills"
    mkdir -p "$agents_dir" "$claude_dir" "$codex_dir" "$agy_dir"

    link_agent() {
        local target="$1"
        local dir="$2"
        local name="$3"
        local link_path="$dir/$name"
        if [ -L "$link_path" ] || [ -e "$link_path" ]; then
            rm -rf "${dir:?}/${name:?}"
        fi
        ln -s "$target" "$link_path"
    }

    for src in "$skill_root"/*; do
        [ -d "$src" ] && [ -f "$src/SKILL.md" ] || continue
        name="$(basename "$src")"
        staging="$(mktemp -d "${agents_dir}/.${name}.XXXXXX")"
        cp -R "$src/." "$staging/"
        find "$staging" -type f -name '*.pyc' -delete
        find "$staging" -type d -name '__pycache__' -empty -delete
        if [ -L "$agents_dir/$name" ] || [ -e "$agents_dir/$name" ]; then
            rm -rf "${agents_dir:?}/${name:?}"
        fi
        mv "$staging" "$agents_dir/$name"
        if [ "$claude_dir" = "$HOME/.claude/skills" ]; then
            link_agent "../../.agents/skills/$name" "$claude_dir" "$name"
        else
            link_agent "$agents_dir/$name" "$claude_dir" "$name"
        fi
        link_agent "$agents_dir/$name" "$codex_dir" "$name"
        link_agent "$agents_dir/$name" "$agy_dir" "$name"
        printf 'Installed skill %s -> %s\n' "$name" "$agents_dir/$name"
    done

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
