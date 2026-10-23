#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PWD}/src${PYTHONPATH:+:${PYTHONPATH}}"
python3 -m pytest -q
python3 -m compileall -q src tests
