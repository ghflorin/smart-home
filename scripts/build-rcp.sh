#!/usr/bin/env bash
#
# Firmware for the border router's radio (OpenThread RCP).
#
#   ./scripts/build-rcp.sh                 # nRF52840 Dongle (PCA10059)
#   ./scripts/build-rcp.sh particle_xenon  # Particle Xenon
#
# RCP = Radio Co-Processor: it does nothing but the 802.15.4 radio, while the
# Thread stack runs on the Raspberry Pi. They talk over USB. The computer has no
# Thread radio of its own, which is the whole reason this piece exists.
#
# SWD DEBUGGING TRAP that cost us an afternoon: if the board does not enumerate,
# do NOT read the USBD registers before checking USBD.LOWPOWER (0x4002752C).
# Once the bus has been idle for more than 3 ms the peripheral drops into low
# power, and from that point the entire register block below 0x40027508 reads
# back as zeros. USBPULLUP looks like 0, EPINEN looks like 0, and you conclude
# the firmware never brought USB up - when in fact D+ has been asserted since
# boot. Write 0 to LOWPOWER and read again. From RAM, usbd_ctx.attached/.ready
# tell the truth regardless.
#
# The zeros that do mean something, and that low power does not affect:
# USBADDR (0x40027470) and EVENTS_USBRESET (0x40027100) still reading 0 means
# the host never answered - so the fault is the cable or the data traces, not
# the firmware. VBUS being present proves nothing about D+/D-.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NCS_DIR=${NCS_DIR:-$HOME/ncs}
TARGET=${1:-dongle}

EXTRA=()

case "$TARGET" in
	dongle|nrf52840dongle)
		BOARD="nrf52840dongle/nrf52840"
		TARGET=dongle
		;;
	particle_xenon|xenon)
		BOARD="particle_xenon"
		TARGET=xenon
		# The Xenon has external SPI NOR flash and a GPIO antenna switch. The
		# board init code references both directly, so without these drivers
		# the link fails on __device_dts_ord_9 and on the nrfx_gpiote_*
		# symbols.
		EXTRA=(
			-Dcoprocessor_CONFIG_GPIO=y
			-Dcoprocessor_CONFIG_SPI=y
			-Dcoprocessor_CONFIG_FLASH=y
			-Dcoprocessor_CONFIG_SPI_NOR=y
			-Dcoprocessor_CONFIG_NRFX_GPIOTE0=y
		)
		;;
	*)
		BOARD="$TARGET"
		;;
esac

# shellcheck disable=SC1091
source "$NCS_DIR/.venv/bin/activate"
# shellcheck disable=SC1091
source "$NCS_DIR/zephyr/zephyr-env.sh"
export ZEPHYR_TOOLCHAIN_VARIANT=zephyr
export PATH="$NCS_DIR/tools/bin:$PATH"

west build -b "$BOARD" --sysbuild \
	-d "$REPO_ROOT/build-rcp-${TARGET}" \
	"$NCS_DIR/nrf/samples/openthread/coprocessor" \
	-- -Dcoprocessor_SNIPPET=usb "${EXTRA[@]}"

HEX="$REPO_ROOT/build-rcp-${TARGET}/coprocessor/zephyr/zephyr.hex"
echo
echo "Artifact: $HEX"
echo
case "$TARGET" in
	dongle)
		cat <<'EOF'
The dongle has a USB bootloader - no programmer needed:
  1. plug it into USB and press the side button (red LED pulsing)
  2. nRF Connect for Desktop -> Programmer -> select the hex -> Write
EOF
		;;
	xenon)
		cat <<'EOF'
The image goes to 0x0, so it overwrites the Particle bootloader. The board stops
being a Particle device and can no longer be flashed over DFU - from here on,
SWD only.

SAVE A COPY FIRST and the decision stays reversible:
  pyocd cmd -t nrf52840 -c "savemem 0 0x100000 xenon-original.bin"

Then:
  pyocd flash -t nrf52840 <hex>

On the Xenon, SWDIO/SWCLK are on the 10-pin debug connector.
EOF
		;;
esac
