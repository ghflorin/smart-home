#!/usr/bin/env bash
#
# RUN THIS ONCE. Generates the signing key and builds the host Matter tools
# (chip-tool + chip-ota-provider-app).
#
# The first run takes a long time (~30-60 min): the Matter bootstrap downloads
# a pigweed environment of several GB into $NCS/modules/lib/matter/.environment.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/config.sh"

echo "=== 1. MCUboot signing key ==="
if [ -f "$SIGNING_KEY" ]; then
	echo "already there: $SIGNING_KEY (not overwriting it)"
else
	mkdir -p "$(dirname "$SIGNING_KEY")"
	# shellcheck disable=SC1091
	source "$NCS_DIR/.venv/bin/activate"
	python "$NCS_DIR/bootloader/mcuboot/scripts/imgtool.py" keygen \
		-k "$SIGNING_KEY" -t ed25519
	echo "generated: $SIGNING_KEY"
fi
cat <<'EOF'

  Back it up NOW, outside the repo (it is in .gitignore).
  Lose it after the modules have been flashed and you can no longer ship OTA
  updates - the bootloader rejects any image signed with a different key, and
  the only way out is a reflash over SWD.

EOF

echo "=== 2. Host Matter tools ==="
mkdir -p "$TOOLS_DIR"

if [ -x "$CHIP_TOOL" ] && [ -x "$OTA_PROVIDER" ]; then
	echo "already present in $TOOLS_DIR"
	exit 0
fi

# Without NCS (the Raspberry Pi case), clone the Matter sources separately.
# Exactly the revision NCS v3.0 uses, so no protocol or command-syntax drift
# creeps in against what is documented here.
if [ ! -d "$MATTER_DIR/.git" ] && [ ! -d "$MATTER_DIR/src" ]; then
	echo "no Matter sources found - cloning into $MATTER_DIR"
	echo "  $MATTER_REPO @ $MATTER_REV"
	git clone --depth 1 --branch "$MATTER_REV" "$MATTER_REPO" "$MATTER_DIR"
	git -C "$MATTER_DIR" submodule update --init --depth 1 --recursive
fi

cd "$MATTER_DIR"

# --- three workarounds, all forced on us by changes at CIPD ---
#
# 1. CIPD no longer grants anonymous access to fuchsia/third_party/zap. The
#    bootstrap attempts an interactive login and dies. Route around it with
#    the no-CIPD config.
# 2. Without CIPD, gn does not arrive either - so we fetch it ourselves, at the
#    revision pigweed pins. Do NOT use "latest": it fails with
#    "Duplicate output file".
# 3. ZAP is still required (chip-tool generates its data model at build time),
#    so we run it from source with node.

if ! command -v gn >/dev/null; then
	GN_REV=$(python3 -c "
import json
d = json.load(open('third_party/pigweed/repo/pw_env_setup/py/pw_env_setup/cipd_setup/pigweed.json'))
print(next(t for p in d['packages'] if p['path'].startswith('gn/gn') for t in p['tags']))
")
	case "$(uname -s)-$(uname -m)" in
		Darwin-arm64) GN_PLAT=mac-arm64 ;;
		Darwin-*)     GN_PLAT=mac-amd64 ;;
		Linux-aarch64) GN_PLAT=linux-arm64 ;;
		*)            GN_PLAT=linux-amd64 ;;
	esac
	echo "fetching gn ($GN_REV, $GN_PLAT)..."
	mkdir -p "$OTA_DIR/tools"
	TMP=$(mktemp -d)
	curl -sL -o "$TMP/gn.zip" \
		"https://chrome-infra-packages.appspot.com/dl/gn/gn/$GN_PLAT/+/$GN_REV"
	unzip -o -q "$TMP/gn.zip" -d "$TMP"
	install -m 0755 "$TMP/gn" "$OTA_DIR/tools/gn"
	rm -rf "$TMP"
	export PATH="$OTA_DIR/tools:$PATH"
fi

if [ ! -d "$OTA_DIR/zap-src/node_modules" ]; then
	ZAP_REV=$(python3 -c "
import json, re
t = json.load(open('scripts/setup/zap.json'))['packages'][0]['tags'][0]
print(re.sub(r'\.[0-9]+$', '', t.split('@', 1)[1]))
")
	echo "fetching ZAP from source ($ZAP_REV)..."
	command -v node >/dev/null || { echo "node is missing - install it"; exit 1; }
	git clone --depth 1 --branch "$ZAP_REV" \
		https://github.com/project-chip/zap "$OTA_DIR/zap-src"
	( cd "$OTA_DIR/zap-src" && npm ci --cache /tmp/npm-cache )
fi
export ZAP_DEVELOPMENT_PATH="$OTA_DIR/zap-src"
export ZAP_SKIP_REAL_VERSION=1

echo "Matter bootstrap (long the first time)..."
export PW_CONFIG_FILE=scripts/setup/environment_no_cipd.json
# shellcheck disable=SC1091
source scripts/activate.sh

# Link parallelism. A single link job takes over 1 GB; with the default -j (the
# core count) a Pi 4 runs out of memory and the process is killed by the OOM
# killer - usually right at the end, after hours of compiling.
#
# We cannot use scripts/examples/gn_build_example.sh, because it only accepts
# -v and key=value arguments - there is no way to hand it -j. So we call gn gen
# + ninja directly, which is exactly what it does.
TOTAL_MB=$(( $(getconf _PHYS_PAGES 2>/dev/null || echo 0) \
             * $(getconf PAGE_SIZE 2>/dev/null || echo 0) / 1048576 ))
NINJA_JOBS=()
if [ "$TOTAL_MB" -gt 0 ] && [ "$TOTAL_MB" -lt 6000 ]; then
	NINJA_JOBS=(-j2)
	echo "RAM: ${TOTAL_MB} MB -> capping at 2 parallel jobs"
elif [ "$TOTAL_MB" -gt 0 ]; then
	echo "RAM: ${TOTAL_MB} MB -> default parallelism"
fi

echo "building chip-tool... (hours on a Raspberry Pi)"
gn gen --check --fail-on-unused-args --root=examples/chip-tool out/chip-tool
ninja -C out/chip-tool "${NINJA_JOBS[@]}"

echo "building chip-ota-provider-app..."
gn gen --check --fail-on-unused-args --root=examples/ota-provider-app/linux \
	out/ota-provider --args='chip_config_network_layer_ble=false'
ninja -C out/ota-provider "${NINJA_JOBS[@]}"

cp "$MATTER_DIR/out/chip-tool/chip-tool" "$CHIP_TOOL"
cp "$MATTER_DIR/out/ota-provider/chip-ota-provider-app" "$OTA_PROVIDER"

echo
echo "done: $TOOLS_DIR"
