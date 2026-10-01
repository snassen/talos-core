#!/bin/zsh
# Quick checks for a Talos change: the UI script parses, the guards and web tests pass, plus any
# tests named on the command line. --full runs the whole suite instead (about 4.5 minutes).
set -e
cd "$HOME/Github repos/talos"
node --check src/talos/web/static/app.js
echo "app.js parses"
if [[ "$1" == "--full" ]]; then
  shift
  uv run pytest -q "$@"
else
  uv run pytest -q tests/test_guards.py tests/test_web.py "$@"
fi
