#!/usr/bin/env bash
# Local AI Hub — uninstaller.
#   ./uninstall.sh           # unregister the native host, keep your config
#   ./uninstall.sh --purge   # also delete state, logs and user config
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HOST_NAME="local_ai_hub"
PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1

if [[ "$(uname -s)" == "Darwin" ]]; then
  DATA_DIR="$HOME/Library/Application Support/LocalAIHub"
  MANIFEST="$HOME/Library/Application Support/Mozilla/NativeMessagingHosts/$HOST_NAME.json"
else
  DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/local-ai-hub"
  MANIFEST="$HOME/.mozilla/native-messaging-hosts/$HOST_NAME.json"
fi

if [[ -f "$MANIFEST" ]]; then
  rm -f "$MANIFEST"
  echo "removed $MANIFEST"
else
  echo "no native manifest at $MANIFEST"
fi

if [[ "$PURGE" == "1" ]]; then
  rm -rf "$DATA_DIR"
  echo "removed $DATA_DIR"
else
  echo "kept state/config at $DATA_DIR (use --purge to delete)"
fi

cat <<'NOTE'
The Firefox extension itself is removed from about:debugging (temporary add-ons
disappear on restart) or from about:addons for a signed installation.
Ollama and your downloaded models are untouched.
NOTE
