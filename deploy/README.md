# deploy/ — everything on the Raspberry Pi

The goal: once installed, the Pi does everything by itself. You can take your
laptop out of the house and the system keeps running.

## What runs on the Pi and what does not

| | Where | Why |
|---|---|---|
| Border router (otbr-agent + dongle) | **Pi** | has to be up permanently; it is the bridge to Thread |
| matter-server | **Pi** | the Matter client: commands, reads, subscriptions, commissioning |
| chip-tool | **Pi**, but **not running** | only for firmware OTA. Leave it disabled — it burns a full CPU core while idle |
| The panel | **Pi** | you reach it from a browser, on any device in the house |
| The schedule | **Pi** | the panel service keeps local time and writes `OnLevel` + color temperature into the bulbs on every slot change |
| The OTA server | **Pi** | started only when you push an update |
| **Firmware builds** | Mac (recommended) | see below |

The switch drives the bulb directly, through the binding — **it works with the Pi
powered down**. The Pi has to be up for administration, for the schedule, for
OTA, and for Apple Home to reach the bulbs.

## Why firmware builds stay on the Mac

Not an architecture limitation — nRF Connect SDK runs on arm64 Linux too. It is a
question of resources:

- NCS + the Zephyr SDK is ~7 GB on the card
- Compiling the Matter library takes minutes on an M-series Mac; on a Pi 4 it can
  mean an hour or more
- The linker needs RAM; on a 2 GB Pi 4 it struggles

The Pi **does not need** a toolchain to distribute an update — all it needs is the
`matter.ota` file. Build on the Mac, copy the artifact:

```bash
scp build-holyiot_25015/matter.ota pi@smarthome.local:/opt/smarthome/firmware-images/
```

If you want to build on the Pi anyway, you can — but run `./scripts/bootstrap.sh`
there and bring patience.

## Installation

### Which board

| | Minimum | Why |
|---|---|---|
| Architecture | ARMv8 (Pi 3 or newer) | the Matter tools require 64-bit |
| RAM | **1 GB** | below that, matter-server + the panel + otbr do not fit at once. matter-server holds ~150 MB: it keeps every attribute of every node subscribed, which is what makes reads free |
| Ethernet | recommended | the border router is more stable on a cable |
| USB | 2 ports | one is permanently taken by the radio |

Checked in practice:

- **Pi 3 A+** — 512 MB, a single USB port, no Ethernet. Very tight: matter-server
  alone is about 150 MB.
- **Pi 3 B+** — 1 GB, Ethernet, 4× USB. Enough, with the binaries built on the Mac.
- **Pi 4 / 5, 2 GB+** — comfortable, and you can build on it directly.

Processor speed does not matter: at runtime everything here is light. RAM and
ports do.

### Which operating system

**Raspberry Pi OS Lite, 64-bit** (Bookworm). Two non-negotiables:

- **64-bit.** Check after installing with `uname -m` — it has to say `aarch64`. On
  `armv7l` the Matter tools do not build reasonably.
- **Lite**, no desktop. This is a server; it has no monitor.

In Raspberry Pi Imager, under advanced settings, set the hostname, user, SSH and
network up front — otherwise you need a monitor and a keyboard on first boot.

**What to put it on:** an SSD over USB3 beats an SD card. The build is I/O-heavy,
and this is where the fabric's CA key ends up — and SD cards die. If you stay on a
card, use at least 32 GB (the Matter sources plus the build need ~10 GB).

If a Pi 4 will not boot from USB, its EEPROM needs updating: boot from a card once
and run `sudo rpi-eeprom-update -a`.

**Network:** Ethernet if you can. The border router bridges LAN and Thread and is
more stable on a cable than on Wi-Fi.

### Step 0 — preparing the Pi (can be done now)

Does not depend on the programmer or the radio. Run **on the Pi**:

```bash
./deploy/prepare-pi.sh
```

It checks the architecture (must be 64-bit), the RAM, the free space, and whether
you are on an SD card; installs Matter's build dependencies; enables IPv6
forwarding (without it nothing in the house reaches the bulbs); and prepares the
Python environment.

### The rest

