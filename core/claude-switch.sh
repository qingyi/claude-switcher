#!/bin/bash
# Stable entry point for the GUI and existing CLI integrations.
set -eu
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export LC_ALL=en_US.UTF-8
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$HERE/claude-switch.py" ]; then
    exec python3 "$HERE/claude-switch.py" "$@"
fi
exec python3 "$HOME/.claude-switcher/claude-switch.py" "$@"
