#!/usr/bin/env bash
#
# Commissioning: the switch and the bulbs, into our own Matter fabric.
#
# TWO STRATEGIES. Pick one and stay with it:
#
# A. Stay on IKEA's Thread network. No resets, no physical access to the bulbs.
#    Add the switch in the IKEA app, then "share" each device and bring it into
#    our fabric as well (multi-admin). IKEA stays an admin.
#      ./scripts/commission.sh check 1001
#      ./scripts/commission.sh switch <share-code>
#      ./scripts/commission.sh bulb 1001 <share-code>
#
# B. Your own Thread network, independent of IKEA. Needs an OpenThread Border
#    Router of your own and one factory reset per bulb, once. After that you
#    depend on nobody and never touch the bulbs again.
#      ./scripts/commission.sh dataset <hex-from-otbr>
#      ./scripts/commission.sh switch-new <code>
#      ./scripts/commission.sh bulb-new 1001 <code>
#
# Common to both:
#      ./scripts/commission.sh sync              # ACL + binding from panel/devices.json
#      ./scripts/commission.sh bind 1001         # one bulb only (rewrites the table!)
#      ./scripts/commission.sh bind-all 1001 1002
#      ./scripts/commission.sh verify 1001
#      ./scripts/commission.sh open-window 1001  # invite another ecosystem, remotely
#      ./scripts/commission.sh backup            # DO THIS AFTER EVERY STEP
#
set -euo pipefail

OTA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../ota" && pwd)"
# shellcheck disable=SC1091
source "$OTA_DIR/config.sh"

CT=("$CHIP_TOOL" --storage-directory "$CHIP_TOOL_STORAGE")
CLUSTER_ONOFF=6
CLUSTER_LEVEL=8
CLUSTER_COLOR=768   # ColorControl 0x0300 - only for bulbs that actually have it
BULB_ENDPOINT=${BULB_ENDPOINT:-1}
SWITCH_ENDPOINT=${SWITCH_ENDPOINT:-1}

[ -x "$CHIP_TOOL" ] || { echo "chip-tool is missing - run ./ota/setup.sh"; exit 1; }
mkdir -p "$CHIP_TOOL_STORAGE"

usage() { sed -n '2,28p' "$0"; exit 1; }
[ $# -ge 1 ] || usage

# Stop the subcommands that assume there is exactly ONE switch.
#
# sync / bind / bind-all write every bulb's ACL with a single subject
# ($SWITCH_NODE), and the binding table on that one switch. With two switches in
# the house, the first run SILENTLY drops the other one from every bulb: it
# stays commissioned, it still has its own binding table, but it is no longer
# allowed to command anything. Nothing reports an error - the switch just stops
# doing anything.
#
# The panel computes the union over all switches correctly (panel/server.py,
# acl_for takes a list), so that is where we send you.
refuse_if_multiple_switches() {
	local n
	n=$(python3 -c "
import json
try:
    d = json.load(open('$DEV'))
    sw = d.get('switches') or ([d['switch']] if d.get('switch') else [])
    print(len(sw))
except Exception:
    print(0)
" 2>/dev/null)
	if [ "${n:-0}" -gt 1 ]; then
		cat <<EOF

STOPPED: devices.json lists $n switches, and this subcommand only knows about one.

It would rewrite every bulb's ACL with a single switch as the subject, which
drops the other one from all of them - without reporting any error. That switch
would stay commissioned and simply stop turning anything on.

Set up the bindings from the panel instead: the switch card -> "edit". That path
computes the union over all switches.
EOF
		exit 1
	fi
}

case "$1" in

dataset)
	# The Thread dataset of YOUR network, taken off the OTBR.
	# On the Raspberry Pi running OpenThread Border Router:
	#     sudo ot-ctl dataset active -x
	# Copy the hex string and run:
	#     ./scripts/commission.sh dataset <hex>
	#
	# This file matters as much as ota/state/ - without it you can no longer add
	# devices to the network. It belongs in the same backup.
	HEX=${2:?run on the OTBR: sudo ot-ctl dataset active -x, and pass the hex here}
	echo "$HEX" > "$OTA_DIR/state/thread-dataset.hex"
	chmod 600 "$OTA_DIR/state/thread-dataset.hex"
	echo "saved to ota/state/thread-dataset.hex"
	;;

switch-new)
	# The switch, straight into OUR Thread network, over BLE.
	#
	#   ./scripts/commission.sh switch-new [node-id]
	#
	# The switch has no printed code: it is our own device, with test
	# credentials compiled into the firmware. We commission it with the passcode
	# + discriminator from ota/config.sh, not with a payload.
	#
	# You have to be within BLE range of the machine running chip-tool.
	NODE=${2:-$SWITCH_NODE}
	DS=$(cat "$OTA_DIR/state/thread-dataset.hex")
	echo "=== Switch as node $NODE, on our Thread network ==="
	"${CT[@]}" pairing ble-thread "$NODE" hex:"$DS" \
		"$SWITCH_PASSCODE" "$SWITCH_DISCRIMINATOR"
	echo
	echo "Add it to panel/devices.json under 'switches', then bind its bulbs"
	echo "from the panel (the 'edit' button on its card)."
	;;

