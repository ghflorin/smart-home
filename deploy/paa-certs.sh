#!/usr/bin/env bash
#
# Copies the production attestation root certificates (PAA) to the Raspberry Pi.
#
#   ./deploy/paa-certs.sh                       # to the default host
#   ./deploy/paa-certs.sh pi@other-pi.local
#
# WHY. During commissioning, chip-tool checks the device certificate against a
# list of root authorities. Out of the box it only has the TEST roots. A
# commercial device - an IKEA bulb, say - is signed by a production authority,
# so it fails at the 'AttestationVerification' step with
#
#   CHIP Error 0x000000AC: Internal error
#
# The failure lands AFTER BLE has connected and the certificates have been read,
# so it looks like a network problem or a badly reset bulb. It is neither.
#
# Our switch gets through without these certificates, because it carries test
# credentials compiled into its firmware - which is why the switch joins on the
# first try and the bulb never does, making the whole thing more misleading still.
#
# The certificates come from the Matter SDK and are public. They are NOT secret
# and have no business in the fabric backup.

set -euo pipefail

DEST_HOST=${1:-pi@smarthome.local}
DEST_DIR=${DEST_DIR:-/opt/smarthome/ota/paa-root-certs}
NCS_DIR=${NCS_DIR:-$HOME/ncs}
SRC=${PAA_SRC:-$NCS_DIR/modules/lib/matter/credentials/production/paa-root-certs}

[ -d "$SRC" ] || {
	echo "cannot find the certificates in $SRC" >&2
	echo "point PAA_SRC at credentials/production/paa-root-certs in the Matter SDK" >&2
	exit 1
}

N=$(find "$SRC" -name '*.der' | wc -l | tr -d ' ')
[ "$N" -gt 0 ] || { echo "no .der files in $SRC" >&2; exit 1; }

echo "Sending $N certificates to $DEST_HOST:$DEST_DIR"
ssh "$DEST_HOST" "mkdir -p '$DEST_DIR'"
# .der only: chip-tool reads the DER format out of the directory it is given.
# The .pem files sitting next to them are the same certificates in another
# format and would only clutter the directory.
scp -q "$SRC"/*.der "$DEST_HOST:$DEST_DIR/"
echo "done: $(ssh "$DEST_HOST" "ls '$DEST_DIR'/*.der | wc -l | tr -d ' '") on the Pi"

cat <<EOF

For chip-tool to use them, its unit has to carry
    --paa-trust-store-path $DEST_DIR
If you updated smarthome-chiptool.service, reload it:

  sudo cp deploy/smarthome-chiptool.service /etc/systemd/system/
  sudo systemctl daemon-reload && sudo systemctl restart smarthome-chiptool
EOF
