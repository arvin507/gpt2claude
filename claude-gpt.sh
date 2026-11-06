#!/usr/bin/env bash
# Start Claude Code with the generated proxy settings. Global Claude settings are untouched.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$root/start.sh"

claude_bin="$(command -v claude || true)"
if [ -z "$claude_bin" ] && [ -x "$HOME/.local/bin/claude" ]; then
  claude_bin="$HOME/.local/bin/claude"
fi
if [ -z "$claude_bin" ]; then
  echo "Claude Code executable not found. Install it, or add it to PATH." >&2
  exit 1
fi

exec "$claude_bin" --settings "$root/claude-settings.json" "$@"
