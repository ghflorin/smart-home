#!/usr/bin/env bash
#
# Installs the systemd units on the machine that runs the house. Run it THERE.
#
#   sudo ./deploy/install-services.sh                    # user smarthome, interface guessed
#   SMARTHOME_USER=ghflorin sudo -E ./deploy/install-services.sh
#   INFRA_IF=eth0 sudo -E ./deploy/install-services.sh
#
# WHY THIS EXISTS. The units used to be copied by hand and then edited, because
# they carry a User= that is right on exactly one machine. Get it wrong and
# systemd fails with status=217/USER without naming the unit, and the service
# restart-loops. The account has been pi, then ghflorin, then smarthome; the
# unit files should not have had an opinion about any of them.
#
# BLUETOOTH is off unless you ask for it. It is needed to commission a
# FACTORY-NEW device - one that is not on Thread yet, so the only way to reach
# it is BLE. Everything already in the house is on Thread and needs none of it,
# and matter-server refuses to start if it is told to use an adapter that is
# not there:
#
#   SMARTHOME_BLE=1 sudo -E ./deploy/install-services.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME=${SMARTHOME_USER:-smarthome}
BLE=${SMARTHOME_BLE:-0}
RCP_DEV=${RCP_DEV:-/dev/ttyACM0}

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ok    %s\n' "$*"; }
die()  { printf '  ERROR %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run this with sudo"
id "$USER_NAME" >/dev/null 2>&1 || die "no such user: $USER_NAME (create it, or set SMARTHOME_USER)"

if [ -z "${INFRA_IF:-}" ]; then
	INFRA_IF=$(ip route show default 2>/dev/null | awk '{print $5; exit}')
fi
[ -n "$INFRA_IF" ] || die "cannot work out the interface toward the house; set INFRA_IF"
ip link show "$INFRA_IF" >/dev/null 2>&1 || die "no such interface: $INFRA_IF"

say "1. Units"
for unit in smarthome-matter smarthome-panel smarthome-thread-watchdog; do
	src="$HERE/$unit.service"
	[ -f "$src" ] || die "missing $src"
	sed -e "s/^User=.*/User=$USER_NAME/" \
	    -e "s/^Group=.*/Group=$USER_NAME/" \
	    -e "s|^ExecStartPre=/usr/bin/install -d -o [^ ]* -g [^ ]* /data|ExecStartPre=/usr/bin/install -d -o $USER_NAME -g $USER_NAME /data|" \
	    "$src" > "/etc/systemd/system/$unit.service"
	ok "$unit.service (User=$USER_NAME)"
done
install -m 644 "$HERE/smarthome-thread-watchdog.timer" /etc/systemd/system/
ok "smarthome-thread-watchdog.timer"

# The adapter argument is the whole of the BLE question: present and there is
# no adapter, matter-server dies at startup.
if [ "$BLE" != 1 ]; then
	# Just the one line. The line above it keeps its continuation and joins
	# straight to --log-level, which is still a valid command.
	sed -i '/--bluetooth-adapter 0/d' /etc/systemd/system/smarthome-matter.service
	ok "bluetooth off (SMARTHOME_BLE=1 turns it on)"
else
	ok "bluetooth on, adapter 0"
fi

say "2. The border router"
CONF=/etc/default/otbr-agent
if [ -f "$CONF" ]; then
	# -d 5 is NOTICE. At the default level otbr-agent logs every packet it
	# forwards - about 22,000 lines an hour in a house this size - and a
	# volatile journal then holds twenty minutes of history.
	sed -i "s|^OTBR_AGENT_OPTS=.*|OTBR_AGENT_OPTS=\"-I wpan0 -B $INFRA_IF -d 5 spinel+hdlc+uart://$RCP_DEV?uart-baudrate=1000000 trel://$INFRA_IF\"|" "$CONF"
	ok "$(grep OTBR_AGENT_OPTS "$CONF")"
else
	printf '  WARN  %s not there yet - run deploy/setup-otbr.sh first\n' "$CONF"
fi

say "3. Ownership"
install -d -o "$USER_NAME" -g "$USER_NAME" /data
chown -R "$USER_NAME:$USER_NAME" /opt/smarthome
ok "/opt/smarthome and /data belong to $USER_NAME"

systemctl daemon-reload
say "Done"
cat <<EOT

Nothing was started. When the radio is in place:

  sudo systemctl enable --now otbr-agent
  sudo systemctl enable --now smarthome-matter smarthome-panel
  sudo systemctl enable --now smarthome-thread-watchdog.timer

EOT
