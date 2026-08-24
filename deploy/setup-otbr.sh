#!/usr/bin/env bash
#
# Installs OpenThread Border Router on the Raspberry Pi. Run it ON the Pi.
#
#   ./deploy/setup-otbr.sh                 # guess the interface and the RCP port
#   ./deploy/setup-otbr.sh eth0            # force the interface
#   RCP_DEV=/dev/ttyACM1 ./deploy/setup-otbr.sh eth0
#
# This is the piece missing before a Thread network exists at all. Without it
# chip-tool runs and the panel starts, but there is no radio and nothing answers
# - which is exactly what the panel is reporting when it says no device responds.
#
# Requires the RCP radio already flashed and plugged in over USB:
#     ./scripts/build-rcp.sh particle_xenon
#     ./scripts/flash.sh xenon
#
# Takes a long time (it compiles otbr-agent from source). Run it in tmux.
set -euo pipefail

SRC_DIR=${SRC_DIR:-$HOME/ot-br-posix}
INFRA_IF=${1:-}

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ok    %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; }
die()  { printf '  ERROR %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Linux" ] || die "this runs on the Raspberry Pi, not on the Mac"

say "1. The radio"

# The RCP shows up as a USB CDC device. If there are several, ask instead of
# guessing - guessing wrong configures the border router on some other device's
# port, and the failure only surfaces much later, at the first commissioning.
if [ -z "${RCP_DEV:-}" ]; then
	mapfile -t CANDIDATES < <(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null || true)
	case ${#CANDIDATES[@]} in
	0) die "no /dev/ttyACM* - the RCP radio is not plugged in, or not flashed" ;;
	1) RCP_DEV=${CANDIDATES[0]} ;;
	*) die "several serial ports (${CANDIDATES[*]}). Pick one: RCP_DEV=... $0 $*" ;;
	esac
fi
[ -e "$RCP_DEV" ] || die "$RCP_DEV does not exist"
ok "radio on $RCP_DEV"

say "2. The interface toward the house"

# INFRA_IF_NAME is the interface the border router talks to the rest of the
# house on: it sends the IPv6 announcements there, and that is the path Apple
# Home takes to the bulbs. Get it wrong and the Thread network forms but stays
# isolated.
if [ -z "$INFRA_IF" ]; then
	INFRA_IF=$(ip route show default 2>/dev/null | awk '{print $5; exit}')
fi
[ -n "$INFRA_IF" ] || die "cannot work out the interface; pass it as an argument"
ip link show "$INFRA_IF" >/dev/null 2>&1 || die "interface $INFRA_IF does not exist"

if [ -d "/sys/class/net/$INFRA_IF/wireless" ]; then
	warn "$INFRA_IF is Wi-Fi."
	warn "It works, but the border router leans on multicast, and plenty of"
	warn "access points filter or delay multicast over wireless."
	warn "On a cable there is far less to debug. Continuing anyway in 10 s;"
	warn "Ctrl-C if you would rather move the Pi to Ethernet first."
	sleep 10
else
	ok "$INFRA_IF is wired"
fi

SYSCTL=$(command -v sysctl || echo /usr/sbin/sysctl)
[ "$("$SYSCTL" -n net.ipv6.conf.all.forwarding 2>/dev/null)" = "1" ] ||
	die "IPv6 forwarding disabled - run ./deploy/prepare-pi.sh first"
ok "IPv6 forwarding enabled"

say "3. Sources"

if [ -d "$SRC_DIR/.git" ]; then
	ok "$SRC_DIR already exists"
else
	git clone --depth 1 https://github.com/openthread/ot-br-posix "$SRC_DIR"
	ok "cloned into $SRC_DIR"
fi
cd "$SRC_DIR"
git submodule update --init --depth 1

say "4. Install"

# NetworkManager, on Raspberry Pi OS from Bookworm onward. The OTBR setup
# behaves differently depending on this flag: with the wrong value it works
# against the network manager and the Thread interface never gets an address.
NM=0
systemctl is-active --quiet NetworkManager && NM=1
ok "NetworkManager: $([ "$NM" = 1 ] && echo active || echo inactive)"

# Cap the parallel compiles by how much memory there actually is.
#
# ot-br-posix calls `ninja` without -j, and ninja defaults to nproc+2 processes.
# On a Pi 3 B+ (4 cores, 955 MB) that is 6 cc1plus instances at once, and the
# OOM killer takes them out: "c++: fatal error: Killed signal terminated program
# cc1plus", after about half an hour of compiling.
#
# Their scripts have no variable for this, so we put a `ninja` of our own
# earlier on PATH that adds -j. Since ninja honors the last -j it is given,
# this still works if one of their own invocations already carries one.
NINJA_REAL=$(command -v ninja || echo /usr/bin/ninja)
MEM_MB=$(awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo)
if   [ "$MEM_MB" -lt 2048 ]; then JOBS=2
elif [ "$MEM_MB" -lt 4096 ]; then JOBS=$(( $(nproc) / 2 ))
else                              JOBS=$(nproc)
fi
[ "$JOBS" -ge 1 ] || JOBS=1
WRAP=$(mktemp -d)
printf '#!/bin/sh\nexec %s -j%s "$@"\n' "$NINJA_REAL" "$JOBS" > "$WRAP/ninja"
chmod +x "$WRAP/ninja"
export PATH="$WRAP:$PATH"
ok "$MEM_MB MB RAM -> compiling with $JOBS processes in parallel"

./script/bootstrap

INFRA_IF_NAME="$INFRA_IF" NETWORK_MANAGER="$NM" RELEASE=1 ./script/setup

say "5. The radio port"

# script/setup writes a default port that is usually not ours.
CONF=/etc/default/otbr-agent
[ -f "$CONF" ] || die "$CONF was not created - setup failed"
sudo sed -i \
	"s|^OTBR_AGENT_OPTS=.*|OTBR_AGENT_OPTS=\"-I wpan0 -B $INFRA_IF spinel+hdlc+uart://$RCP_DEV?uart-baudrate=1000000 trel://$INFRA_IF\"|" \
	"$CONF"
ok "$(grep OTBR_AGENT_OPTS "$CONF")"

sudo systemctl daemon-reload
sudo systemctl restart otbr-agent
sleep 5

say "6. Verification"

systemctl is-active --quiet otbr-agent || {
	sudo journalctl -u otbr-agent -n 30 --no-pager
	die "otbr-agent will not start - the log is above"
}
ok "otbr-agent is running"

if sudo ot-ctl state >/dev/null 2>&1; then
	ok "ot-ctl answers: $(sudo ot-ctl state | head -1)"
else
	die "ot-ctl does not answer - usually the wrong RCP port, or an unflashed radio"
fi

cat <<EOF

Done. Next, once, to form the network:

  sudo ot-ctl dataset init new
  sudo ot-ctl dataset commit active
  sudo ot-ctl ifconfig up
  sudo ot-ctl thread start
  sudo ot-ctl state            # after ~10 s it has to say "leader"

Then save the dataset - without it you can no longer add devices:

  sudo ot-ctl dataset active -x
  ./scripts/commission.sh dataset <the hex from above>

EOF
