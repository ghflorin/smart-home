#!/usr/bin/env bash
#
# Watches the Thread radio and puts it back when it goes.
#
# WHY THIS EXISTS. otbr-agent ships as an LSB init script, and the unit systemd
# generates from it says:
#
#     Restart=no
#     GuessMainPID=no
#     RemainAfterExit=yes
#
# systemd therefore has no idea whether the daemon is alive. When the radio
# co-processor stopped answering, otbr-agent died and the unit went on
# reporting `active (exited)` - from nine days earlier - while every Matter
# node in the house failed. Nothing was going to notice, because nothing was
# watching.
#
# WHAT ACTUALLY BREAKS. Two different faults, and only one of them is fixed by
# restarting the service:
#
#   the daemon died          -> restarting otbr-agent is enough.
#   the RCP wedged           -> it is not. The dongle still enumerates on USB
#                               and still has a /dev/ttyACM node, but will not
#                               speak spinel, and every restart ends at
#                               `Init() at spinel_driver.cpp:87: Failure`.
#                               Only power-cycling the device clears it, which
#                               over USB means unbind then bind.
#
# So this escalates rather than retrying: restart first, and reset the radio
# only if a restart was not enough. A USB reset is the bigger hammer and it is
# not free - the network drops and every node re-attaches - so it is not the
# first thing tried.
#
# WHICH USB DEVICE. Resolved from the port otbr is actually configured to use
# (the RadioURL in /etc/default/otbr-agent), never by hunting for a vendor id:
# the switches in this project are Nordic parts too, and one plugged in for
# flashing would match 1915:0000 just as well as the radio does. The resolved
# id is cached, because a dongle that has fallen off the bus has no tty left to
# resolve it from - which is exactly when it needs resetting.
#
# Run by smarthome-thread-watchdog.timer, once a minute. Safe to run by hand:
#
#     sudo ./thread-watchdog.sh            # check, and act if needed
#     sudo ./thread-watchdog.sh --check    # report only, change nothing
#
# To stop it acting while you work on the radio:
#
#     sudo touch /run/smarthome-watchdog.pause     # cleared by a reboot
#
# Deliberately not `set -e`: a failing health check is the normal case here and
# must not abort the script that exists to handle it.
set -uo pipefail

OT_CTL=${OT_CTL:-/usr/sbin/ot-ctl}
OTBR_DEFAULTS=${OTBR_DEFAULTS:-/etc/default/otbr-agent}
STATE=${STATE:-/opt/smarthome/ota/state/thread-watchdog.state}
PAUSE=${PAUSE:-/run/smarthome-watchdog.pause}

# How many consecutive bad checks before acting. At a 60 s timer that is two
# minutes of genuinely down, which no transient survives - and an attach after
# a reset takes longer than one check, so acting sooner would just interrupt
# the recovery already under way.
THRESHOLD=${THRESHOLD:-2}
# Quiet period after acting, so the network is given time to come back before
# being judged again.
COOLDOWN=${COOLDOWN:-150}

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

say() {
	logger -t smarthome-thread-watchdog -- "$*" 2>/dev/null
	printf '%s\n' "$*"
}

# ---------------------------------------------------------------- state file
fails=0 stage=0 last_action=0 usb=""
# shellcheck disable=SC1090
[ -r "$STATE" ] && . "$STATE"

save_state() {
	mkdir -p "$(dirname "$STATE")"
	cat >"$STATE" <<-EOF
		fails=$fails
		stage=$stage
		last_action=$last_action
		usb="$usb"
	EOF
}

now() { date +%s; }

# ------------------------------------------------------------------- health
#
# Three answers, not two. "Attaching" is not "broken": a border router that has
# just been reset spends a while detached on its way to a partition, and
# treating that as a fault would have the watchdog resetting the radio over and
# over while it was busy recovering from the last reset.
health() {
	local out role
	out=$(timeout 10 "$OT_CTL" state 2>&1)
	role=$(printf '%s\n' "$out" | head -1 | tr -d '\r')
	case "$role" in
	leader | router | child) echo up ;;
	detached | disabled) echo attaching ;;
	*) echo down ;;
	esac
}