```bash
# on the Pi
sudo mkdir -p /opt/smarthome && sudo chown pi:pi /opt/smarthome
rsync -a --exclude build-'*' --exclude .git ./ pi@smarthome.local:/opt/smarthome/

# the Python environment for the panel
python3 -m venv /opt/smarthome/.venv
/opt/smarthome/.venv/bin/pip install websockets segno

# the Matter client. A separate venv on purpose: it pulls in the whole CHIP
# stack as a wheel, and you do not want that in the panel's environment.
python3 -m venv /opt/smarthome/.venv-matter
/opt/smarthome/.venv-matter/bin/pip install python-matter-server

# the attestation root certificates, so commercial bulbs can be commissioned
./deploy/paa-certs.sh

# the services
sudo cp deploy/smarthome-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smarthome-matter smarthome-panel
```

**`/data` has to exist and be writable.** The CHIP stack hardcodes
`/data/chip_factory.ini` — it is built to run in a container, where `/data` is a
mount. On bare metal it dies at startup with `CHIP Error 0x000000AD: Open file
failed` and a traceback naming neither the path nor the reason.
`smarthome-matter.service` creates it in `ExecStartPre`.

**Only for firmware OTA**, build chip-tool as well — it is not needed to run the
house, and should stay disabled:

```bash
# Clones the Matter sources (~2-3 GB) and builds them. On a Pi 4 this takes hours.
# Run it under tmux/screen so you do not lose it if the SSH session drops.
cd /opt/smarthome && ./ota/setup.sh
sudo cp deploy/smarthome-chiptool.logrotate /etc/logrotate.d/smarthome
```

If your user is not `pi`, change `User=` / `Group=` in **every** `.service` file,
including the `ExecStartPre` in `smarthome-matter.service`. systemd fails with
`status=217/USER` when the account does not exist, and the message does not say
which unit is at fault — it just restart-loops.

The panel is then at `http://smarthome.local:8080`.

### Attestation certificates

Only needed for **chip-tool**, so only if you build it for OTA. matter-server
fetches the production attestation roots itself at startup — which is the reason
it takes about thirteen seconds to come up on a Pi 3 B+, and why commissioning an
IKEA bulb through it needs no certificate work at all.

`./deploy/paa-certs.sh` copies the production Product Attestation Authority (PAA)
root certificates out of the Matter SDK
(`credentials/production/paa-root-certs`) into
`/opt/smarthome/ota/paa-root-certs`, which is what
`smarthome-chiptool.service` passes as `--paa-trust-store-path`. Only the `.der`
files are copied; the `.pem` files next to them are the same certificates in
another format and would just bloat the directory.

Without them chip-tool only holds the TEST roots, so a commercial device fails at
the `AttestationVerification` step with `CHIP Error 0x000000AC: Internal error` —
*after* BLE has connected and the certificates have been read, which makes it look
like a network problem. It is not. Our own switch commissions fine without them,
because it has test credentials compiled into the firmware, which makes the
failure even more confusing: the switch joins on the first try and the bulb never
does.

The certificates come from the Matter SDK and are public. They are not secrets and
have no business in the fabric backup.

## If the Pi is short on RAM (Pi 3, Zero 2)

A Pi 3 B+ **runs** all of this without trouble. Measured idle, everything up:

| | CPU | RSS |
|---|---|---|
| matter-server | 0.2% | 153 MB |
| the panel | 0.4% | 34 MB |
| otbr-agent | 0.2% | small |

leaving about 640 MB free. What it cannot do with 1 GB is **build** the Matter
tools: linking alone needs over 1 GB.

The way out: build on the Mac, inside a Debian arm64 container.

```bash
./deploy/build-tools-docker.sh
scp ota/tools-linux-arm64/* pi@smarthome.local:/opt/smarthome/ota/tools/
```

On an Apple Silicon Mac, `linux/arm64` containers run **natively**, without
emulation. The `debian:bookworm` image has the same base and the same glibc (2.36)
as Raspberry Pi OS Bookworm 64-bit, so the binaries run directly.

Check on the Pi that it starts: `chip-tool --version`.

The Pi 3 B+ officially supports 64-bit Raspberry Pi OS — the processor is ARMv8.
Only the default image is 32-bit, which is why the 64-bit one has to be chosen
explicitly.

## Three traps when building the Matter tools

