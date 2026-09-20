#!/usr/bin/env bash
#
# Publishes an extra mDNS name for this machine, so the panel keeps the address
# people have in their phones whatever the machine is called.
#
#   ./deploy/mdns-alias.sh smarthome.local
#
# WHY. The house moved off a Pi called `smarthome` onto a machine that already
# had a name and a job. Renaming that machine to suit us is the wrong way round:
# it is somebody else's box first. avahi will happily answer for a second name,
# so it does.
#
# Stays in the foreground - avahi-publish holds the record only while it runs,
# which is exactly what a systemd service wants.
set -euo pipefail

NAME=${1:-smarthome.local}
IF=${INFRA_IF:-$(ip route show default 2>/dev/null | awk '{print $5; exit}')}
[ -n "$IF" ] || { echo "no interface toward the house" >&2; exit 1; }

# The address is read at start, not baked in: this machine is on DHCP and a
# record pointing at an address it no longer holds is worse than no record.
IP=$(ip -4 -brief addr show "$IF" | awk '{print $3}' | cut -d/ -f1)
[ -n "$IP" ] || { echo "no IPv4 on $IF" >&2; exit 1; }

echo "publishing $NAME -> $IP on $IF"
exec avahi-publish -a -R "$NAME" "$IP"
