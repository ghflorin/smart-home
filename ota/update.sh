#!/usr/bin/env bash
#
# THIS IS THE SCRIPT YOU RUN WHEN YOU WANT TO CHANGE THE FIRMWARE.
#
#   ./ota/update.sh
#
# In order, it:
#   1. bumps the software version (otherwise the device refuses the update)
#   2. builds and signs the image
#   3. starts the OTA Provider
#   4. tells the module to look at the provider
#   5. waits for the transfer, then stops the provider
#
# The provider runs ONLY for that window. Nothing stays up between updates.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/config.sh"

BUMP=${BUMP:-patch}   # major | minor | patch | none

die() { echo "ERROR: $*" >&2; exit 1; }

[ -x "$CHIP_TOOL" ] || die "chip-tool is missing. Run this first: ./ota/setup.sh"
[ -x "$OTA_PROVIDER" ] || die "chip-ota-provider-app is missing. Run: ./ota/setup.sh"
[ -f "$SIGNING_KEY" ] || die "the signing key is missing. Run: ./ota/setup.sh"

# ---------------------------------------------------------------------------
echo "=== 1. Version ==="
VERSION_FILE="$REPO_ROOT/firmware/VERSION"
if [ "$BUMP" != "none" ]; then
	python3 - "$VERSION_FILE" "$BUMP" <<'PY'
import sys, re, pathlib
path, part = sys.argv[1], sys.argv[2]
p = pathlib.Path(path); s = p.read_text()
keys = {"major": "VERSION_MAJOR", "minor": "VERSION_MINOR", "patch": "PATCHLEVEL"}
key = keys[part]
m = re.search(rf"^{key}\s*=\s*(\d+)", s, re.M)
new = int(m.group(1)) + 1
s = re.sub(rf"^{key}\s*=\s*\d+", f"{key} = {new}", s, flags=re.M)
# reset the lower-order components
order = ["VERSION_MAJOR", "VERSION_MINOR", "PATCHLEVEL"]
for lower in order[order.index(key) + 1:]:
    s = re.sub(rf"^{lower}\s*=\s*\d+", f"{lower} = 0", s, flags=re.M)
p.write_text(s)
PY
fi
grep -E "VERSION_MAJOR|VERSION_MINOR|PATCHLEVEL" "$VERSION_FILE" | tr '\n' ' '
echo

# ---------------------------------------------------------------------------
echo "=== 2. Build ==="
"$REPO_ROOT/scripts/build.sh" "$BOARD_TARGET"
[ -f "$OTA_IMAGE" ] || die "$OTA_IMAGE was not produced"

# Confirm the version in the image really did go up against what is running on
# the device. If it did not, the provider answers updateNotAvailable and you
# wait for nothing, with no error message anywhere.
# shellcheck disable=SC1091
source "$NCS_DIR/.venv/bin/activate"
python "$MATTER_DIR/src/app/ota_image_tool.py" show "$OTA_IMAGE" | sed -n '1,12p'

# ---------------------------------------------------------------------------
echo "=== 3. Starting the OTA Provider ==="
mkdir -p "$CHIP_TOOL_STORAGE"
"$OTA_PROVIDER" \
	--discriminator "$PROVIDER_DISCRIMINATOR" \
	--passcode "$PROVIDER_PASSCODE" \
	--secured-device-port 5541 \
	--KVS "$CHIP_TOOL_STORAGE/provider.kvs" \
	--filepath "$OTA_IMAGE" &
PROVIDER_PID=$!
trap 'echo; echo "stopping the provider (pid $PROVIDER_PID)"; kill $PROVIDER_PID 2>/dev/null || true' EXIT
sleep 3
kill -0 $PROVIDER_PID 2>/dev/null || die "the provider died at startup"

# Commission the provider into the fabric, once. If it is already there the
# command fails and we carry on.
if ! "$CHIP_TOOL" pairing onnetwork "$PROVIDER_NODE" "$PROVIDER_PASSCODE" \
	--storage-directory "$CHIP_TOOL_STORAGE" 2>/dev/null; then
	echo "(the provider looks already commissioned - carrying on)"
fi

# ACL on the provider: any node in the fabric may query the OTA Provider
# cluster (0x0029 = 41). The first entry is the admin; leave it out and you
# lose access to the provider.
"$CHIP_TOOL" accesscontrol write acl "[
  {\"fabricIndex\":1,\"privilege\":5,\"authMode\":2,\"subjects\":[$ADMIN_NODE],\"targets\":null},
  {\"fabricIndex\":1,\"privilege\":3,\"authMode\":2,\"subjects\":null,
   \"targets\":[{\"cluster\":41,\"endpoint\":null,\"deviceType\":null}]}
]" "$PROVIDER_NODE" 0 --storage-directory "$CHIP_TOOL_STORAGE"

# ---------------------------------------------------------------------------
echo "=== 4. Announcement to the module ==="
# Arguments (checked against zzz_generated/chip-tool/.../Commands.h):
#   ProviderNodeID VendorID AnnouncementReason Endpoint  <destination> <endpoint>
# AnnouncementReason: 0=Simple, 1=UpdateAvailable, 2=UrgentUpdateAvailable.
# MetadataForNode is optional and is omitted.
#
# The module is a Sleepy End Device: the announcement is INBOUND traffic, so it
# can be delayed by up to the poll interval (minutes). Nothing happening right
# away is normal.
"$CHIP_TOOL" otasoftwareupdaterequestor announce-ota-provider \
	"$PROVIDER_NODE" "$VENDOR_ID" 1 0 \
	"$DEVICE_NODE" 0 \
	--storage-directory "$CHIP_TOOL_STORAGE"

# ---------------------------------------------------------------------------
echo "=== 5. Waiting for the transfer ==="
echo "Watch the provider logs above. When you see BDX finish, the module"
echo "applies the image and resets itself."
echo "Press Ctrl-C when it is done (the provider stops on its own)."
wait $PROVIDER_PID
