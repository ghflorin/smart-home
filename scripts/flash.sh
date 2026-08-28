#!/usr/bin/env bash
#
# Flash over SWD using a Raspberry Pi Pico as the probe.
#
#   ./scripts/flash.sh                 # holyiot_25008, the switch
#   ./scripts/flash.sh xenon            # the border router's radio
#   ./scripts/flash.sh --erase chip     # also wipes commissioning
#
# Same programmer for both - only the chip differs (nrf54l vs nrf52840).
#
# The Holyiot modules have no USB. They are flashed over SWD, using an RP2040
# running the debugprobe firmware (formerly picoprobe), from raspberrypi/debugprobe.
#
# From the release, take "debugprobe_on_pico.uf2", NOT the other two:
#   debugprobe.uf2         the official Debug Probe board, different pins
#   debugprobe_on_pico2.uf2  RP2350 (Pico 2), a different chip
# The _on_pico variant is built for a plain RP2040 and uses
# GP2 = SWCLK, GP3 = SWDIO - exactly what this script assumes.
#
# To install it, hold BOOT while plugging in: an RPI-RP2 disk appears, drop the
# .uf2 onto it, and the disk disappears on its own after a few seconds. Verified
# with debugprobe-v2.3.1; it then shows up as
# "Raspberry Pi Debugprobe on Pico (CMSIS-DAP)".
#
# MIND THE PINOUT - it differs between boards. The firmware drives GPIO2 and
# GPIO3 at the chip level; what changes is how the pads are labeled:
#
#   signal             Raspberry Pi Pico    Seeed XIAO RP2040
#   SWCLK (GPIO2)      GP2                  D8
#   SWDIO (GPIO3)      GP3                  D10
#   GND                GND                  GND
#   3V3                3V3                  3V3   (optional)
#
# On the XIAO, D9 is GPIO4 - NOT GPIO3 - even though it sits physically between
# D8 and D10. debugprobe uses GPIO4 for UART, so wiring it to SWDIO gets you
# nothing at all and looks like broken hardware.
#
# WARNING: do not power the module from the Pico and from the CR2032 cell at the
# same time - pull the cell if you are using 3V3 from the Pico.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET=${1:-holyiot_25008}
shift || true

# The pyocd target name is "nrf54l", NOT "nrf54l15".
# Verified with: pyocd list --targets | grep nrf54
#   nrf54l       Nordic Semiconductor   NRF54L15   builtin
PYOCD_TARGET=nrf54l
HEX="$REPO_ROOT/build-${TARGET}/merged.hex"
BUILD_HINT="./scripts/build.sh $TARGET"

# The border router's radio: different chip, different image, same programmer.
case "$TARGET" in
	xenon|particle_xenon)
		PYOCD_TARGET=nrf52840
		HEX="$REPO_ROOT/build-rcp-xenon/coprocessor/zephyr/zephyr.hex"
		BUILD_HINT="./scripts/build-rcp.sh particle_xenon"
		cat <<'EOF'
WARNING: this overwrites the Particle bootloader. Save a copy first so you can
go back if you ever want the board as a Particle or Arduino again:

  pyocd cmd -t nrf52840 -c "savemem 0 0x100000 xenon-original.bin"

EOF
		read -r -p "Continue? [y/N] " ans
		[ "$ans" = "y" ] || { echo "canceled"; exit 0; }
		;;
	dongle|nrf52840dongle)
		echo "The dongle has a USB bootloader - no programmer needed."
		echo "Use nRF Connect for Desktop -> Programmer, with:"
		echo "  $REPO_ROOT/build-rcp-dongle/coprocessor/zephyr/zephyr.hex"
		exit 0
		;;
esac

[ -f "$HEX" ] || { echo "$HEX is missing - run $BUILD_HINT first"; exit 1; }

# Look for pyocd inside venvs too, not just on PATH.
#
# NCS ships its own pyocd in ~/ncs/.venv, but that venv is not active in an
# ordinary shell. Without this search the script reports "pyocd is not
# installed" while a perfectly good copy sits on disk, and you end up installing
# a second one - exactly the kind of friction that shows up right when you want
# to get started.
PYOCD=$(command -v pyocd 2>/dev/null || true)
if [ -z "$PYOCD" ]; then
	for candidate in "$HOME/ncs/.venv/bin/pyocd" "$HOME/.venv-pyocd/bin/pyocd" \
			"$REPO_ROOT/.venv/bin/pyocd"; do
		[ -x "$candidate" ] && { PYOCD="$candidate"; break; }
	done
fi

[ -n "$PYOCD" ] || {
	cat <<'EOF'
pyocd is nowhere to be found. In a venv:

  python3 -m venv ~/.venv-pyocd
  source ~/.venv-pyocd/bin/activate
  pip install pyocd

nRF54L15 support is built in as of pyocd 0.45 - no CMSIS pack needed.
EOF
	exit 1
}
echo "pyocd: $PYOCD ($("$PYOCD" --version 2>/dev/null))"

echo "=== Probes detected ==="
PROBES=$("$PYOCD" list 2>&1 || true)
echo "$PROBES"
if ! echo "$PROBES" | grep -qE "^ +[0-9]+ "; then
	cat <<'EOF'

No probe. Check these, in the order they usually go wrong:

  1. Does the XIAO have the debugprobe firmware? Holding BOOT while plugging in
     brings up an "RPI-RP2" disk; drop a .uf2 on it and it reboots itself.
     https://github.com/raspberrypi/debugprobe/releases (debugprobe_on_pico.uf2)
  2. The USB cable is a data cable, not charge-only.
  3. The wiring: SWCLK on D8, SWDIO on D10, GND to GND. NOT D9 - that one is GPIO4.
EOF
	exit 1
fi
echo

echo "=== Flash $TARGET ==="
# By default pyocd only erases the sectors it writes, so settings_storage
# (Matter fabric, ACL, binding) SURVIVES a reflash. To really start from
# scratch, add: --erase chip
"$PYOCD" flash -t "$PYOCD_TARGET" "$@" "$HEX"

echo
echo "Done. If that was the first flash, commissioning comes next:"
echo "  scripts/commission.sh"
echo "After that, updates go over the air: ./ota/update.sh"
