#!/usr/bin/env bash
# trellm-stop-hook.sh — Claude Code `Stop` hook for trellm interactive mode.
#
# Registered as a `Stop` hook in an interactive project's
# `.claude/settings.json` (docs/claude-interactive.md §6.2). Claude Code
# fires `Stop` deterministically when the agent finishes a turn and pipes a
# JSON payload to this script's stdin:
#
#     { "session_id": "...", "cwd": "...", "transcript_path": "...", ... }
#
# The script derives the project name from `cwd` (its basename — window name
# == project name == working-dir basename, doc §6.1/§8) and appends one line
#
#     <session_id> <iso8601-utc>
#
# to ~/.trellm/interactive/<project>.signal. trellm's signal-file watcher
# (trellm/completion.py SignalWatcher) is awaiting exactly that append — the
# primary, deterministic completion trigger, with no TUI screen scraping.
#
# `transcript_path` is part of the payload but is not used here: trellm
# resolves the transcript itself from session_id + working_dir.
set -euo pipefail

# State directory. Overridable via $TRELLM_INTERACTIVE_DIR (used by tests and
# any non-default state-dir setup); defaults to the real interactive dir.
state_dir="${TRELLM_INTERACTIVE_DIR:-$HOME/.trellm/interactive}"

payload="$(cat)"

# Extract session_id + cwd from the hook JSON. python3 is always present
# wherever trellm runs (it is a Python project), so this avoids a hard `jq`
# dependency. A malformed payload prints nothing and the checks below fail.
mapfile -t fields < <(
  printf '%s' "$payload" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
print(d.get("session_id", "") or "")
print(d.get("cwd", "") or "")
'
)
session_id="${fields[0]:-}"
cwd="${fields[1]:-}"

if [[ -z "$session_id" || -z "$cwd" ]]; then
  echo "trellm-stop-hook: missing session_id or cwd in hook payload" >&2
  exit 1
fi

project="$(basename "$cwd")"
timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

mkdir -p "$state_dir"
printf '%s %s\n' "$session_id" "$timestamp" >> "$state_dir/$project.signal"
