# Notes

Detail that would drown the README. Nothing here is required reading — it is
what you look up when a specific thing surprises you.

- [Battery and the Thread role](#battery-and-the-thread-role)
- [Brightness is not linear](#brightness-is-not-linear)
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

## Brightness is not linear

The Matter LevelControl level is 1..254 and is proportional to **emitted light**.
The eye is not linear: double the light and you see a much smaller increase. On a
perceptual scale (L\* from CIE Lab):

| Matter level | How bright it looks |
|---|---|
| 254 | 100% |
| 127 | **76%** |
| 60 | 55% |
| 47 | 50% |
| 10 | 23% |

So the whole useful evening range is crammed between 1 and 40, while the top half
of the scale is nearly indistinguishable. The schedule editor in the panel uses a
**perceptual vertical axis** for this reason: half the height really does look
like half the light, and it lands around level 47.

This assumes the bulb maps level linearly to light. Some bulbs apply a curve of
their own and there is no way to read it, so treat the table as a starting point
and finish by eye.

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
| holyiot_25015 | 87.5% (621 KB / **710 KB slot**) | 66.5% (170 KB / 256 KB) |
| holyiot_25008 | 87.1% (619 KB / **710 KB slot**) | 66.5% (170 KB / 256 KB) |
| nrf54l15dk | 54.4% (795 KB / 1426 KB) | 67.5% (256 KB) |

On the Holyiot modules FLASH is measured against **one image slot**, not the whole
RRAM: OTA needs two. About 89 KB of headroom is left.

Logging is compiled out on Holyiot (`CONFIG_LOG=n`) — the 83 KB that saves is
exactly what makes two slots fit. The module does have a UART on P1.04/P1.05, so
a console can be brought out on wires, but re-enabling `CONFIG_LOG` means OTA no
longer fits. For development, `nrf54l15dk` is more convenient.

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
