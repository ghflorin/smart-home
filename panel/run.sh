#!/usr/bin/env bash
#
# Starts the admin panel.
#
#   ./panel/run.sh
#
# The panel needs matter-server on the other end - that is where it does all its
# Matter work. On the Pi that is a systemd unit
# (deploy/smarthome-matter.service) and this script only starts the panel; set
# MATTER_WS if it lives somewhere else.
#
# It used to start chip-tool as well, on a second port, and redirect its stdout
# to a log because the pairing codes were only ever printed there. None of that
# is needed now.

set -euo pipefail

PANEL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PANEL_PORT=${PANEL_PORT:-8080}
MATTER_WS=${MATTER_WS:-ws://127.0.0.1:5580/ws}

for mod in websockets segno; do
	python3 -c "import $mod" 2>/dev/null || {
		echo "the python package '$mod' is missing:"
		echo "  pip install websockets segno"
		exit 1
	}
done

# A clear message beats a page full of failed reads. The panel survives
# matter-server being down - it retries with backoff - but if it is not there at
# all you want to know now.
python3 - "$MATTER_WS" <<'PY' || {
import sys
from websockets.sync.client import connect
try:
    with connect(sys.argv[1], open_timeout=5) as ws:
        ws.recv(timeout=10)
except Exception as exc:
    print(f"cannot reach matter-server at {sys.argv[1]}: {exc}")
    raise SystemExit(1)
PY
	echo
	echo "Start it first:  sudo systemctl start smarthome-matter"
	echo "or point the panel elsewhere with MATTER_WS=..."
	exit 1
}

echo
echo "=== Panel: http://$(hostname -s).local:$PANEL_PORT ==="
echo
MATTER_WS="$MATTER_WS" PANEL_PORT="$PANEL_PORT" python3 "$PANEL_DIR/server.py"
