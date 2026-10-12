#!/usr/bin/env bash
#
# Makes https://smarthome.local work without a warning. Run it ON THE HUB:
#
#   sudo ./deploy/tls.sh
#   sudo systemctl restart smarthome-panel
#
# The house gets a certificate authority of its own, once, and the panel a
# certificate from it. A phone trusts the panel once it has installed that
# authority, which the panel hands out at http://smarthome.local/ca.crt. On an
# iPhone it then has to be switched on as well, under Settings > General >
# About > Certificate Trust Settings.
#
# The authority can only vouch for smarthome.local and private addresses (the
# nameConstraints line): a phone that trusts it trusts it for the panel, and
# its key - which stays on the hub - is no use against any other site.
#
# Run it again when the panel's certificate runs out (825 days, the most an
# iPhone accepts from a private authority) or the hub's address changes. The
# authority stays, so the phones do not need it again.
set -euo pipefail

DIR=${TLS_DIR:-/opt/smarthome/ota/state/tls}
USER_NAME=${SMARTHOME_USER:-smarthome}
NAME=${NAME:-smarthome.local}
IF=$(ip route show default | awk '{print $5; exit}')
IP=$(ip -4 -o addr show dev "$IF" | awk '{print $4; exit}' | cut -d/ -f1)
[ -n "$IP" ] || { echo "ERROR cannot work out this machine's address" >&2; exit 1; }

install -d -m 700 "$DIR"
cd "$DIR"

if [ ! -f ca.key ]; then
	openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
		-keyout ca.key -out ca.crt -days 3650 -subj "/CN=smarthome local authority" \
		-addext "keyUsage=critical,keyCertSign,cRLSign" \
		-addext "nameConstraints=critical,permitted;DNS:$NAME,permitted;IP:192.168.0.0/255.255.0.0,permitted;IP:10.0.0.0/255.0.0.0,permitted;IP:172.16.0.0/255.240.0.0"
fi

openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
	-keyout panel.key -out panel.csr -subj "/CN=$NAME"
openssl x509 -req -in panel.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
	-days 825 -out panel.crt -extfile <(printf '%s\n' \
		"subjectAltName=DNS:$NAME,IP:$IP" \
		"extendedKeyUsage=serverAuth" \
		"keyUsage=critical,digitalSignature" \
		"basicConstraints=critical,CA:FALSE")
cat panel.crt panel.key > panel.pem
rm -f panel.csr

chown -R "$USER_NAME:$USER_NAME" "$DIR"
chmod 600 ca.key panel.key panel.pem
openssl verify -CAfile ca.crt panel.crt
echo "  ok    $NAME and $IP, until $(openssl x509 -in panel.crt -noout -enddate | cut -d= -f2)"