# ------------------------------------------------------- which USB device
#
# /etc/default/otbr-agent holds the RadioURL:
#   OTBR_AGENT_OPTS="-I wpan0 -B eth0 spinel+hdlc+uart:///dev/ttyACM0?..."
radio_tty() {
	sed -n 's|.*uart://\([^?" ]*\).*|\1|p' "$OTBR_DEFAULTS" 2>/dev/null | head -1
}

# ttyACM0 -> 1-1.1.3. The tty's `device` link points at the USB INTERFACE; the
# thing that can be unbound is its parent, the device.
usb_id_from_tty() {
	local tty path
	tty=$(basename "$(radio_tty)")
	[ -n "$tty" ] || return 1
	path=$(readlink -f "/sys/class/tty/$tty/device" 2>/dev/null) || return 1
	[ -n "$path" ] || return 1
	basename "$(dirname "$path")"
}

reset_radio() {
	local id
	id=$(usb_id_from_tty) || id=""
	# Nothing to resolve it from: the dongle has fallen off the bus entirely.
	# That is precisely when it needs the reset, so fall back to the id cached
	# while it was last healthy.
	[ -z "$id" ] && id="$usb"
	if [ -z "$id" ]; then
		say "radio is wedged and I cannot tell which USB device it is - no tty at $(radio_tty) and nothing cached. Re-plug it by hand."
		return 1
	fi
	if [ ! -d "/sys/bus/usb/devices/$id" ]; then
		say "cached USB id $id is not on the bus any more. Re-plug the radio by hand."
		return 1
	fi
	say "power-cycling the radio on USB ($id)"
	systemctl stop otbr-agent
	echo "$id" >/sys/bus/usb/drivers/usb/unbind 2>/dev/null
	sleep 4
	echo "$id" >/sys/bus/usb/drivers/usb/bind 2>/dev/null
	sleep 8
	systemctl start otbr-agent
	return 0
}

# --------------------------------------------------------------------- main
state=$(health)

if [ "$CHECK_ONLY" = 1 ]; then
	say "thread: $state (consecutive failures: $fails, stage: $stage, usb: ${usb:-unresolved})"
	exit 0
fi

if [ -e "$PAUSE" ]; then
	[ "$state" != up ] && say "thread is $state, but $PAUSE exists - leaving it alone"
	exit 0
fi

if [ "$state" = up ]; then
	# Cache the USB id while we can still resolve it, for the day we cannot.
	id=$(usb_id_from_tty) && [ -n "$id" ] && usb="$id"
	if [ "$fails" -gt 0 ] || [ "$stage" -gt 0 ]; then
		say "thread is back up (after $fails failed check$([ "$fails" = 1 ] || echo s))"
	fi
	fails=0
	stage=0
	save_state
	exit 0
fi

# Attaching is not a failure, but it is not forever either: a radio stuck
# detached is as useless as a dead one, so it still counts - just from a
# clean slate each time it makes progress.
fails=$((fails + 1))
save_state

if [ "$fails" -lt "$THRESHOLD" ]; then
	say "thread is $state ($fails/$THRESHOLD) - waiting"
	exit 0
fi

# Inside the quiet period the last action is still playing out. Say nothing
# and change nothing; the attach may simply not have finished.
if [ $(( $(now) - last_action )) -lt "$COOLDOWN" ]; then
	exit 0
fi

stage=$((stage + 1))
last_action=$(now)
save_state

if [ "$stage" = 1 ]; then
	say "thread is $state - restarting otbr-agent"
	systemctl restart otbr-agent
else
	# A restart did not do it, so this is the RCP and not the daemon.
	say "thread is still $state after a restart - resetting the radio (attempt $((stage - 1)))"
	reset_radio || exit 1
fi

sleep 15
after=$(health)
if [ "$after" = up ]; then
	say "recovered: thread is $after"
	fails=0
	stage=0
	id=$(usb_id_from_tty) && [ -n "$id" ] && usb="$id"
	save_state
else
	say "still $after - will escalate on the next check"
fi
