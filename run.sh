#!/bin/bash
# Convenience script to run trellm with the latest code
# Activates venv, installs in editable mode, and runs trellm

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

source .venv/bin/activate
pip install -q -e .
trellm "$@"
