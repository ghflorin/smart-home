#!/usr/bin/env bash
#
# Starts the admin panel.
#
#   ./panel/run.sh
#
# Starts two processes:
#   1. chip-tool in "interactive server" mode -> WebSocket on 9002
#   2. the panel server                       -> HTTP on 8080
#
# chip-tool uses the SAME ota/state/ as the rest of the scripts, so it sees the
# same devices and the same fabric. It does not create another one.

set -euo pipefail

PANEL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$PANEL_DIR/../ota/config.sh"

PANEL_PORT=${PANEL_PORT:-8080}
WS_PORT=${WS_PORT:-9002}

[ -x "$CHIP_TOOL" ] || { echo "chip-tool is missing - run ./ota/setup.sh"; exit 1; }
for mod in websockets segno; do
	python3 -c "import $mod" 2>/dev/null || {
		echo "the python package '$mod' is missing:"
		echo "  pip install websockets segno"
		exit 1
	}
done

# We redirect chip-tool's stdout into a log: the pairing codes (manual + QR)
# are written there, not into the JSON response on the WebSocket.
CHIP_LOG="$CHIP_TOOL_STORAGE/chip-tool.log"
: > "$CHIP_LOG"

echo "=== chip-tool interactive server, port $WS_PORT ==="
echo "    log: $CHIP_LOG"
"$CHIP_TOOL" interactive server --port "$WS_PORT" \
	--storage-directory "$CHIP_TOOL_STORAGE" > "$CHIP_LOG" 2>&1 &
CHIP_PID=$!
trap 'echo; echo "stopping chip-tool ($CHIP_PID)"; kill $CHIP_PID 2>/dev/null || true' EXIT
sleep 2
kill -0 $CHIP_PID 2>/dev/null || { echo "chip-tool died on startup"; exit 1; }

echo
echo "=== Panel: http://$(hostname -s).local:$PANEL_PORT ==="
echo
CHIP_TOOL_WS="ws://127.0.0.1:$WS_PORT" PANEL_PORT="$PANEL_PORT" \
	CHIP_TOOL_LOG="$CHIP_LOG" \
	python3 "$PANEL_DIR/server.py"
