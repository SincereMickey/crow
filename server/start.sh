#!/bin/bash
# Start Crow server + evo daemon + cloudflared tunnel
# Usage: ./start.sh  (optionally: ./start.sh --no-evo to skip the daemon)
set -e
cd "$(dirname "$0")"

if [ -z "$CROW_PASSWORD" ]; then
    echo "Error: CROW_PASSWORD is not set. Export it before running:" >&2
    echo "  export CROW_PASSWORD='yourpassword'" >&2
    exit 1
fi

python3 server.py &
FLASK_PID=$!
echo "Server started       (PID $FLASK_PID)  http://localhost:7429"

EVO_PID=""
if [ "$1" != "--no-evo" ]; then
    sleep 2
    python3 evolve.py &
    EVO_PID=$!
    echo "Evo daemon started   (PID $EVO_PID)"
else
    echo "Evo daemon skipped   (--no-evo flag)"
fi

echo "Remote: https://crow.sinceremickey.com"
echo ""
echo "Press Ctrl+C to stop all."

cleanup() {
    echo "Stopping..."
    kill "$FLASK_PID" 2>/dev/null || true
    [ -n "$EVO_PID" ] && kill "$EVO_PID" 2>/dev/null || true
    echo "Stopped."
}
trap cleanup EXIT INT TERM

cloudflared tunnel --config cloudflared.yml run
