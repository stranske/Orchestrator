#!/usr/bin/env bash
# Standalone reader witness; needs only Bash and standard-library Python.
# Run from any directory: bash tests/test_mirror_generation_imports.sh
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$repo"
exec python3 "$repo/tests/mirror_generation_imports_witness.py" "$repo"
