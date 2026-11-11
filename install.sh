#!/usr/bin/env bash
# Local AI Hub — macOS installer
#
#   ./install.sh                 # register native host, check dependencies
#   ./install.sh --model qwen3:4b
#   ./install.sh --mock          # offline test mode (no Ollama needed)
#   ./install.sh --api           # also enable the experimental local HTTP API
#   ./install.sh --no-test
#
# Nothing is downloaded, no sudo, nothing outside your home directory.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HOST_NAME="local_ai_hub"
EXTENSION_ID="local-ai-hub@localaihub.dev"
MODEL=""
MOCK=0
API=0
RUN_TEST=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL="${2:-}"; shift 2 ;;
    --mock) MOCK=1; shift ;;
    --api) API=1; shift ;;
    --no-test) RUN_TEST=0; shift ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

say "Local AI Hub installer"
echo "  project: $ROOT"

# ---------------------------------------------------------------- dependencies
command -v python3 >/dev/null 2>&1 || die "python3 not found (install Xcode command line tools or Homebrew python)"
PY_VERSION=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' || die "Python 3.9+ required, found $PY_VERSION"
ok "python3 $PY_VERSION"

PYTHON_BIN="$(command -v python3)"

if command -v node >/dev/null 2>&1; then
  ok "node $(node --version) (JavaScript plugins enabled)"
else
  warn "node not found — JavaScript plugins will be unavailable (brew install node)"
fi

OLLAMA_BIN=""
for candidate in "$(command -v ollama 2>/dev/null || true)" /opt/homebrew/bin/ollama /usr/local/bin/ollama /Applications/Ollama.app/Contents/Resources/ollama; do
  if [[ -n "$candidate" && -x "$candidate" ]]; then OLLAMA_BIN="$candidate"; break; fi
done
if [[ -n "$OLLAMA_BIN" ]]; then
  ok "ollama at $OLLAMA_BIN"
else
  warn "ollama not found — install from https://ollama.com/download (or use --mock for offline tests)"
fi

# ---------------------------------------------------------------- state dirs
if [[ "$(uname -s)" == "Darwin" ]]; then
  DATA_DIR="$HOME/Library/Application Support/LocalAIHub"
  MANIFEST_DIR="$HOME/Library/Application Support/Mozilla/NativeMessagingHosts"
else
  DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/local-ai-hub"
  MANIFEST_DIR="$HOME/.mozilla/native-messaging-hosts"
fi
mkdir -p "$DATA_DIR/logs" "$DATA_DIR/config/domains" "$MANIFEST_DIR"
ok "state directory: $DATA_DIR"

# ---------------------------------------------------------------- user config
USER_CONFIG="$DATA_DIR/config/hub.json"
python3 - "$USER_CONFIG" "$MODEL" "$MOCK" "$API" "$PYTHON_BIN" <<'PY'
import json, sys
from pathlib import Path

path, model, mock, api, python_bin = sys.argv[1:6]
config = {}
if Path(path).is_file():
    try:
        config = json.loads(Path(path).read_text())
    except json.JSONDecodeError:
        config = {}
config.setdefault("runtime", {})["python"] = python_bin
ollama = config.setdefault("ollama", {})
if model:
    ollama["default_model"] = model
    ollama["vision_model"] = model
if mock == "1":
    ollama["mock"] = True
if api == "1":
    config.setdefault("api", {})["enabled"] = True
Path(path).parent.mkdir(parents=True, exist_ok=True)
Path(path).write_text(json.dumps(config, indent=2) + "\n")
print(f"  user config: {path}")
PY

if [[ "$API" == "1" && ! -f "$DATA_DIR/api-token" ]]; then
  python3 -c "import secrets,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(secrets.token_urlsafe(32))" "$DATA_DIR/api-token"
  chmod 600 "$DATA_DIR/api-token"
  ok "API token written to $DATA_DIR/api-token"
fi

# ---------------------------------------------------------------- native manifest
chmod +x "$ROOT/bin/local-ai-hub-host" "$ROOT/bin/laihub" "$ROOT/native/local_ai_hub_host.py" 2>/dev/null || true
find "$ROOT/plugins" -name 'plugin.py' -o -name 'plugin.js' | while read -r entry; do chmod +x "$entry" 2>/dev/null || true; done

MANIFEST="$MANIFEST_DIR/$HOST_NAME.json"
cat > "$MANIFEST" <<JSON
{
  "name": "$HOST_NAME",
  "description": "Local AI Hub native messaging host",
  "path": "$ROOT/bin/local-ai-hub-host",
  "type": "stdio",
  "allowed_extensions": ["$EXTENSION_ID"]
}
JSON
ok "native manifest: $MANIFEST"

# ---------------------------------------------------------------- model hint
if [[ -n "$OLLAMA_BIN" && "$MOCK" == "0" ]]; then
  EFFECTIVE_MODEL="${MODEL:-$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('ollama',{}).get('default_model','qwen3.5:4b'))" "$ROOT/config/hub.json")}"
  if "$OLLAMA_BIN" list 2>/dev/null | awk 'NR>1{print $1}' | grep -qx "$EFFECTIVE_MODEL"; then
    ok "model $EFFECTIVE_MODEL is installed"
  else
    warn "model $EFFECTIVE_MODEL is not installed yet — run: ollama pull $EFFECTIVE_MODEL"
  fi
fi

# ---------------------------------------------------------------- smoke test
if [[ "$RUN_TEST" == "1" ]]; then
  say "Running smoke test (offline mock model)"
  if LOCAL_AI_HUB_MOCK_LLM=1 python3 "$ROOT/scripts/smoke_test.py" --quiet; then
    ok "smoke test passed"
  else
    warn "smoke test failed — run: python3 scripts/smoke_test.py"
  fi
fi

say "Done. Next steps"
cat <<STEPS
  1. Open Firefox Nightly → about:debugging#/runtime/this-firefox
  2. "Load Temporary Add-on…" → select:
       $ROOT/extension/manifest.json
  3. Click the Local AI Hub toolbar button — it should say "connected".
  4. Configure a site in the extension's Settings → Domains,
     or edit $ROOT/config/domains/<domain>.json
  5. CLI check:  $ROOT/bin/laihub status

  Logs:   $DATA_DIR/logs/hub.log
  Remove: $ROOT/uninstall.sh
STEPS
