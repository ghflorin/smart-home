# ota/ — firmware updates without wires

The update server runs **only for the duration of the update**. Between updates
nothing is left running.

```bash
./ota/update.sh
```

> **chip-tool is no longer on the devices' fabric.** The panel moved to
> python-matter-server and chip-tool's fabric was removed from every device, so
> `update.sh` cannot reach the switch until you let it back on:
>
> 1. in the panel, open **Share** on the switch and copy the pairing code
> 2. `sudo systemctl start smarthome-chiptool`
> 3. commission chip-tool with that code, so it holds a fabric again
> 4. run `./ota/update.sh`
> 5. `sudo systemctl stop smarthome-chiptool`, and remove its fabric from the
>    device when you are done — it burns a full CPU core while merely idle
>    ([connectedhomeip#29971](https://github.com/project-chip/connectedhomeip/issues/29971))
>
> Each fabric takes a slot in the device, and the spec only requires five, so do
> not leave it holding one it is not using. Moving OTA onto matter-server is the
> obvious next step and is not done.

That is all. The script bumps the version, builds, signs, starts the provider,
tells the module to look at it, and shuts it down at the end.

## The first time

```bash
./ota/setup.sh        # the signing key + the Matter tools (30-60 min)
./scripts/build.sh holyiot_25015
# flash over SWD - ONCE, to install the bootloader
./scripts/flash.sh holyiot_25015
```

After that flash you commission the module and the bulbs
([scripts/commission.sh](../scripts/commission.sh)), and from then on you use
`update.sh`.

## Files

| | |
|---|---|
| `setup.sh` | once: generates the key, builds `chip-tool` and `chip-ota-provider-app` |
| `update.sh` | **the everyday script** |
| `config.sh` | node IDs, VID/PID, board. Edit here |
| `keys/` | the MCUboot signing key (in `.gitignore`) |
| `state/` | chip-tool's fabric credentials. **Do not delete** |
| `tools/` | the Matter binaries built by `setup.sh` |

## What the partition map looks like

Fixed in `firmware/pm_static_holyiot_*.yml` and **not changing again**:

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

## What has to be true for this to work

**You have your own Thread Border Router (OpenThread).** The provider runs on the
machine you launch `update.sh` from — normally the build machine, which is on
Wi-Fi, not on Thread — and reaches the module by IPv6 routing through the border
router. With OTBR this works; through Apple's border router it is unreliable for
a third-party host running chip-tool.

**The version increases on every update.** `update.sh` does this itself
(`BUMP=patch` by default, or `major`/`minor`/`none`). If the version does not
increase, the provider answers `updateNotAvailable` and you see no error message
at all — nothing simply happens.

**VID/PID are identical** between the firmware and `config.sh`. Same failure mode:
silent.

**Patience.** The module is a Sleepy End Device, and `AnnounceOTAProvider` is
*inbound* traffic — it can take minutes to arrive. This is the one direction in
which being a SED genuinely hurts.

## Security

Images are signed with `keys/mcuboot-signing.pem`. The bootloader rejects any
image signed with anything else.

**Back it up outside the repo.** If you lose it after flashing the modules, you
can no longer ship OTA updates — the only way out is a reflash over SWD.

The transfer is authenticated by the Matter fabric: only commissioned nodes can
talk to the provider. Nothing is exposed in the open, unlike a DFU over BLE.

**What is NOT implemented:** anti-rollback. Anyone with fabric access can push an
older, validly signed version. For a home network that is acceptable; if you want
the protection, enable MCUboot's security counters (which requires writing to OTP
— irreversible, so do it deliberately).

## What has not been tested

**Nothing in this directory has been run.** The commands and arguments are taken
from the Matter sources in NCS v3.0
(`examples/ota-provider-app/linux/README.md`,
`zzz_generated/chip-tool/.../Commands.h`), not from memory — but the full flow
needs hardware, a Thread network and real bulbs.

The firmware side **is** verified: it compiles, the partition map applies exactly
as written, and `matter.ota` is generated and signed with our key.

## If something does not work

| Symptom | Likely cause |
|---|---|
| nothing happens after the announce | the version did not increase; or the module has not woken up yet |
| `updateNotAvailable` | VID/PID do not match, or the version is lower than or equal to the running one |
| `UNSUPPORTED_ACCESS` | the ACL entry for cluster 41 on the provider is missing |
| the transfer starts and stalls | is IPv6 routed through the border router? try `ping6` to the module |
| the module does not boot after the update | image signed with a different key — reflash over SWD |
