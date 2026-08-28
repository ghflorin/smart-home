#!/usr/bin/env bash
#
# Build. Assumes bootstrap.sh has already run.
#
# Usage:
#   ./scripts/build.sh                    # holyiot_25008, the switch
#   ./scripts/build.sh nrf54l15dk         # Nordic DK, for debugging over shell/RTT
#   ./scripts/build.sh holyiot_25008 -p   # pristine

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NCS_DIR=${NCS_DIR:-$HOME/ncs}
TARGET=${1:-holyiot_25008}
shift || true

case "$TARGET" in
	holyiot_25008) BOARD="holyiot_25008/nrf54l15/cpuapp" ;;
	nrf54l15dk)    BOARD="nrf54l15dk/nrf54l15/cpuapp" ;;
	*)             BOARD="$TARGET" ;;
esac

# shellcheck disable=SC1091
source "$NCS_DIR/.venv/bin/activate"
# shellcheck disable=SC1091
source "$NCS_DIR/zephyr/zephyr-env.sh"
export ZEPHYR_TOOLCHAIN_VARIANT=zephyr
# gn has to be EXACTLY the revision pigweed pins; a newer one gives
# "Duplicate output file" on pw_chrono. See bootstrap.sh.
export PATH="$NCS_DIR/tools/bin:$PATH"

SIGNING_KEY="$REPO_ROOT/ota/keys/mcuboot-signing.pem"
if [ ! -f "$SIGNING_KEY" ]; then
	echo "ERROR: the MCUboot signing key is missing:"
	echo "  $SIGNING_KEY"
	echo "Run this first: ./ota/setup.sh"
	echo
	echo "Without it the build would use the default NCS key, which is PUBLIC -"
	echo "meaning anyone can sign firmware the device will accept."
	exit 1
fi

# Everything after the board name goes to CMake, not to west: that is where
# things like -DEXTRA_CONF_FILE=debug-rtt.conf belong.
west build -b "$BOARD" --sysbuild \
	-d "$REPO_ROOT/build-${TARGET}" \
	"$REPO_ROOT/firmware" \
	-- -DBOARD_ROOT="$REPO_ROOT/firmware" \
	   -DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="\"$SIGNING_KEY\"" \
	   "$@"

echo
echo "Artifact: $REPO_ROOT/build-${TARGET}/merged.hex"
echo
echo "Flash over SWD (the Holyiot modules have no USB):"
echo "  ./scripts/flash.sh ${TARGET}"
