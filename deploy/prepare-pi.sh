#!/usr/bin/env bash
#
# Prepares the Raspberry Pi. Run it ON the Pi, not on the Mac.
#
#   ./deploy/prepare-pi.sh
#
# Does only the steps that do not depend on hardware which has not arrived yet:
# system checks, dependencies, IPv6, the Python environment, the directory layout.
#
# It does not install the border router and does not build the Matter tools -
# those come separately, once you have the radio flashed. See deploy/README.md.

set -euo pipefail

SMARTHOME_DIR=${SMARTHOME_DIR:-/opt/smarthome}
fail=0

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ok    %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; }
bad()  { printf '  ERROR %s\n' "$*"; fail=1; }

say "1. System checks"

if [ "$(uname -s)" != "Linux" ]; then
	bad "this script runs on the Raspberry Pi, not on the Mac"
	exit 1
fi

ARCH=$(uname -m)
if [ "$ARCH" = "aarch64" ]; then
	ok "architecture $ARCH (64-bit)"
else
	bad "architecture $ARCH - you need the 64-bit Raspberry Pi OS."
	bad "The Matter tools will not build sensibly on 32-bit."
fi

RAM_MB=$(( $(getconf _PHYS_PAGES) * $(getconf PAGE_SIZE) / 1048576 ))
BUILD_HERE=1
if [ "$RAM_MB" -ge 3500 ]; then
	ok "RAM ${RAM_MB} MB - you can build the Matter tools right here"
elif [ "$RAM_MB" -ge 1800 ]; then
	warn "RAM ${RAM_MB} MB - a local build is possible but painful."
	warn "Easier: deploy/build-tools-docker.sh on the Mac, then copy the binaries."
else
	BUILD_HERE=0
	warn "RAM ${RAM_MB} MB - do NOT build the Matter tools here, the link will not fit."
	warn "Run on the Mac: ./deploy/build-tools-docker.sh"
	warn "and copy the binaries to $SMARTHOME_DIR/ota/tools/"
	warn "The Pi RUNS them without trouble - at runtime they use under 200 MB."
fi

# 10 GB only if the build happens here (Matter sources + build tree). If the
# binaries arrive prebuilt from the Mac, 3 GB is enough.
NEED_GB=$([ "$BUILD_HERE" -eq 1 ] && echo 10 || echo 3)
FREE_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
if [ "${FREE_GB:-0}" -ge "$NEED_GB" ]; then
	ok "${FREE_GB} GB free (${NEED_GB} GB needed)"
else
	bad "only ${FREE_GB} GB free, ${NEED_GB} GB needed"
fi

# Memory reserved for the GPU is wasted on a headless system.
GPU_MB=$(vcgencmd get_mem gpu 2>/dev/null | tr -dc '0-9')
if [ -n "$GPU_MB" ] && [ "$GPU_MB" -gt 16 ]; then
	warn "the GPU holds ${GPU_MB} MB. On an SSH-only system, put gpu_mem=16 in"
	warn "/boot/firmware/config.txt and reboot - that gets back ~$((GPU_MB - 16)) MB."
elif [ -n "$GPU_MB" ]; then
	ok "GPU ${GPU_MB} MB (minimum)"
fi

# SD cards die, and the fabric's CA key ends up on this one.
ROOT_DEV=$(findmnt -no SOURCE / | sed 's/p\?[0-9]*$//')
case "$ROOT_DEV" in
	*mmcblk*) warn "the system is on an SD card. Works, but an SSD over USB3 is safer and faster." ;;
	*)        ok "the system is not on an SD card ($ROOT_DEV)" ;;
esac

say "2. Dependencies"
# Exactly the list from Matter's docs/guides/BUILDING.md, minus default-jre
# (that one is only needed for test tooling, not for chip-tool).
sudo apt-get update
sudo apt-get install -y \
	git gcc g++ pkg-config libssl-dev libdbus-1-dev \
	libglib2.0-dev libavahi-client-dev ninja-build python3-venv python3-dev \
	python3-pip unzip libgirepository1.0-dev libcairo2-dev libreadline-dev \
	tmux logrotate
ok "packages installed"

say "3. IPv6"
# On Debian 13, /usr/sbin is no longer on an ordinary user's PATH, so a bare
# "sysctl" is not found and the checks below silently come out wrong.
SYSCTL=$(command -v sysctl || echo /usr/sbin/sysctl)
# The border router routes IPv6 between the LAN and the Thread network. Without
# forwarding, nothing in the house reaches the bulbs - not chip-tool, not Apple
# Home.
if [ "$("$SYSCTL" -n net.ipv6.conf.all.forwarding 2>/dev/null)" = "1" ]; then
	ok "IPv6 forwarding enabled"
else
	warn "IPv6 forwarding disabled - enabling it permanently"
	echo 'net.ipv6.conf.all.forwarding=1' | sudo tee /etc/sysctl.d/99-smarthome-ipv6.conf >/dev/null
	sudo "$SYSCTL" -p /etc/sysctl.d/99-smarthome-ipv6.conf >/dev/null
	ok "enabled"
fi

if [ "$("$SYSCTL" -n net.ipv6.conf.all.disable_ipv6 2>/dev/null)" = "1" ]; then
	bad "IPv6 is disabled system-wide. Nothing will work without it."
fi

say "4. Layout"
sudo mkdir -p "$SMARTHOME_DIR"
sudo chown "$USER:$USER" "$SMARTHOME_DIR"
ok "$SMARTHOME_DIR"

if [ ! -d "$SMARTHOME_DIR/.venv" ]; then
	python3 -m venv "$SMARTHOME_DIR/.venv"
fi
"$SMARTHOME_DIR/.venv/bin/pip" install -q --upgrade pip
"$SMARTHOME_DIR/.venv/bin/pip" install -q websockets segno
ok "Python environment with websockets and segno"

say "Done"
if [ "$fail" -ne 0 ]; then
	echo "There are errors above - fix them before going on."
	exit 1
fi

cat <<EOF

Next steps, in order:

  1. Copy the project over from the Mac:
       rsync -a --exclude 'build-*' <project>/ $USER@$(hostname):$SMARTHOME_DIR/

  2. The Matter tools (chip-tool, ota-provider):
       - with 4 GB+ RAM, right here:  cd $SMARTHOME_DIR && ./ota/setup.sh   (in tmux, hours)
       - with less, on the Mac:       ./deploy/build-tools-docker.sh
                                      then scp to $SMARTHOME_DIR/ota/tools/

  3. Once the programmer arrives: flash the radio, install the border router,
     then the services. See deploy/README.md.
EOF
