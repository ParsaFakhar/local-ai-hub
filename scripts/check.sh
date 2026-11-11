#!/usr/bin/env bash
# Run every check without needing `make`.
#   ./scripts/check.sh          # lint + unit tests + extension tests + smoke test
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${LOCAL_AI_HUB_PYTHON:-python3}"

echo "== Python syntax =="
"$PYTHON" -m compileall -q hub native scripts plugins sdk tests >/dev/null && echo "  ok"

echo "== JavaScript syntax =="
if command -v node >/dev/null 2>&1; then
  while IFS= read -r file; do node --check "$file"; done < <(find extension sdk/js plugins templates tests -name '*.js')
  echo "  ok"
else
  echo "  skipped (node not installed)"
fi

echo "== JSON validity =="
while IFS= read -r file; do
  "$PYTHON" -c 'import json,sys; json.load(open(sys.argv[1]))' "$file"
done < <(find . -name '*.json' -not -path './dist/*' -not -path './.git/*')
echo "  ok"

echo "== Unit tests =="
"$PYTHON" -m unittest discover -s tests 2>&1 | tail -4

if command -v node >/dev/null 2>&1; then
  echo "== Extension logic tests =="
  node tests/test_extension.js | tail -2
fi

echo "== End-to-end smoke test =="
"$PYTHON" scripts/smoke_test.py --quiet

echo
echo "All checks passed."
