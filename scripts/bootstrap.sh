#!/usr/bin/env bash
#
# Installs the nRF Connect SDK and stages the application on top of the
# matter/light_switch sample (that is where the ZAP file and the endpoint with
# the client clusters come from).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NCS_DIR=${NCS_DIR:-$HOME/ncs}
NCS_VERSION=${NCS_VERSION:-v3.0.0}

echo "=== macOS dependencies ==="
command -v brew >/dev/null || { echo "install Homebrew first"; exit 1; }
# python@3.12 explicitly: NCS does not work on 3.13+ (some packages in
# requirements.txt have no wheels), and macOS may already ship a newer python3.
brew install cmake ninja gperf ccache dtc wget python@3.12

echo "=== nRF Connect SDK $NCS_VERSION in $NCS_DIR ==="
mkdir -p "$NCS_DIR"
cd "$NCS_DIR"
/opt/homebrew/bin/python3.12 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install west
west init -m https://github.com/nrfconnect/sdk-nrf --mr "$NCS_VERSION" .
west update
west zephyr-export
pip install -r zephyr/scripts/requirements.txt
pip install -r nrf/scripts/requirements.txt
# Needed to generate factory data (the DK build).
pip install -r modules/lib/matter/scripts/setup/requirements.nrfconnect.txt

echo "=== ARM toolchain (Zephyr SDK) ==="
west sdk install --version 0.17.4 -t arm-zephyr-eabi

echo "=== gn ==="
# Matter builds with GN. The version has to be EXACTLY the one pigweed pins - a
# newer one fails with "Duplicate output file" on pw_chrono. Homebrew has no gn
# formula, so fetch it from CIPD.
GN_REV=$(python3 -c "
import json
d = json.load(open('$NCS_DIR/modules/lib/matter/third_party/pigweed/repo/pw_env_setup/py/pw_env_setup/cipd_setup/pigweed.json'))
print(next(t for p in d['packages'] if p['path'].startswith('gn/gn') for t in p['tags']))
")
echo "gn revision pinned by pigweed: $GN_REV"
mkdir -p "$NCS_DIR/tools/bin"
TMP=$(mktemp -d)
curl -sL -o "$TMP/gn.zip" \
	"https://chrome-infra-packages.appspot.com/dl/gn/gn/mac-arm64/+/$GN_REV"
unzip -o -q "$TMP/gn.zip" -d "$TMP"
install -m 0755 "$TMP/gn" "$NCS_DIR/tools/bin/gn"
rm -rf "$TMP"
"$NCS_DIR/tools/bin/gn" --version

cat <<'EOF'

=== Done ===

firmware/ already holds the assembled project (the nrf/samples/matter/light_switch
base plus our own modules). Do NOT copy the sample over it again - that would
overwrite CMakeLists.txt, Kconfig.sysbuild and the hooks in src/app_task.cpp.

Build:
  ./scripts/build.sh holyiot_25015     # or holyiot_25008 / nrf54l15dk

EOF