bulb-new)
	# A factory-reset bulb, straight into our network. Commissioning a
	# factory-new device goes over BLE, so you have to be within BLE range of
	# the bulb (same room) - but you do NOT have to touch it.
	NODE=${2:?node ID for the bulb}
	CODE=${3:?the code on the bulb or in its manual}
	DS=$(cat "$OTA_DIR/state/thread-dataset.hex")
	echo "=== Bulb as node $NODE, on our Thread network ==="
	"${CT[@]}" pairing code-thread "$NODE" hex:"$DS" "$CODE"
	;;

backup)
	# THE MOST IMPORTANT THING IN THE WHOLE PROJECT.
	#
	# ota/state/ holds our fabric's CA key and the node IDs. If you are the only
	# admin of the bulbs (strategy B) and you lose this directory, you can no
	# longer administer them AT ALL - you cannot remove your fabric from them
	# and you cannot open a commissioning window. The only way out would be a
	# physical factory reset on each one.
	DEST=${2:-$REPO_ROOT/fabric-backup-$(date +%Y%m%d-%H%M%S).tar.gz}
	tar czf "$DEST" -C "$OTA_DIR" state
	chmod 600 "$DEST"
	echo "backup: $DEST"
	echo
	echo "Keep it OFF this machine: USB stick, password manager, cloud."
	echo "Repeat after every new device you add."
	;;

open-window)
	# Opens the commissioning window on a device so you can add it to another
	# ecosystem (Apple Home, Home Assistant, even back into IKEA). Works
	# remotely, with no physical access - as long as you are an admin.
	#
	# This is why strategy B is not a dead end: you own the fabric, so you can
	# invite anyone, at any time.
	NODE=${2:?node ID}
	echo "=== Commissioning window on $NODE, 300 s ==="
	"${CT[@]}" pairing open-commissioning-window "$NODE" 1 300 1000 "$((RANDOM % 4096))"
	;;

check)
	# How many fabrics the bulb supports and how many are already taken.
	# The spec requires at least 5. If 4 of 5 are already taken, stop and work
	# out what is holding them - an orphaned entry can only be freed by a
	# factory reset (physical access).
	NODE=${2:?pass the node ID of a bulb already added to our fabric}
	echo "=== Fabric slots on bulb $NODE ==="
	"${CT[@]}" operationalcredentials read supported-fabrics "$NODE" 0
	"${CT[@]}" operationalcredentials read commissioned-fabrics "$NODE" 0
	"${CT[@]}" operationalcredentials read fabrics "$NODE" 0
	;;

switch)
	# PRECONDITION: the switch is already added in the IKEA app (over BLE), so
	# it is on the bulbs' Thread network. From the app, hit "share" and you get
	# an 11-digit pairing code.
	#
	# We use "pairing code" (not "code-thread"): the device is already on the
	# network, so we do not hand it a Thread dataset ourselves.
	CODE=${2:?the share code from the IKEA app}
	echo "=== Adding the switch as node $SWITCH_NODE to our fabric ==="
	"${CT[@]}" pairing code "$SWITCH_NODE" "$CODE"
	echo
	echo "Node ID $SWITCH_NODE has to be KEPT. The bulbs' ACLs reference it; if"
	echo "you recommission the switch under another ID, they must be rewritten."
	;;

bulb)
	# Same for a bulb: share from the IKEA app -> code -> here.
	# The bulb is NOT reset and NOT removed from IKEA.
	NODE=${2:?node ID to give the bulb, e.g. 1001}
	CODE=${3:?the share code from the IKEA app}
	echo "=== Adding the bulb as node $NODE to our fabric ==="
	"${CT[@]}" pairing code "$NODE" "$CODE"
	;;

