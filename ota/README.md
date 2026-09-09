# ota/ — firmware updates without wires

```bash
./ota/publish.sh
```

That bumps the version, builds the image signed with our key, and puts it where
matter-server looks for updates on the Pi. Then, in the panel, open a switch and
press **update** — one switch at a time. Each takes about eight minutes: the
image is fetched in small pieces by a device that sleeps between them.

Why one at a time: a switch that is asleep with a flat cell does not hold up the
others, and more than three transfers at once fail at the announce with
`Error while announcing OTA Provider to node`.

## How it works

matter-server has an update source of its own: a directory of descriptors,
`/opt/smarthome/updates` on the Pi, one JSON file per image, read **at startup
only**. `publish.sh` writes the descriptor next to the image and restarts the
service. The panel asks matter-server what is on offer for each device, shows
it when it is newer than what the device reports, and starts the update on
request — then watches the device's own `UpdateState` rather than trusting the
call to return, and reads the version back at the end.

The device decides whether to take an image: the version has to be **higher**
than what it runs, and VID/PID have to match (`0xFFF1`/`0x8004`, the test
values, set in the firmware and in `config.sh`). If either is off, nothing
happens and nothing says so — which is why `publish.sh` reads the version back
out of the signed image's own header before it publishes anything.

## The first time

```bash
./ota/setup.sh                      # the signing key (the Matter tools it also
                                    # builds are not needed for updates any more)
./scripts/build.sh holyiot_25008
./scripts/flash.sh holyiot_25008    # over SWD - once, to install the bootloader
```

After that flash you commission the module from the panel, and from then on you
use `publish.sh`.

## Files

| | |
|---|---|
| `publish.sh` | **the everyday script** |
| `setup.sh` | once: generates the key. Also builds chip-tool and the OTA provider app, which updates no longer use |
| `config.sh` | VID/PID, board, paths. Edit here |
| `keys/` | the MCUboot signing key (in `.gitignore`) |
| `state/` | on the Pi: matter-server's fabric, in `state/matter-server/`. Back it up — see the main README |
| `tools/` | the Matter binaries built by `setup.sh` |

## What the partition map looks like

Fixed in `firmware/pm_static_holyiot_25008_nrf54l15_cpuapp.yml` and **not changing
again**:

```
0x000000  mcuboot              56 KB
0x00E000  mcuboot_pad           2 KB  ┐ active slot
0x00E800  app                 710 KB  ┘  712 KB   (~613 KB used, 86%)
0x0C0000  mcuboot_secondary   712 KB    the slot the update downloads into
0x172000  factory_data          4 KB    reserved
0x173000  settings_storage     40 KB    Matter fabric, ACL, binding
0x17D000  = 1524 KB
```

`settings_storage` sits at the tail on purpose. If it moved, the module would
lose its commissioning and would have to be re-associated with the bulbs. When
the application grows, the slots shrink — **not** the tail.

The bootloader has to see the same 1524 KB the application does —
`firmware/sysbuild/mcuboot/boards/holyiot_25008_nrf54l15_cpuapp.overlay` — or
it refuses to swap in an image that lands past what it thinks is the end of
flash. That, and `settings_storage` being where it is, are the two things that
made updates transfer perfectly and never apply.

## Security

Images are signed with `keys/mcuboot-signing.pem`. The bootloader rejects any
image signed with anything else — including one signed with the key NCS ships
for its samples, which is public. `scripts/build.sh` passes ours; a bare `west
build` does not, and the device will not take the result.

**Back the key up outside the repo.** If you lose it after flashing the modules,
you can no longer ship updates — the only way out is a reflash over SWD.

The transfer is authenticated by the Matter fabric: only a node on it can be
served. Nothing is exposed in the open.

**Not implemented:** anti-rollback. Anyone with fabric access can push an older,
validly signed version. For a home network that is acceptable; if you want the
protection, enable MCUboot's security counters (which requires writing to OTP —
irreversible, so do it deliberately).

## If something does not work

| Symptom | Likely cause |
|---|---|
| the panel shows no update | the version did not increase; or matter-server was not restarted after publishing; or the device has not reported its version since the restart — wait a minute |
| `Error while announcing OTA Provider to node` | too many transfers at once, or the device is asleep — try again, one at a time |
| the transfer reaches 99% and the version does not change | the bootloader did not apply it: the image is signed with a different key, or the bootloader's flash size is wrong (see above) |
| the device drops off during the transfer | usually the cell — a switch at 2.7 V fails under the radio load of a download |
