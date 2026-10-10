#!/usr/bin/env bash
# ==============================================================================
# Roostoo Sentinel - AWS EC2 / TMUX Bot Launch Script
# ==============================================================================
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"

echo "============================================================"
echo "  Roostoo Sentinel - Competition Bot Launcher"
echo "============================================================"

# 1. Load .env if present
if [ -f "$DIR/.env" ]; then
    echo "[*] Loading environment variables from .env..."
    export $(grep -v '^#' "$DIR/.env" | xargs)
fi

# 2. Check for required environment variables
if [ -z "$ROOSTOO_API_KEY" ] || [ -z "$ROOSTOO_SECRET_KEY" ]; then
    echo "[!] WARNING: ROOSTOO_API_KEY or ROOSTOO_SECRET_KEY is not set!"
    echo "    Set them in your environment or in a .env file:"
    echo "    export ROOSTOO_API_KEY=\"your_api_key\""
    echo "    export ROOSTOO_SECRET_KEY=\"your_secret_key\""
    echo ""
fi

# 3. Detect Python virtual environment
PYTHON="python3"
if [ -d "$DIR/.venv" ]; then
    PYTHON="$DIR/.venv/bin/python"
elif [ -n "$VIRTUAL_ENV" ]; then
    PYTHON="$VIRTUAL_ENV/bin/python"
fi

echo "[*] Using Python: $($PYTHON --version)"

# 4. Mode Selection
STRATEGY="${1:-tick}"
MODE="${2:-paper-exchange}"

echo "[*] Strategy: $STRATEGY"
echo "[*] Mode:     $MODE"
echo "============================================================"

# 5. Preflight verification
echo "[*] Running authenticated preflight check..."
$PYTHON -m sentinel preflight --authenticated || {
    echo "[!] Authenticated preflight failed. Check API credentials and network."
}

# 6. Launch Bot
echo ""
echo "[*] Starting bot process in mode: $MODE..."
echo "[*] Remember to run inside TMUX (Ctrl+B, then D to detach):"
echo "------------------------------------------------------------"

if [ "$STRATEGY" = "tick" ]; then
    exec $PYTHON -m sentinel tick-run --mode "$MODE"
else
    exec $PYTHON -m sentinel run --mode "$MODE"
fi
