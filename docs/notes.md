# Notes

Detail that would drown the README. Nothing here is required reading — it is
what you look up when a specific thing surprises you.

- [Battery and the Thread role](#battery-and-the-thread-role)
- [Brightness, and what a level actually means](#brightness-and-what-a-level-actually-means)
- [The status LED](#the-status-led)
- [The accelerometer](#the-accelerometer)
- [Flashing](#flashing)
- [Build sizes](#build-sizes)
- [Board definitions](#board-definitions)
- [Toolchain requirements](#toolchain-requirements)
- [A bulb quirk](#a-bulb-quirk)

## Battery and the Thread role

The switch is a **Sleepy End Device**. It polls its Thread parent periodically,
and that poll *is* the keep-alive holding the attachment — it happens whether or
not you press anything, so there is no "disconnect on inactivity", not even
after weeks. Commissioning lives in `settings_storage` and survives a reboot and
a battery change.

A poll interval does **not** delay the commands the switch sends: it wakes on the
button interrupt and transmits immediately. The interval governs *inbound*
traffic — commands to us, attribute writes, `AnnounceOTAProvider`.

| Thread role | Button → bulb | Draw | Power |
|---|---|---|---|
| Sleepy End Device (default) | tens of ms | ~10 µA average | CR2032, years |
| FTD Router | 20–40 ms | ~5–8 mA continuous | USB / mains |

Matter has two classes of intermittently connected device:

| | Idle poll | For what |
|---|---|---|
| SIT | max **15 s** (spec 9.16.1.5) | must receive commands quickly — locks, valves |
| LIT | minutes | sends a lot, receives rarely — switches, sensors |

The SDK defaults are 1000 ms for SIT and 300000 ms for LIT. This runs **SIT at
15000 ms**, the maximum allowed: 15× fewer wakeups than the default, and anything
inbound is delayed by at most 15 s. That is why changing a switch's role from the
panel can take that long to be acknowledged.

15 s stays below `OPENTHREAD_MLE_CHILD_TIMEOUT` (240 s), so it is not clamped —
OpenThread computes the effective poll as `min(requested, child_timeout - margin)`.
Moving to LIT (`snippets/lit_icd`, 5 min) means raising the child timeout above
300 s too, or you get ~4 min instead of 5.

The CASE session to the bulb does expire — Matter evicts idle sessions — so the
first press after a long pause costs a few hundred ms while it is rebuilt.
`light_ctrl.cpp` handles that; it cannot avoid it.

### Measuring what is left in the cell

**The nRF54L's ADC cannot see its own supply.** `NRF_SAADC_AVDD` sounds like it
should, and nrfx says otherwise in one line - "internal 0.9 V analog supply
rail". The hardware agreed: the first version of `battery.cpp` reported 903 mV
off a 3 V cell, which is the reading working correctly on the wrong signal. This
SoC exposes two internal ADC inputs, AVDD and DVDD, and both are regulated rails
that hold still while the battery drains. There is no VDD input.

What does see the supply is the **power-fail comparator**. `POFCON` takes a
threshold from 1.7 to 2.8 V in 0.1 V steps and `POFSTAT` says whether VDD is
below it, so walking the thresholds down and stopping at the first one the supply
still clears brackets it to within 100 mV - no external parts, no standing
current, and it ships as a firmware update rather than a rework.

So `BatVoltage` is a **lower bound**: 2700 means "at least 2.7 V", and a healthy
cell reads 2800 because that is as high as the comparator looks. The ceiling
costs nothing - a lithium coin cell sits near 3 V for most of its life and then
falls off a cliff, and the cliff is entirely inside 2.8 to 2.2 V.

One consequence worth knowing before testing: **the low end cannot be exercised
on the bench.** Anything above 2.8 V - a debug probe at 3.3 V, a fresh cell at
3.0 V - reads the top step, and every threshold reports "above". Seeing the
comparator discriminate takes a cell that has actually aged, or a supply that can
be turned down.

The cluster itself is a **dynamic endpoint**, endpoint 3, carrying the Power
Source device type, for the same reason `lock_cluster.cpp` uses one: PowerSource
belongs on the root endpoint, the root endpoint comes from ZAP, and regenerating
the data model is not worth it. Endpoint numbers are read once, at commissioning,
so they are not free to renumber later.

## Brightness, and what a level actually means

**The range is 1..254**, and the bulb says so itself — its LevelControl cluster
reports `MinLevel = 1`, `MaxLevel = 254`. In Matter 255 is not a brightness and 0
is not one either for a dimmable light.

**But level 1 switches this bulb off.** It reports `OnOff = false` and goes dark,
while levels 2 and 3 stay lit — so the device advertises a minimum it does not
honour. `LEVEL_MIN = 2` exists for that, and nothing in the panel sends 1.

**The percentage shown is simply `level / 254`.** That is the perceived
brightness, because the bulb applies the perceptual curve itself.

### How that was established

Matter deliberately does not define what a level means physically. The
specification's only word on it is that the meaning *"is device dependent"*, and
the Device Library adds no photometric requirement at all — the terms *curve*,
*gamma*, *perceptual* and *lumen* appear nowhere in it. There is a recommended
logarithmic dimming curve in the spec, identical to DALI's, but it lives in the
Ballast Configuration cluster and is referenced only by Color Control's
intensity attributes. It has never applied to LevelControl.

So it has to be measured. With a phone on a fixed surface, exposure locked at
ISO 320 / f1.78 / 1/60 s, photographing a ceiling lit by the bulb, ambient
subtracted from a bulb-off frame, sRGB linearised:

| level | measured light | if level ∝ light | if level = perceived |
|---|---|---|---|
| 254 | 100% | 100% | 100% |
| **64** | **3.7%** | **25.2%** | **4.5%** |

Level 64 is the decisive point — the only one far enough above the ambient floor
to resolve. Proportional-to-light is out by a factor of seven. "The level is
already perceived brightness" predicts 4.5% and measurement gave 3.7%.

This matches published scope measurements of IKEA TRÅDFRI hardware, where PWM
duty against setting fits an exponential to within 4%, and firmware
reverse-engineering that found the curve computed in floating point inside the
bulb.

### Why it is not corrected here

Applying L\* on top of a bulb that already carries the curve corrects twice, and
the compounded scale is worse than either alone: the top fifth of the range moved
41 points of perceived lightness while the bottom fifth moved 2. That is a light
that falls off a cliff near full and does nothing at all down low.

It also puts the panel in line with everything else. Google Home documents
*"to set the brightness level to approximately 50%, use a value of 127"*; Home
Assistant, zigbee2mqtt, SmartThings and openHAB all map percent to level
affinely. No consumer controller applies a perceptual transform.

**Not every bulb is like this.** Philips anchors Hue's scale to lumens —
`min_dim_level` is documented as a percentage of maximum lumen output — and Hue's
measured floor sits where a linear map predicts. A Hue bulb may genuinely be
proportional to light, in which case L\* would be right *for it*. `perceived()`
in `panel/index.html` and `panel/server.py` is the one place a per-device curve
would go.

### The bulb does not report lumens

There is no luminous-flux attribute in Matter, and this bulb exposes none: its
endpoint 1 carries Identify, Groups, OnOff, LevelControl, Descriptor and
ColorControl, and nothing else. Ballast Configuration — the only cluster that
ever described light output — is absent, and was removed from the specification
in Matter 1.6.

The rated output is in the product name and nowhere else:

```
VendorName   IKEA of Sweden
ProductName  KAJPLATS E27 CWS globe 1055lm
PartNumber   LED2405G8
```

So absolute lumens can only be had by parsing that string, which works for these
bulbs and is not a general mechanism.

## The status LED

Dark once the switch is on the network. The background layer has one job: to say
whether the switch is waiting to be added. Everything else is an event.

| Colour | Shape | Meaning |
|---|---|---|
| blue | blink, 1.2 s period | waiting to be added — commissioning window open |
| teal | two short flashes | commissioning succeeded |
| amber `#ffa726` | 1 s pulse, full brightness | Identify — same colour and rhythm as the panel's button |
| teal | one short flash | short press: the `Toggle` went out |
| teal | two flashes | long press: full brightness + 4000 K sent |
| red / teal | two flashes | a lock-role switch locked / unlocked the others |
| nothing, not even on a press | — | the switch is locked |

The flash after a short press is deliberately **neutral**: the switch does not
know which way the bulb toggled, and pretending otherwise would be a lie.

A locked switch and a dead one look the same from the wall, since the LED is dark
while idle anyway. The panel tells them apart.

**Why there is no permanent indicator.** An LED held on under PWM needs HFCLK
running and costs ~1.5 mA in the peripheral alone, however faint — days of
battery. Even a 180 ms pulse every 10 s came to **30 µA at night, 55 µA during
the day**: 6–11 months out of a CR2032, more than the radio costs.

**Signalling colours are picked saturated, not "pretty on screen".** An interface
blue like `#4a8fff` comes out **green** on the LED even with blue at 100% duty and
green at 27%, because the blue die is several times less efficient. Pure `#0000ff`
is too weak to see. `kBlue` is `#0040ff` — about 5% green, enough to borrow light
from the efficient die, too little to drag the hue back.

When a colour looks wrong, suspect this before the pin mapping.
`CONFIG_SMARTHOME_LED_CHANNEL_TEST` lights each channel with a different number
of blinks (1 = red, 2 = green, 3 = blue) and takes the status LED offline while it
runs, which separates "which pin" from "which colour" without guessing.

**Colour calibration.** The sRGB → duty conversion is not linear: `#ffa726` means
100/39/2% per channel, not 100/65/15%. See `kSrgbToLinear` in `src/status_led.cpp`
— an amber built straight from sRGB values comes out a washed-out yellow. The dies
also differ in efficiency, hence `CONFIG_SMARTHOME_LED_TRIM_{R,G,B}`, calibrated
by eye with the module next to the screen and Identify running.

**`nordic,invert` in pinctrl is mandatory.** Without it the LEDs sit at constant
white: the pins are LOW while PWM is stopped and the LEDs are active-low. The
`pwm_nrfx` driver has a shortcut for 0% duty that parks the pin in GPIO at the
right level honouring `PWM_POLARITY_INVERTED`, but on nRF54L the pad stays under
PWM control, so the GPIO write never reaches it.

All three channels share one PWM block (`pwm20`) — four channels per instance
share a period, which is exactly right for RGB, where only duty differs. At 0% or
100% the driver stops the peripheral and parks the pin, so an LED that is off
draws no current.

## The accelerometer

Not used, and on battery that matters, so the driver is compiled in and the
sensor is powered down **explicitly**: the driver writes the ODR in
`lis2dh_init()` (`zephyr/drivers/sensor/st/lis2dh/lis2dh.c:431`) and we write
ODR = 0 immediately afterwards. Costs a few ms per boot and ~2.3 KB of flash, and
buys certainty rather than trusting the datasheet's power-up state on a chip
nobody has read a register from.

The result is kept in `sAccelPowerDownResult` so it can be read over SWD. It is
`volatile` on purpose — nothing in the firmware reads it, and without that LTO
removes it, i.e. the instrument disappears exactly when you need it. Verified on
the board: **0**.

Gestures (tap / double tap) are behind `CONFIG_SMARTHOME_ACCEL_GESTURES`, off by
default: an accelerometer listening for taps costs tens of µA, comparable to the
entire average budget of a Thread SED.

The SPI bus stays **on** deliberately — the bus driver holds `cs-gpios` as
inactive outputs. Shut it down and CS floats, and a floating CS can make an SPI
interface read noise as activity.

## Flashing

Over SWD with a Raspberry Pi Pico as the probe. Do not power the module from the
Pico and the CR2032 at the same time.

| Pico | Holyiot module |
|---|---|
| GP2 | SWCLK |
| GP3 | SWDIO |
| GND | GND |
| 3V3 | VCC (optional — or leave the battery in) |

Verified with pyocd 0.45.1:

- The target name is **`nrf54l`**, not `nrf54l15` — the long form is rejected.
- Support is built in since 0.45; no CMSIS pack needed.
- There is a dedicated `picoprobe` plugin alongside `cmsisdap`; both the Pico's
  legacy firmware and debugprobe (CMSIS-DAP) work.
- `pyocd flash` erases only the sectors it writes, so `settings_storage` survives
  a reflash and **commissioning is not lost**. `--erase chip` wipes everything.

APPROTECT is disabled explicitly (`CONFIG_NRF_APPROTECT_DISABLE=y`, LOCK unset)
so the debug port stays open. pyocd's nRF54L target has no recovery path over
CTRL-AP, so locking the port leaves you needing a J-Link plus nrfjprog.

## Build sizes

NCS v3.0.0, Zephyr SDK 0.17.4, macOS arm64:

| Board | FLASH | RAM |
|---|---|---|
| holyiot_25015 | 88.6% (629 KB / **710 KB slot**) | 66.8% (171 KB / 256 KB) |
| holyiot_25008 | 88.3% (627 KB / **710 KB slot**) | 66.8% (171 KB / 256 KB) |
| nrf54l15dk | 54.4% (795 KB / 1426 KB) | 67.5% (256 KB) |

On the Holyiot modules FLASH is measured against **one image slot**, not the whole
RRAM: OTA needs two. About 81 KB of headroom is left.

**The usable ceiling is well below the 710 KB slot, and is not yet known.**
Measured on holyiot_25008, same signing key throughout: 642076 B boots; 714880,
720048 and 722288 B are all rejected by MCUboot - `FIH_PANIC` in `main.c:611`,
PC frozen around `0x5230`, and a switch that does nothing until it is reflashed.
`imgtool` passes every one of them, because it checks `image + trailer <= slot`
against a fixed 6224-byte trailer while MCUboot's swap status area grows with
the sector count (177 here). Treat anything much above 642 KB as unproven.

Reading the coin cell costs **23 KB** of that — the PowerSource cluster and the
ADC driver, measured as 619 KB before and 642 KB after on the 25008. Worth
knowing before adding the next thing: the slot is the ceiling, and it is the
same slot OTA has to fit an image into.

Logging is compiled out on Holyiot (`CONFIG_LOG=n`) — the 83 KB that saves is
exactly what makes two slots fit. The module does have a UART on P1.04/P1.05, so
a console can be brought out on wires, but re-enabling `CONFIG_LOG` means OTA no
longer fits. For development, `nrf54l15dk` is more convenient.

## Over-the-air updates

Updates work. They did not for a long time, and there were **two** causes stacked
on top of each other - the first hid the second, which is why several plausible
hypotheses (the battery feature, the debug console, image size, the NCS confirm
guard) were each tried and each wrong. None of them was ever the problem.

**The bootloader saw a shorter flash than the application did.** `nrf54l15.dtsi`
reserves part of the RRAM for the FLPR coprocessor, leaving the application core
1428 KB. `boards/holyiot_*.overlay` takes it all back - 1524 KB - because the
partition map needs the room. MCUboot builds separately and never got the same
overlay, so it thought flash ended at `0x165000` while `mcuboot_secondary` runs
to `0x172000`, and counted the slot short:

```
W: Cannot upgrade: not a compatible amount of sectors
D: slot0 sectors: 178, slot1 sectors: 165, usable slot0 sectors: 173
```

165 x 4096 = `0xA5000`, which is exactly `0xC0000` to `0x165000` - the secondary
slot truncated at the old end of flash. Swap-using-move requires the secondary to
hold at least as many sectors as the usable primary, so every update was refused
before it began. `sysbuild/mcuboot/boards/holyiot_*.overlay` gives the bootloader
the same map; NCS does the same for its own nRF54L15 DK.

**The version is fixed when the build is configured, not when it is compiled.**
`CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION` defaults to the VERSION file through a
Kconfig default, so it is read once and cached. Bump VERSION, rebuild into the
same directory, and imgtool still signs the old number while the Matter wrapper
around it carries the new one. The device then downloads an image that announces
3.0.4 and contains 3.0.0, swaps it in correctly, and comes back on the version it
started with - which looks exactly like a failed update and is not one.

The tell is a secondary slot byte-identical to the primary. Read both headers over
SWD: the version lives at offset 20 of the MCUboot header, which sits at the start
of the slot (`0xE000` and `0xC0000`), not at the start of the application.

**So: build OTA images in a clean build directory.** `west build -d build-ota`
after `rm -rf build-ota`. An incremental build is fine for flashing over SWD,
where the version only has to be right in what you read back, and wrong for
anything that travels.

Note that `ota/update.sh` cannot be used for any of this: it drives the update
with chip-tool on a fabric that no longer exists. Updates go through
matter-server's local update descriptors, which are JSON files dropped in
`/opt/smarthome/updates` - one `modelVersion` object each, with `otaUrl` relative
to that directory (`file:///name.ota`, since only the leading slash is stripped),
`otaChecksum` as base64 sha256 and `otaChecksumType: 1`. They are read at startup
only, so matter-server has to be restarted after adding one. See
`deploy/matter-custom-clusters.py` for the shape of the surrounding machinery.

## Board definitions

`firmware/boards/holyiot/` covers **Holyiot 25008** and **25015**, started from
[uAmpHome/micro_matter_button](https://github.com/uAmpHome/micro_matter_button)
and corrected against
[the official board upstream](https://docs.zephyrproject.org/latest/boards/holyiot/holyiot_25008/doc/index.html).
When NCS carries these boards upstream, delete these and use the official ones.

The community definition declares four green LEDs and no UART; the module really
has one RGB LED and a UART. Two corrections are load-bearing:

- **`CONFIG_NRF_GRTC_START_SYSCOUNTER=y`.** On nRF54L, GRTC is shared between
  cores and its counter must be started by exactly one of them. Every official
  nRF54L board in Zephyr sets this. Without it the system clock never advances:
  `main` sits in `zms_add_empty_ate → flash_write → nrf_flash_sync_exe →
  mpsl_timeslot_request` waiting out a 25 s `FLASH_TIMEOUT_MS` per write, because
  MPSL cannot grant timeslots without a clock; the LED never comes on, because its
  delayed `k_work` never fires; and with the console off for the OTA budget and
  LTO having melted the function boundaries, there is nothing to debug with. The
  tell is `curr_tick` read over SWD: it stays 0 for ever.

  If you inherit a board definition from outside the Zephyr tree, diff it line by
  line against the official defconfig of the closest SoC.

- **`boards/holyiot_*.overlay` reclaims memory** implicitly reserved by
  `nrf54l15.dtsi` for the **FLPR** coprocessor — 68 KB SRAM and 96 KB RRAM.
  Without it RAM comes out at 89% with almost no headroom. Delete the overlays if
  you ever run code on FLPR.

The physical button is P1.13 on both modules but with a different index: `sw1` on
25008, `sw0` on 25015, where P1.09 is taken by I2C21 SCL. The remaining
`gpio-keys` entries are exposed pads, not mounted buttons.

## Toolchain requirements

- **`gn` must be exactly the revision pigweed pins**
  (`git_revision:05eed8f6252e2dd6b555e0b65192ef03e2c4a276`, version 2179). A newer
  one fails with `Duplicate output file` on `pw_chrono`. Homebrew has no `gn`
  formula; `bootstrap.sh` fetches it from CIPD.
- **Python ≤ 3.12.** `bootstrap.sh` builds the venv on `python@3.12`.
- **ZAP is required** even for host tools — the Matter tools generate their data
  model at build time.
- On nRF54L15 settings go through **ZMS, not NVS** (`CONFIG_NVS=n`).
- The board definitions target NCS v3.2.4; on v3.0 they need
  `<nordic/nrf54l15_partition.dtsi>` replaced with an inline table, and
  `chosen { zephyr,ieee802154 }` added.
- **`SB_CONFIG_MATTER_OTA` is force-propagated** over `boards/*.conf` — a
  `CONFIG_CHIP_OTA_REQUESTOR=n` there is ignored. Turn it off in
  `Kconfig.sysbuild`.

## A bulb quirk

The IKEA bulb takes roughly half a second to go dark, while turning on looks
instant. Worth writing down what it is **not**, since each of these is an easy
thing to re-investigate for nothing:

- **Not the command.** The switch sends `Toggle`, so the command is byte for byte
  identical in both directions.
- **Not a configured fade.** `OnOffTransitionTime` is 0, and the bulb answers
  `UNSUPPORTED_ATTRIBUTE` for `OnTransitionTime` and `OffTransitionTime` — it does
  not implement them, so the nulls you read are absence, not "unset".
- **Not the network.** The same off command from the Pi is confirmed in 40 ms, at
  any brightness.
- **Not a pending transition.** Unchanged after waiting 30 s following switch-on.
- **Not the radio path.** Dropping the poll interval from 15 s to 3 s made no
  difference.

What is left is the bulb's own LED driver, and one observation supports it: at
full brightness the same command turns the bulb off instantly, while at the night
level it does not — consistent with a ramp inside the driver near the bottom of
its dimming range, which nothing in Matter exposes.

Unverified, because it needs a bulb from another vendor to compare against. Until
then, treat it as a property of this bulb rather than a defect in this system.
