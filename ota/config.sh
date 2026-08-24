#!/usr/bin/env bash
# Shared configuration for the OTA scripts. Edit here, not in the scripts.

# --- Matter nodes ---------------------------------------------------------
# These have to match what you used at commissioning (scripts/commission.sh).
# The default switch. With more than one switch the real list lives in
# panel/devices.json; this variable is only the default for commands that take
# a single node. DEVICE_NODE stays on as an alias, for the OTA scripts.
SWITCH_NODE=${SWITCH_NODE:-2001}
DEVICE_NODE=${DEVICE_NODE:-$SWITCH_NODE}
PROVIDER_NODE=${PROVIDER_NODE:-3001} # the update server (OTA Provider)
ADMIN_NODE=${ADMIN_NODE:-112233}     # chip-tool itself; never take it out of the ACL

# --- Product identity -----------------------------------------------------
# These have to be IDENTICAL to the ones in the firmware, or the provider
# answers "updateNotAvailable" with no explanation at all. See
# CONFIG_CHIP_DEVICE_VENDOR_ID and _PRODUCT_ID. 0xFFF1/0x8004 are the test
# values.
VENDOR_ID=${VENDOR_ID:-0xFFF1}
PRODUCT_ID=${PRODUCT_ID:-0x8004}

# --- Board ----------------------------------------------------------------
# The boards in the house are 25008. The wrong default here would have built
# and served the image for the other variant, and the device would have taken
# it: VID, PID and version are identical across the two builds, so nothing
# would have complained.
BOARD_TARGET=${BOARD_TARGET:-holyiot_25008}

# --- Provider commissioning ----------------------------------------------
# The provider is a desktop application; it is commissioned "onnetwork" (over
# IP), not over BLE. Test values from the Matter SDK.
PROVIDER_PASSCODE=${PROVIDER_PASSCODE:-20202021}
PROVIDER_DISCRIMINATOR=${PROVIDER_DISCRIMINATOR:-3841}

# --- Switch commissioning ------------------------------------------------
# The switch is OUR device, with the test credentials compiled into the
# firmware (CONFIG_CHIP_DEVICE_SPAKE2_PASSCODE and
# CONFIG_CHIP_DEVICE_DISCRIMINATOR). It has no printed code - which is why we
# commission it with a passcode + discriminator instead of a payload.
#
# 0xF00 = 3840. If you change the values in the firmware, change them here too.
SWITCH_PASSCODE=${SWITCH_PASSCODE:-20202021}
SWITCH_DISCRIMINATOR=${SWITCH_DISCRIMINATOR:-3840}

# --- Paths ----------------------------------------------------------------
OTA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$OTA_DIR/.." && pwd)"
NCS_DIR=${NCS_DIR:-$HOME/ncs}

# The Matter sources, for building chip-tool and ota-provider-app.
#
# On the development machine they come from NCS. On the Raspberry Pi, where
# installing all of NCS (~7 GB) just to get the sources makes no sense, they
# are cloned separately - see ota/setup.sh.
#
# NOTE: Nordic uses its own fork, not upstream project-chip.
MATTER_REPO=${MATTER_REPO:-https://github.com/nrfconnect/sdk-connectedhomeip}
MATTER_REV=${MATTER_REV:-v3.0.0}
if [ -n "${MATTER_DIR:-}" ]; then
	:
elif [ -d "$NCS_DIR/modules/lib/matter" ]; then
	MATTER_DIR="$NCS_DIR/modules/lib/matter"
else
	MATTER_DIR="$OTA_DIR/matter-src"
fi

# The host Matter binaries, built by setup.sh.
TOOLS_DIR="$OTA_DIR/tools"
CHIP_TOOL="$TOOLS_DIR/chip-tool"
OTA_PROVIDER="$TOOLS_DIR/chip-ota-provider-app"

SIGNING_KEY="$OTA_DIR/keys/mcuboot-signing.pem"
BUILD_DIR="$REPO_ROOT/build-${BOARD_TARGET}"
OTA_IMAGE="$BUILD_DIR/matter.ota"

# The chip-tool persistent state (the fabric credentials). Do NOT delete it:
# you would lose administrative access to the bulbs and to the module.
export TMPDIR=${TMPDIR:-/tmp}
CHIP_TOOL_STORAGE="$OTA_DIR/state"
