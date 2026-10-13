set shell := ["bash", "-cu"]
# Keep the venv and bytecode out of the (iCloud-synced) checkout.
export UV_PROJECT_ENVIRONMENT := env_var("HOME") + "/.cache/uv-venvs/agent-dispatch"
export PYTHONPYCACHEPREFIX := env_var("HOME") + "/.cache/pycache/agent-dispatch"

default:
    @just --list

# The long-poll daemon with the caller's environment (AGENT_DISPATCH_* variables)
dev:
    uv run agent-dispatch watch

# One reconciliation pass over Requested tasks, no polling
run:
    uv run agent-dispatch once

test:
    uv run pytest

# All static analysis (read-only, CI-safe)
check:
    uv run ruff check . && uv run ruff format --check . && nix flake check

fmt:
    uv run ruff format . && uv run ruff check --fix .

# Tail the installed agent's log (on the always-on Mac)
logs:
    tail -F "${XDG_STATE_HOME:-$HOME/.local/state}/agent-dispatch/agent-dispatch.log"

# --- project-specific recipes below (one-offs live in scripts/, run directly) ---
