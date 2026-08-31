#!/usr/bin/env bash
# The one command CI and a human both run: lint, then tests.
#
# `ruff check` is the gate; `ruff format --check` deliberately is not.
#
# There is no per-bank step. `scenariobank verify` was cut on 2026-08-31 along with the durable-bank
# premise it enforced: a bank is regenerated per batch, so there is nothing to check a bank against.
# What used to be its only non-reproducibility check -- that the map really is left-side drive --
# moved into `generate` and `doctor`, and `tests/unit/test_invariance.py` covers the option axes.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== ruff =="
uv run ruff check .

echo
echo "== pytest =="
uv run pytest
