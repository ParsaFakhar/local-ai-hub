#!/bin/sh
# usage: sh "for LLM/fingerprint.sh" hub plugins tests
H=$(command -v sha256sum || echo "shasum -a 256")
find "$@" -type f -not -path '*/__pycache__/*' -not -path '*/dist/*' -not -name '.DS_Store' \
  | LC_ALL=C sort | while read -r f; do $H "$f"; done | $H | cut -c1-12