They apply wherever you build — Mac, container, Pi. All three are already handled
in `ota/setup.sh` and `deploy/build-tools-docker.sh`, but they are worth knowing
when something breaks.

**CIPD no longer allows anonymous access** to `fuchsia/third_party/zap`. Matter's
bootstrap attempts an interactive login and dies with
`interactive login flow requires the stdout to be attached to a terminal`.
Workaround: `PW_CONFIG_FILE=scripts/setup/environment_no_cipd.json`.

**Without CIPD, `gn` does not arrive either.** It is fetched separately, at **the
revision pigweed pins** (read from
`third_party/pigweed/repo/.../cipd_setup/pigweed.json`). With `latest` it fails on
`Duplicate output file` in `pw_chrono`.

**ZAP is still required**, even for host tools: chip-tool generates its data model
at build time. Without it:
`FAILED TO EXECUTE ZAP GENERATION: No such file or directory - "zap-cli"`.
It is run from source with node (`ZAP_DEVELOPMENT_PATH`), at the version pinned in
`scripts/setup/zap.json`. Do not use the release binaries: the
`zap-mac-arm64.zip` archive contains an **x86** `zap-cli`.

## About building the tools on the Pi

`ota/setup.sh` works without NCS: if it does not find the Matter sources, it
clones them itself from the **Nordic fork** (`nrfconnect/sdk-connectedhomeip`, tag
`v3.0.0`), not from upstream project-chip. The same revision as the firmware, so
no surprises in protocol or in command syntax.

Binaries from the Mac **cannot** be copied to the Pi — they are darwin/arm64, the
Pi is linux/arm64. The tools have to be built there.

The script detects the available RAM and caps itself at 2 parallel jobs below
6 GB. The reason: one link job takes over 1 GB, and with the default parallelism a
Pi 4 runs out of memory and the process is killed by the OOM killer — usually
right at the end, after hours of compiling.

With 4 GB it works, only slower. With 8 GB nothing is capped.

If you can, put the system on an SSD over USB3 rather than an SD card: the build is
I/O-heavy and wears the card out.

## Border router