sync)
	refuse_if_multiple_switches
	# Brings the network to the state described in panel/devices.json.
	#
	# This is the everyday command when you add a bulb: commission it, put it in
	# devices.json, run this. It writes the ACL on EVERY bulb and then the
	# ENTIRE binding table on the switch.
	#
	# Why you cannot write only the new bulb: writing the binding table replaces
	# its whole contents. A partial write would delete the bulbs already in it.
	DEV="$REPO_ROOT/panel/devices.json"
	[ -f "$DEV" ] || { echo "$DEV is missing"; exit 1; }

	NODES=$(python3 -c "
import json
d = json.load(open('$DEV'))
print(' '.join(str(b['node']) for b in d.get('bulbs', [])))
")
	[ -n "$NODES" ] || { echo "no bulbs in devices.json"; exit 1; }
	echo "=== bulbs from devices.json: $NODES ==="

	for n in $NODES; do
		echo "--- ACL on $n ---"
		"${CT[@]}" accesscontrol write acl "[
		  {\"fabricIndex\":1,\"privilege\":5,\"authMode\":2,\"subjects\":[$ADMIN_NODE],\"targets\":null},
		  {\"fabricIndex\":1,\"privilege\":4,\"authMode\":2,\"subjects\":[$SWITCH_NODE],\"targets\":null}
		]" "$n" 0
	done

	echo "--- binding table on the switch ---"
	# Build the table from devices.json, including ColorControl wherever the
	# bulb has that capability detected (the "caps" field).
	ENTRIES=$(python3 -c "
import json
d = json.load(open('$DEV'))
out = []
for b in d.get('bulbs', []):
    caps = b.get('caps') or {}
    cl = [$CLUSTER_ONOFF, $CLUSTER_LEVEL]
    if caps.get('ct') or caps.get('color'):
        cl.append($CLUSTER_COLOR)
    for c in cl:
        out.append({'fabricIndex': 1, 'node': b['node'],
                    'endpoint': b.get('endpoint', 1), 'cluster': c})
print(json.dumps(out))
")
	"${CT[@]}" binding write binding "$ENTRIES" "$SWITCH_NODE" "$SWITCH_ENDPOINT"

	echo
	echo "Done. Check in the panel that every binding shows up."
	echo "And take a backup: ./scripts/commission.sh backup"
	;;

bind)
	refuse_if_multiple_switches
	# ACL on the bulb + binding on the switch, for ONE bulb.
	# Prefer "sync" - this one rewrites the table and drops the other bulbs.
	NODE=${2:?bulb node ID}
	echo "=== ACL on bulb $NODE ==="
	# privilege 5 = Administer (chip-tool; do not remove it or you lose access)
	# privilege 4 = Manage     (the switch)
	#
	# Manage, not Operate: On/Off works under Operate too, but writing
	# StartUpOnOff / StartUpCurrentLevel requires Manage. Under Operate it fails
	# with UNSUPPORTED_ACCESS.
	"${CT[@]}" accesscontrol write acl "[
	  {\"fabricIndex\":1,\"privilege\":5,\"authMode\":2,\"subjects\":[$ADMIN_NODE],\"targets\":null},
	  {\"fabricIndex\":1,\"privilege\":4,\"authMode\":2,\"subjects\":[$SWITCH_NODE],\"targets\":null}
	]" "$NODE" 0

	echo "=== Binding on the switch, to bulb $NODE ==="
	# WARNING: the write replaces the entire table. For several bulbs, ALL
	# entries have to go out in a single command - otherwise you delete the
	# earlier ones. See 'bind-all' below.
	"${CT[@]}" binding write binding "[
	  {\"fabricIndex\":1,\"node\":$NODE,\"endpoint\":$BULB_ENDPOINT,\"cluster\":$CLUSTER_ONOFF},
	  {\"fabricIndex\":1,\"node\":$NODE,\"endpoint\":$BULB_ENDPOINT,\"cluster\":$CLUSTER_LEVEL}
	]" "$SWITCH_NODE" "$SWITCH_ENDPOINT"
	;;

bind-all)
	refuse_if_multiple_switches
	# Binding to several bulbs: ./commission.sh bind-all 1001 1002 1003
	# The ACL is written separately, per bulb, with 'bind'.
	shift
	[ $# -ge 1 ] || { echo "pass at least one bulb node ID"; exit 1; }
	ENTRIES=""
	for n in "$@"; do
		[ -n "$ENTRIES" ] && ENTRIES="$ENTRIES,"
		ENTRIES="$ENTRIES{\"fabricIndex\":1,\"node\":$n,\"endpoint\":$BULB_ENDPOINT,\"cluster\":$CLUSTER_ONOFF}"
		ENTRIES="$ENTRIES,{\"fabricIndex\":1,\"node\":$n,\"endpoint\":$BULB_ENDPOINT,\"cluster\":$CLUSTER_LEVEL}"
	done
	echo "=== Binding on the switch, to: $* ==="
	"${CT[@]}" binding write binding "[$ENTRIES]" "$SWITCH_NODE" "$SWITCH_ENDPOINT"
	;;

verify)
	NODE=${2:?bulb node ID}
	echo "=== Power-loss recovery state, read back from the bulb ==="
	# Run this at least a minute after the switch has come up - it writes these
	# roughly 45 s after boot.
	"${CT[@]}" onoff read start-up-on-off "$NODE" "$BULB_ENDPOINT"
	"${CT[@]}" levelcontrol read start-up-current-level "$NODE" "$BULB_ENDPOINT"
	echo
	echo "=== The switch's binding table ==="
	"${CT[@]}" binding read binding "$SWITCH_NODE" "$SWITCH_ENDPOINT"
	echo
	echo "Final test: press the button. Then unplug the DIRIGERA and press it"
	echo "again - it has to keep working, the command goes direct."
	;;

*) usage ;;
esac
