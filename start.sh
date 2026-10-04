#!/usr/bin/env sh
# Start the arbitrage scanner (Linux/macOS). First run creates .venv and installs the dependencies.
# Extra arguments are passed through, e.g.:  ./start.sh probe
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    echo "Creating virtual environment in .venv ..."
    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -e .
fi
exec .venv/bin/python -m odds_scanner "$@"
