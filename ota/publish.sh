#!/usr/bin/env bash
#
# THIS IS THE SCRIPT YOU RUN WHEN YOU WANT TO CHANGE THE FIRMWARE.
#
#   ./ota/publish.sh              # bump the patch version, build, publish
#   BUMP=none ./ota/publish.sh    # publish what the VERSION file already says
#   BUILD=no ./ota/publish.sh     # publish what is already in the build directory
#
# It puts the image where matter-server looks for updates and tells it to look
# again. The update itself is started from the panel, one switch at a time -
# open the switch, "update" - because a sleepy device takes minutes to fetch an
# image and a switch that is asleep with a flat cell should not hold up the rest.
#
# In order:
#   1. bumps the version - the device refuses an image that is not newer
#   2. builds, PRISTINE, through scripts/build.sh so it is signed with our key
#      (an incremental build has shipped an image whose header said the old
#      version while the application inside was the new one - once was enough)
#   3. writes the descriptor matter-server wants, next to the image, in
#      /opt/smarthome/updates on the Pi, and drops the previous one
#   4. restarts matter-server, which reads that directory at startup only

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/config.sh"

BUMP=${BUMP:-patch}   # major | minor | patch | none
BUILD=${BUILD:-yes}
PI=${PI:-ghflorin@smarthome.local}
UPDATES=${UPDATES:-/opt/smarthome/updates}

die() { echo "ERROR: $*" >&2; exit 1; }

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
s = re.sub(rf"^{key}\s*=\s*\d+", f"{key} = {int(m.group(1)) + 1}", s, flags=re.M)
order = ["VERSION_MAJOR", "VERSION_MINOR", "PATCHLEVEL"]
for lower in order[order.index(key) + 1:]:
    s = re.sub(rf"^{lower}\s*=\s*\d+", f"{lower} = 0", s, flags=re.M)
p.write_text(s)
PY
fi
VERSION=$(python3 - "$VERSION_FILE" <<'PY'
import sys, re
s = open(sys.argv[1]).read()
g = lambda k: int(re.search(rf"^{k}\s*=\s*(\d+)", s, re.M).group(1))
print(f"{g('VERSION_MAJOR')}.{g('VERSION_MINOR')}.{g('PATCHLEVEL')}")
PY
)
echo "firmware $VERSION"

# ---------------------------------------------------------------------------
echo "=== 2. Build ==="
if [ "$BUILD" != "no" ]; then
	"$REPO_ROOT/scripts/build.sh" "$BOARD_TARGET" -p
fi
[ -f "$OTA_IMAGE" ] || die "no image at $OTA_IMAGE"

# The version the IMAGE carries is what the device compares, not the VERSION
# file - so read it back from the MCUboot header rather than trusting the build
# to have picked the file up.
read -r IMAGE_VERSION IMAGE_CODE < <(python3 - "$BUILD_DIR/firmware/zephyr/zephyr.signed.bin" <<'PY'
import struct, sys
d = open(sys.argv[1], 'rb').read(32)
major, minor = d[20], d[21]
rev = struct.unpack('<H', d[22:24])[0]
print(f"{major}.{minor}.{rev}", (major << 24) | (minor << 16) | (rev << 8))
PY
)
[ "$IMAGE_VERSION" = "$VERSION" ] || die "the image says $IMAGE_VERSION, VERSION says $VERSION - build again, pristine"

# ---------------------------------------------------------------------------
echo "=== 3. Descriptor ==="
VID=$((VENDOR_ID)); PID=$((PRODUCT_ID))
NAME=$(printf '%04x_%04x_%s' "$VID" "$PID" "$VERSION")
read -r CHECKSUM SIZE < <(python3 - "$OTA_IMAGE" <<'PY'
import base64, hashlib, sys
d = open(sys.argv[1], 'rb').read()
print(base64.b64encode(hashlib.sha256(d).digest()).decode(), len(d))
PY
)
DESCRIPTOR=$(mktemp)
# otaUrl is a file: URL relative to the updates directory - matter-server strips
# only the leading slash, so this is "the file next to me", not an absolute path.
cat > "$DESCRIPTOR" <<JSON
{
 "modelVersion": {
  "vid": $VID,
  "pid": $PID,
  "softwareVersion": $IMAGE_CODE,
  "softwareVersionString": "$VERSION",
  "cdVersionNumber": 1,
  "firmwareInformation": "",
  "minApplicableSoftwareVersion": 0,
  "maxApplicableSoftwareVersion": 4294967294,
  "otaUrl": "file:///$NAME.ota",
  "otaFileSize": $SIZE,
  "otaChecksum": "$CHECKSUM",
  "otaChecksumType": 1,
  "releaseNotesUrl": ""
 }
}
JSON
echo "$NAME.ota  $SIZE bytes"

# ---------------------------------------------------------------------------
echo "=== 4. Publish ==="
scp -q "$OTA_IMAGE" "$PI:$UPDATES/$NAME.ota"
scp -q "$DESCRIPTOR" "$PI:$UPDATES/$NAME.json"
rm -f "$DESCRIPTOR"
# One offer at a time. matter-server would pick the newest anyway; the old
# files just take up the card.
ssh "$PI" "cd $UPDATES && for f in \$(ls ${NAME%_*}_*.json ${NAME%_*}_*.ota 2>/dev/null | grep -v '^$NAME\\.'); do rm -f \"\$f\"; done; sudo systemctl restart smarthome-matter"

cat <<EOT

Published $VERSION. matter-server is restarting; give it a minute, then update
each switch from the panel. The lock first, if the change is about the lock.
EOT
