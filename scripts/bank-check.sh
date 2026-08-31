#!/usr/bin/env bash
# The one command CI and a human both run: lint, then tests, then every bank on disk.
#
# `ruff check` is the gate; `ruff format --check` deliberately is not.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== ruff =="
uv run ruff check .

echo
echo "== pytest =="
uv run pytest

echo
echo "== banks =="
shopt -s nullglob
banks=(banks/*/)
if [ ${#banks[@]} -eq 0 ]; then
  # Not a pass. Phase 2 is what puts a bank here; until then say so out loud rather than
  # letting an empty loop read as a green check.
  echo "no banks in banks/ -- nothing verified (expected until Phase 2)"
else
  for bank in "${banks[@]}"; do
    echo "-- ${bank}"
    uv run scenariobank verify --bank "${bank}"
  done
fi