Installing OTBR is not part of these scripts — follow the
[official guide](https://openthread.io/guides/border-router/build). In short: an
nRF52840 radio flashed with RCP firmware, then `ot-br-posix` with
`INFRA_IF_NAME=eth0` or `wlan0`.

The RCP firmware is built with:

```bash
./scripts/build-rcp.sh                 # nRF52840 Dongle
./scripts/build-rcp.sh particle_xenon  # Particle Xenon
```

**A Particle Xenon works just as well as the dongle** — it is also an nRF52840.
Verified: it compiles, 16% flash. The difference is how you flash it:

| | Flashing | Bootloader |
|---|---|---|
| nRF52840 Dongle | button + nRF Connect Programmer | kept, USB DFU |
| Particle Xenon | **SWD only** | **overwritten** — the board is no longer a Particle |

The Zephyr image goes to `0x0`, so on a Xenon it replaces the Particle bootloader.
That decision is irreversible without a reflash.

Once it works, take the dataset and store it on our side:

```bash
sudo ot-ctl dataset active -x
./scripts/commission.sh dataset <hex>
```

## Verification

```bash
systemctl status smarthome-matter smarthome-panel
journalctl -u smarthome-panel -f
```

A good health check: from the browser, `Identify` a bulb. If it blinks, the whole
chain works — panel, matter-server, border router, Thread.

## Worth knowing

**chip-tool's log grows without bound** — hence the logrotate file. It uses
`copytruncate`, because chip-tool holds the descriptor open and does not react to
SIGHUP.

**`PANEL_BYPASS_ATTESTATION=1` is set in `smarthome-panel.service`.** It is what
makes IKEA bulbs commissionable today: the PAA root set shipped with the Matter
SDK has 40 authorities and does not include IKEA (vendor `0x117C`), so
commissioning otherwise fails with `Attestation Information: err 101` =
`kPaaNotFound`. What you give up is the defense against a counterfeit device —
with it on, anything that answers gets commissioned. The panel writes a warning to
the console on every commissioning so this does not get quietly forgotten. Once
you have the real certificates, drop the line and install them with
`./deploy/paa-certs.sh`.

**`ota/state/` lives on the Pi's card.** That is where the fabric's CA key is. SD
cards die. Run `./scripts/commission.sh backup` and keep the archive elsewhere —
otherwise a burnt card means a physical factory reset of every bulb.

## Bringing up the border router

`./deploy/prepare-pi.sh` only does the steps that do not depend on the radio. The
border router comes after you have the RCP flashed and plugged in:

```bash
./deploy/setup-otbr.sh eth0
```

The script checks, in order, the radio, the interface facing the house, IPv6
forwarding, and then builds and starts `otbr-agent`. It stops at the first thing
that is missing, with a concrete message — it does not install half of it and
leave you guessing.

Two things it corrects relative to the official instructions:

- **The radio's port.** `script/setup` writes a default port that is usually not
  yours. The script replaces it with the detected one, and refuses to guess if
  there are several serial ports.
- **NetworkManager.** From Raspberry Pi OS Bookworm on it is active, and OTBR's
  setup behaves differently depending on that; with the wrong value it fights the
  network manager and the Thread interface ends up without an address.

The interface matters: that is where the border router sends its IPv6
advertisements and where Apple Home reaches the bulbs from. Wi-Fi works, but it
depends on multicast, which many access points filter or delay — the script warns
you and gives you time to reconsider.

### Wi-Fi off, cable only

The Pi runs on `eth0`, with Wi-Fi off. That way there are no longer two default
routes to pick the wrong one from.

```bash
sudo nmcli radio wifi off      # undone with "wifi on"
```

**Order matters.** If you turn Wi-Fi off from a session that is running over
Wi-Fi, you cut yourself off. Plug the cable in, check that `eth0` has an address
and that the default route is on it, connect over the wired address, and only then
turn Wi-Fi off.

The state is not kept by NetworkManager (`NetworkManager.state` does not even exist
on Raspberry Pi OS) but by **systemd-rfkill**, which saves the block in
`/var/lib/systemd/rfkill/` and restores it at boot. That is where to look if you
ever wonder whether it really persists.

The `netplan-wlan0-*` profile keeps autoconnect enabled. That is deliberate: if you
unblock the radio, it reconnects on its own — a way back in if the cable ever
fails.


## The Thread radio watchdog

`otbr-agent` ships as an LSB init script, and the unit systemd generates from it
says `Restart=no`, `GuessMainPID=no`, `RemainAfterExit=yes`. systemd therefore
has no idea whether the daemon is alive. When the radio co-processor stopped
answering, otbr-agent died and the unit went on reporting `active (exited)` —
from nine days earlier — while every Matter node in the house failed. Nothing
was going to notice, because nothing was watching.

Two different faults hide behind "the radio is gone", and only one of them is
fixed by restarting the service:

| what broke | what fixes it |
|---|---|
| the daemon died | restarting `otbr-agent` |
| the RCP wedged | nothing but a power cycle. The dongle still enumerates and still has a `/dev/ttyACM` node, but will not speak spinel, and every restart ends at `Init() at spinel_driver.cpp:87: Failure`. Over USB, a power cycle means unbind then bind. |

So the watchdog **escalates rather than retrying**: restart first, reset the
radio only if a restart was not enough. A USB reset drops the network and makes
every node re-attach, so it is not the first thing tried.

It resolves which USB device to reset from the RadioURL in
`/etc/default/otbr-agent`, never by hunting for a vendor id — the switches in
this project are Nordic parts too, and one plugged in for flashing would match
`1915:0000` just as well as the radio does. The resolved id is cached, because a
dongle that has fallen off the bus has no tty left to resolve it from, which is
exactly when it needs resetting.

Install:

```bash
sudo install -m755 deploy/thread-watchdog.sh /opt/smarthome/deploy/thread-watchdog.sh
sudo install -m644 deploy/smarthome-thread-watchdog.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smarthome-thread-watchdog.timer
```

Check it without changing anything, and suspend it while you work on the radio:

```bash
sudo /opt/smarthome/deploy/thread-watchdog.sh --check
sudo touch /run/smarthome-watchdog.pause      # cleared by a reboot
```

Both paths are verified on the real radio: stopping `otbr-agent` is counted once
(`1/2 - waiting`), acted on at the threshold and recovered by a restart; with a
restart already spent, the next check power-cycles the dongle on USB and the
border router comes back as `router` with all three nodes answering.
