#!/usr/bin/env bash
# Build the Lambda deployment bundle into build/lambda.
#
# Only the core dependencies go in (httpx and friends, all pure Python, so the
# bundle runs on arm64 whatever machine builds it). boto3 is left out on
# purpose: the Lambda runtime ships it, and bundling it would triple the size.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
OUT="build/lambda"

rm -rf "${OUT:?}"
mkdir -p "${OUT:?}"
"$PYTHON" -m pip install --quiet --no-compile --target "${OUT:?}" .
find "${OUT:?}" -type d -name "__pycache__" -prune -exec rm -rf {} +
rm -rf "${OUT:?}/bin"

# Smoke test: the handlers import with nothing but the bundle on the path.
"$PYTHON" -I -S -c "import sys; sys.path.insert(0, '${OUT}'); import hopwatch.aws.lambdas" \
  || { echo "bundle does not import cleanly" >&2; exit 1; }

echo "Lambda bundle: ${OUT} ($(du -sh "${OUT}" | cut -f1))"
