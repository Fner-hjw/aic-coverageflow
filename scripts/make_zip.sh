#!/usr/bin/env bash
# Produce the submission ZIP including the complete hidden .git directory.
# Output: <repo parent>/25210660138-final-project.zip (next to the repo folder).
set -euo pipefail
cd "$(dirname "$0")/.."   # repo root
out="$(cd .. && pwd)/25210660138-final-project.zip"
rm -f "$out"
parent="$(dirname "$out")"
name="$(basename "$out")"
(cd "$parent" && zip -qr "$name" coverageflow \
    -x "coverageflow/.venv/*" "coverageflow/*/__pycache__/*" "coverageflow/.pytest_cache/*")
echo "wrote $out"
