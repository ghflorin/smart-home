#!/usr/bin/env bash
#
# Builds chip-tool and chip-ota-provider-app FOR the Raspberry Pi, but on the
# Mac, inside a Debian arm64 container.
#
#   ./deploy/build-tools-docker.sh
#
# WHY: the Matter tools want a lot of RAM to link. On a Raspberry Pi 3 B+, with
# 1 GB, the build is effectively impossible. But the Pi RUNS them without
# trouble - at runtime they use under 200 MB.
#
# On an Apple Silicon Mac, linux/arm64 containers run natively, with no
# emulation. So we build at Mac speed and end up with aarch64 binaries that run
# directly on the Pi.
#
# The image has to be the SAME Debian generation as the OS on the Pi. Check on
# the Pi with:  cat /etc/debian_version
#
#   Debian 13 (Trixie)   -> debian:trixie     <- current Raspberry Pi OS
#   Debian 12 (Bookworm) -> debian:bookworm
#
# Override it with IMAGE=debian:bookworm ./deploy/build-tools-docker.sh
#
# The direction matters: a binary built against an older glibc runs on a newer
# one, but not the other way around. So if you get it wrong, get it wrong toward
# the older version.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$REPO_ROOT/ota/tools-linux-arm64"
IMAGE=${IMAGE:-debian:trixie}
MATTER_REPO=${MATTER_REPO:-https://github.com/nrfconnect/sdk-connectedhomeip}
MATTER_REV=${MATTER_REV:-v3.0.0}

command -v docker >/dev/null || { echo "docker is not installed"; exit 1; }
docker info >/dev/null 2>&1 || { echo "the docker daemon is not running"; exit 1; }

HOST_ARCH=$(docker info --format '{{.Architecture}}')
if [ "$HOST_ARCH" != "aarch64" ]; then
	echo "WARNING: the docker daemon is $HOST_ARCH, not aarch64."
	echo "The build will be emulated and will take a very long time. Continue? [y/N]"
	read -r ans; [ "$ans" = "y" ] || exit 0
fi

mkdir -p "$OUT_DIR"

echo "=== Building in container $IMAGE (linux/arm64) ==="
echo "    this takes a while; start it when you have the time"

# bash -exc, without -u: the Matter scripts/activate.sh reads unset variables
# (PW_ENVIRONMENT_ROOT) and dies instantly under "set -u".
docker run --rm --platform linux/arm64 \
	-v "$OUT_DIR:/out" \
	-e MATTER_REPO="$MATTER_REPO" \
	-e MATTER_REV="$MATTER_REV" \
	"$IMAGE" bash -exc '
	export DEBIAN_FRONTEND=noninteractive
	apt-get update
	# The list from docs/guides/BUILDING.md in the Matter repo, plus curl and
	# unzip for gn.
	apt-get install -y --no-install-recommends \
		git gcc g++ pkg-config libssl-dev libdbus-1-dev \
		libglib2.0-dev libavahi-client-dev ninja-build python3-venv \
		python3-dev python3-pip unzip libgirepository1.0-dev \
		libcairo2-dev libreadline-dev ca-certificates curl \
		nodejs npm

	git clone --depth 1 --branch "$MATTER_REV" "$MATTER_REPO" /matter
	cd /matter
	git submodule update --init --depth 1 --recursive

	# gn, at the revision pigweed pins. Not "latest": a newer version fails
	# with "Duplicate output file" on pw_chrono.
	GN_REV=$(python3 -c "
import json
d = json.load(open(\"third_party/pigweed/repo/pw_env_setup/py/pw_env_setup/cipd_setup/pigweed.json\"))
print(next(t for p in d[\"packages\"] if p[\"path\"].startswith(\"gn/gn\") for t in p[\"tags\"]))
")
	echo "pinned gn: $GN_REV"
	curl -sL -o /tmp/gn.zip \
		"https://chrome-infra-packages.appspot.com/dl/gn/gn/linux-arm64/+/$GN_REV"
	unzip -o -q /tmp/gn.zip -d /tmp/gnbin
	install -m 0755 /tmp/gnbin/gn /usr/local/bin/gn
	gn --version

	# No CIPD: the fuchsia/third_party/zap package no longer allows anonymous
	# access, so bootstrap attempts an interactive login, which fails with no
	# terminal attached.
	export PW_CONFIG_FILE=scripts/setup/environment_no_cipd.json
	source scripts/activate.sh

	# ZAP is needed anyway: chip-tool generates its data model at build time.
	# We run it from source with node, so we depend neither on CIPD nor on the
	# release binaries (the mac arm64 ones ship an x86 zap-cli).
	ZAP_REV=$(python3 -c "
import json, re
t = json.load(open(\"scripts/setup/zap.json\"))[\"packages\"][0][\"tags\"][0]
t = t.split(\"@\", 1)[1]
print(re.sub(r\"\.[0-9]+$\", \"\", t))
")
	echo "pinned ZAP: $ZAP_REV"
	git clone --depth 1 --branch "$ZAP_REV" https://github.com/project-chip/zap /zap
	cd /zap && npm ci --cache /tmp/npm-cache && cd /matter
	export ZAP_DEVELOPMENT_PATH=/zap
	export ZAP_SKIP_REAL_VERSION=1

	gn gen --check --fail-on-unused-args --root=examples/chip-tool out/chip-tool
	ninja -C out/chip-tool

	gn gen --check --fail-on-unused-args \
		--root=examples/ota-provider-app/linux out/ota-provider \
		--args="chip_config_network_layer_ble=false"
	ninja -C out/ota-provider

	cp out/chip-tool/chip-tool /out/
	cp out/ota-provider/chip-ota-provider-app /out/
	chmod 755 /out/chip-tool /out/chip-ota-provider-app
	'

echo
echo "Binaries in: $OUT_DIR"
file "$OUT_DIR/chip-tool" 2>/dev/null || true
echo
cat <<EOF
Copy them to the Pi:

  scp $OUT_DIR/* pi@smarthome.local:/opt/smarthome/ota/tools/

The Pi needs the runtime libraries (deploy/prepare-pi.sh installs them):
libavahi-client3, libdbus-1-3, libglib2.0-0, libssl3.

Check on the Pi that the binary actually starts:
  /opt/smarthome/ota/tools/chip-tool --version
EOF
