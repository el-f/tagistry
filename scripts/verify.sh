#!/usr/bin/env bash
# The full gate: what CI runs, minus the per-OS matrix. Green before a push.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run mypy # files = src + tests (pyproject); the tests are strict-checked too
uv run pytest -q --block-network --cov=tagistry --cov-report=term-missing --cov-fail-under=85
echo "gate: green"
