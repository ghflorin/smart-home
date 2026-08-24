# smarthome — a Matter switch on nRF54L15 driving IKEA bulbs directly

Firmware for the Holyiot nRF54L15 module (BLE beacon + 3-axis accelerometer),
reflashed as a **Matter switch accessory** that drives IKEA Matter bulbs
**directly over Thread**, with no hub in the command path.

## Why this is not a BLE project

IKEA Matter bulbs do not accept commands over BLE. BLE exists in them only as a
commissioning transport; once a bulb is on the network, control is exclusively
**Matter over Thread (802.15.4)**.

The nRF54L15 has a multiprotocol radio, so it can do both BLE and 802.15.4 — but
this firmware is a Matter/OpenThread application, not a BLE one. BLE is used
exactly once, when the module is commissioned.

## How "direct, no latency" works

Not by running a controller on the module: that would need certificates, CASE
and mDNS, and it does not fit in 1.5MB. It works through the standard Matter
**binding** mechanism:

```
[button/accel] -> nRF54L15 module ---- Thread unicast ----> IKEA bulb
   the hub / border router is NOT in the command path
```

1. The module is commissioned as a Matter accessory (OnOff + LevelControl client)
2. An admin writes the **binding** table into the module → bulb node ID + endpoint
3. The bulb gets an **ACL** entry for the module's node ID
4. The module opens a CASE session straight to the bulb and sends commands

The command never passes through the border router: the switch talks to the bulb
directly. The **schedule, however, needs the Raspberry Pi**, which writes into
the bulbs what they should come on to for the current slot. With the Pi powered
down, pressing the button still turns the bulb on — at the last value it
received, not at the one for the time of day.

That was a deliberate trade. The schedule used to live in the switch and worked
with the Pi unplugged, but then the switch had to remember whether the bulb was
on, and that assumption went stale the moment you turned the light on from
somewhere else. See "Who decides brightness" below.

**Important limitation:** Apple Home, Google Home and DIRIGERA do **not** expose
binding-table or ACL editing. Steps 2–3 are done with `chip-tool` (see
`scripts/commission.sh`), so you administer the primary fabric yourself. You can
add Apple/Google later as secondary admins through multi-admin.

## What the firmware does

| Requirement | Where | How |
|---|---|---|
| on/off straight to the bulb | `src/light_ctrl.cpp` | `OnOff::Toggle`, unicast through the binding table |
| brightness by time of day | `panel/server.py` | the Pi writes `OnLevel` and color temperature into the bulbs on every slot change |
| state after a power cut | `src/automation.cpp` | writes `StartUpOnOff` + `StartUpCurrentLevel` into the bulb |
| full brightness on demand | `src/automation.cpp` | long press = 254 + 4000K, once |
| editable schedule | `panel/` | graphical editor, stored on the Pi in `ota/state/schedule.json` |
| the correct time | Raspberry Pi | the switch has no clock; the Pi knows local time, with the right time zone and DST |
| firmware updates without wires | [`ota/`](ota/) | Matter OTA, provider started only on demand |
| which bulb is which, what a switch drives | [`panel/`](panel/) | Identify + binding table, local panel on the Pi |
| what was sent and what answered | `panel/server.py` | the panel console; the switch has no console of its own |
| locking the switches | `src/lock_cluster.cpp` | our own cluster on dynamic endpoint 2 |
| the status LED says what is going on | `src/status_led.cpp` | blinks only while waiting to be commissioned; dark otherwise |
| everything on the Raspberry Pi, no laptop | [`deploy/`](deploy/) | systemd units for the panel and chip-tool |

### Details that matter

**`MoveToLevelWithOnOff`, not `On` + `MoveToLevel`.** With two separate commands
the bulb first comes on at the old brightness and only then moves to the new one
— it reads as a flash. The combined command removes that.

**Startup state is written rarely, not on every press.** `StartUpOnOff` and
`StartUpCurrentLevel` are persistent attributes **in the bulb**. We rewrite them
once a day as a defense against an IKEA OTA or another admin changing them — not
on every command, which would wear out the bulb's flash for nothing.

**Writing them requires `Manage` privilege in the ACL**, not `Operate`. With
`Operate` the on/off commands work, but the startup-state write fails with
`UNSUPPORTED_ACCESS`. This is the most common trap here.

**Matter level is 1..254, not percent**, and perception is logarithmic: for
"half the light" you want ~70, not 127. The default schedule in `panel/server.py`
(`SCHED_DEFAULT`) is already calibrated perceptually, and the editor's vertical
axis is in L\* from CIE Lab, not linear in level.

### Who decides brightness

**The Raspberry Pi, not the switch.** On every slot change — seven times a day —
the Pi writes two persistent attributes into each bulb:

| attribute | what it does |
|---|---|
| `OnLevel` (LevelControl) | the level the bulb comes on to in response to `On` |
| color temperature | written with `ExecuteIfOff`, so it applies while the bulb is off |

The switch sends nothing but `Toggle`. The bulb already knows the rest.

Verified on hardware before building on it: with the bulb **off**, we write
454 mireds and `OnLevel` 200; on `Toggle` it comes on at exactly 454 and 200.

**Why this way round.** The switch used to hold the schedule and send the level
and the color temperature on every press. Three defects:

- it kept its state in a local variable, which went stale the moment you turned
  the bulb on from the panel or from another ecosystem; after that a press
  looked like it did nothing, and you had to press twice
- the bulb ramped from the old brightness to the new one, so you saw a flash
- a battery-powered device sent three commands per press

**Cadence matters.** The write happens only on a slot change, not periodically.
`OnLevel` is persistent in the IKEA bulb; writing it every few minutes would
mean over 100,000 writes a year into its memory, for an identical result.

**The switch no longer has a clock.** The Pi knows local time, with the correct
time zone and the daylight-saving changeover — things the module used to take
from a compile-time constant.

### A bulb quirk we could not remove

The IKEA bulb takes roughly half a second to go dark, while turning on looks
instant. It is worth writing down what this is NOT, because every one of these
was measured and each is an easy thing to re-investigate for nothing:

- **Not the command.** Since the switch sends `Toggle`, the command is byte for
  byte identical in both directions. The asymmetry cannot be in what we send.
- **Not a configured fade.** `OnOffTransitionTime` is 0, and the bulb answers
  `UNSUPPORTED_ATTRIBUTE` for `OnTransitionTime` and `OffTransitionTime` - it
  does not implement them at all, so the nulls you read are absence, not
  "unset".
- **Not the network.** The same off command sent from the Pi is confirmed in
  40 ms, at any brightness.
- **Not a pending transition.** The delay is unchanged after waiting 30 s
  following the switch-on.
- **Not the radio path.** Dropping the switch poll interval from 15 s to 3 s -
  a five-fold change in how long a lost frame would wait for its retry - made no
  difference to how it feels.

What is left is the bulb's own LED driver, and one observation supports it: at
full brightness the same command turns the bulb off instantly, while at the
night level it does not. That is consistent with a ramp inside the driver near
the bottom of its dimming range, which nothing in Matter exposes.

Unverified, because it needs a bulb from a different vendor to compare against.
Until then, treat it as a property of this bulb rather than a defect in this
system.

### Idle power

The module is a Sleepy End Device: it polls its Thread parent periodically, and
that poll **is** the keep-alive that maintains the attachment. It happens
regardless of button presses, so there is no "disconnect on inactivity" — not
even after weeks. Commissioning lives in `settings_storage` and survives a
reboot and a battery change.

What does expire is the CASE session to the bulb (Matter evicts idle sessions).
The first press after a long pause re-establishes it — a few hundred ms instead
of tens. `light_ctrl.cpp` handles that case.

Matter has two classes of intermittently connected device:

| | Idle poll | For what |
|---|---|---|
| SIT | max **15 s** (spec 9.16.1.5) | must receive commands quickly — locks, valves |
| LIT | minutes | sends a lot, receives rarely — switches, sensors |

The SDK defaults are 1000 ms for SIT and 300000 ms for LIT. We run **SIT at
15000 ms**, the maximum allowed: 15 times fewer wakeups than the default, and
anything inbound (`AnnounceOTAProvider` included) is delayed by at most 15 s.

That stays below `OPENTHREAD_MLE_CHILD_TIMEOUT` (240 s), so it is not clamped —
OpenThread computes the effective poll as `min(requested, child_timeout - margin)`.
If you ever move to LIT (`snippets/lit_icd`, 5 min), the child timeout has to go
above 300 s as well, otherwise you get ~4 min instead of 5.

## Before production

Things deliberately left in a development state along the way. Work through them
before you call the system finished.

- [ ] **Replace the test cluster code.** `0xFFF1FC30` (dynamic endpoint 2) is in
      the `0xFFF1` test vendor space. Once you have an allocated vendor ID,
      change it in **all three** places that have to agree:
      `firmware/src/lock_cluster.cpp` (the cluster itself),
      `firmware/src/light_ctrl.cpp` (`kSmartHomeCluster`, the binding entries a
      lock writes through) and `panel/server.py` (`SCHED_CLUSTER`).
- [ ] **Confirm on hardware that endpoint 2 does not show up in Apple Home.** It
      carries a device type no ecosystem knows, so it should be ignored, but
      this has not yet been seen on a real phone. If it does show up, move the
      lock attributes onto endpoint 1 (which requires regenerating app-common,
      see `docs/zap.md`).
- [x] **Status LED power draw.** Fixed by deletion: the LED no longer pulses
      periodically. It blinks only while waiting to be commissioned, and answers
      briefly to a press. Otherwise it is dark.
- [ ] **Calibrate `CONFIG_SMARTHOME_LED_TRIM_*`** on the real module, otherwise
      the amber used by Identify is not the same amber as in the interface.
- [x] **Remove demo mode from the panel.** Done: `PANEL_DEMO`, every `if DEMO:`
      branch in `panel/server.py` and the "demo mode" indicator in `index.html`
      are gone. The panel can no longer answer with fabricated data, not even by
      accident.
- [ ] **Your own Matter credentials.** Right now the test ones are used,
      VID `0xFFF1` / PID `0x8004`, compiled into the firmware
      (`CONFIG_CHIP_FACTORY_DATA=n`). Acceptable for your own fabric, not for
      distribution. The `factory_data` partition is already reserved in the map.
- [ ] **Change the passcode and the discriminator.** `20202021` / `3840` are the
      sample's test values, in plain sight in `ota/config.sh`.
- [ ] **Back up `ota/keys/mcuboot-signing.pem`**, off this machine. Lose it after
      flashing and you can no longer ship OTA updates.
- [ ] **Back up `ota/state/`** (`./scripts/commission.sh backup`). Lose it and
      you can no longer administer the bulbs at all — only a physical factory
      reset is left.
- [ ] **Anti-rollback**, if you want it. Without it, anyone with fabric access
      can push a validly signed old version. It is done with the MCUboot
      security counters, which write to OTP — irreversible.
- [ ] **Automate the backup to the NAS.** `ota/state/` and `ota/keys/` are on the
      order of a few hundred KB, so a daily job that archives them and ships them
      to the NAS costs nothing. Today the backup is manual
      (`./scripts/commission.sh backup`), which means it depends on discipline.
- [ ] **Delete `docs/2019042516322180424.pdf`** — it is the wrong datasheet
      (Holyiot-18010 nRF52840), unrelated to our modules.

## The latency / battery trade

Configurable in `prj.conf`. The trade is smaller than it looks at first, and
that changes the recommendation:

| Thread role | Latency button -> bulb | Draw | Power |
|---|---|---|---|
| Sleepy End Device (current default) | tens of ms | ~10µA average | CR2032, years |
| FTD Router | 20–40ms | ~5–8mA continuous | USB / mains |

A SED's poll interval does **not** delay the commands it sends: the device wakes
on the button interrupt and transmits immediately. The poll governs *inbound*
traffic (commands to us, subscriptions). For a switch, which almost only sends,
the real penalty is small — wake up, turn the radio on, plus one extra hop
through the parent.

The cost shows up when the CASE session to the bulb has expired and has to be
rebuilt — then the first command after a long pause can take hundreds of ms.
`light_ctrl.cpp` handles that case (CASE recovery on timeout) but does not
eliminate it.

Conclusion: on battery this is perfectly usable. The router role only makes
sense if you also want low latency on *inbound* traffic and have permanent power.

## Hardware

Board definitions for **Holyiot 25008** and **Holyiot 25015** (nRF54L15),
started from [uAmpHome/micro_matter_button](https://github.com/uAmpHome/micro_matter_button)
and corrected against [the official board in Zephyr upstream](https://docs.zephyrproject.org/latest/boards/holyiot/holyiot_25008/doc/index.html)
— see `firmware/boards/holyiot/`.

The community definition declares **four green LEDs** and has no UART. In
reality the module has **one RGB LED** (three GPIOs) and a UART on P1.04/P1.05.
Corrected. When you update NCS to a version that carries these boards upstream,
delete the definitions here and use the official ones.

| | Pin | Notes |
|---|---|---|
| RGB LED — red | P2.09 | active low, **confirmed on the board** |
| RGB LED — green | P1.10 | active low, **confirmed on the board** |
| RGB LED — blue | P2.07 | active low, **confirmed on the board** |
| Physical button | P1.13 | active low + pull-up |
| UART TX / RX | P1.04 / P1.05 | `uart20`, declared; the driver is off |
| Accelerometer | LIS2DH on SPI00 | IRQ P2.00 / P2.03 — **disabled**, see below |
| SHT40 (25015 only) | I2C21 | SDA P1.11, SCL P1.09 |

**`nordic,invert` in pinctrl is mandatory**, not an optimization. Without it the
first firmware flashed produced a **constant white LED**: the pins sit LOW while
PWM is stopped, and since the LEDs are active-low that means all three on at
full brightness.

The `pwm_nrfx` driver has a shortcut for 0% duty — it stops the peripheral and
parks the pin in GPIO at the correct level, honoring `PWM_POLARITY_INVERTED` —
but on nRF54L the pad stays under PWM control, so the GPIO write never reaches
it. Diagnosed by reading the registers over SWD: `P2 OUT = 0x020` and
`P1 OUT = 0x000`, i.e. exactly the LED bits at zero, with `PWM ENABLE = 0`.
After `nordic,invert`: `0x2a0` and `0x400`, i.e. off.

All three channels run off the **same PWM block** (`pwm20`) — nRF has four
channels per instance sharing one period, which is exactly what you want for RGB
where only the duty cycle differs. The `pwm_nrfx` driver stops the peripheral and
parks the pin in GPIO at 0% or 100% duty, so an LED that is off draws no current.

### What the status LED says

**Dark once it is on the network.** The background layer has exactly one job
left: to say whether the switch is waiting to be added to a network. Everything
else is an event — a short flash on a press, an amber pulse for Identify — not a
permanent indicator.

There used to be a permanent indicator: a short pulse every 10 s whose color and
brightness encoded the level the switch would send at the current time of day.
That stopped meaning anything when the schedule moved to the Pi — the switch no
longer decides the level, so it has nothing to announce — and it was not cheap:
**~30 µA at night, ~55 µA during the day**, which is 6–11 months out of a CR2032,
more than the radio costs. It was deleted rather than tuned.

**Locked = no answer at all.** A locked switch does not flash on a press, does
not blink, does nothing — the reason is under "Locking the switches". That has a
diagnostic cost: with the LED dark while idle anyway, a locked switch and a dead
one look the same from the wall. The permanent indicator used to tell them apart;
only the panel does now.

| Color | Shape | What it means |
|---|---|---|
| blue | blink, 1.2 s period | waiting to be added — commissioning window open |
| teal | two short flashes | commissioning succeeded |
| amber `#ffa726` | 1 s pulse, full brightness | Identify — "I am here". Same color and same rhythm as the Identify button in the panel |
| teal | one short flash | short press: the `Toggle` went out |
| teal | two flashes | long press: full brightness + 4000K sent |
| red / teal | two flashes | a switch in the lock role locked / unlocked the others |
| nothing, not even on a press | — | the switch is locked |

The flash after a short press is deliberately **neutral**: we do not know which
way the bulb toggled, and pretending we do would be exactly the lie this design
removed.

**Why a permanent indicator has to pulse, and why even pulsing was too much.** An
LED held on under PWM is not an option on battery: PWM needs HFCLK running and
costs ~1.5 mA in the peripheral alone, however faint the LED. That drains the
battery in days. A 180 ms pulse every 10 s keeps the peripheral up 1.8% of the
time, and between pulses the work item reschedules itself straight to the next
pulse, so nothing wakes up for nothing. Even that came to the 30–55 µA above,
which is why the periodic indicator is gone and only events remain.

**Signaling colors are picked SATURATED, not "pretty on screen".**

The first blue chosen was `#4a8fff` — an interface blue, with a little green in
it to read brighter. It looked good on a monitor. On the LED it came out
**green**, even though blue was at 100% duty and green at 27%: the blue die is
several times less efficient than the green one at the same current, so green
dominates.

Maximum saturation was not the answer either: pure `#0000ff` was too weak to see,
because 100% duty on the weakest die is still not much light. `kBlue` is now
`#0040ff` — about 5% green, enough to borrow light from the efficient die, too
little to drag the hue back toward green.

When a color does not look the way you expect, suspect this first, not the pin
mapping. `CONFIG_SMARTHOME_LED_CHANNEL_TEST` lights each channel with a different
number of blinks (1 = red, 2 = green, 3 = blue) and takes the status LED offline
while it runs, so nothing overlaps the observation. That separates "which pin"
from "which color" without guessing.

**Color calibration.** The sRGB → duty conversion is not linear: `#ffa726` means
100/39/2% per channel, not 100/65/15%. See the comment at `kSrgbToLinear` in
`src/status_led.cpp` — an amber built "straight from the sRGB values" always
comes out a washed-out yellow. On top of that the three dies do not have equal
efficiency, hence `CONFIG_SMARTHOME_LED_TRIM_{R,G,B}`; those are calibrated by
eye, holding the module next to the screen with Identify running.

### Why level 127 is not half

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

This is why brightness feels uncontrollable: the whole useful evening range is
crammed between 1 and 40, while the top half of the scale is nearly
indistinguishable. That is why the graphical editor in the panel has a
**perceptual vertical axis** — half the height really does look like half the
light, and it lands around level 47.

Careful: this assumes the bulb maps level linearly to light. Some bulbs already
apply a curve of their own, and there is no way to read it. The table above is a
starting point; the final adjustment is done by eye, in the editor.

The physical button is P1.13 on both modules, but with a different index: `sw1`
on 25008, `sw0` on 25015 (where P1.09 is taken by I2C21 SCL). The remaining
`gpio-keys` entries are exposed pads, not mounted buttons.

The modules have no USB — flashing is over SWD (Raspberry Pi Pico with picoprobe,
or a J-Link).

## Locking the switches

A child playing with light switches is a good enough reason to be able to
disable them without taking them off the wall. The lock carries two pieces of
state — locked, and the role — both held **in the switch**, in non-volatile
memory: lock the switches, cut the power, and they do not unlock themselves when
it comes back.

A locked switch does not respond to a press **and does not blink either**. A
visible response would turn a locked switch into a toy.

Two paths, and they work together:

**From the panel.** Every switch has a lock toggle on its card, plus
`lock all` / `unlock all` above the list. The panel reads the value back after
the write — a confirmed write does not guarantee the value landed, and the
mistake is expensive here: you would believe you had locked when you had not.

**With a dedicated switch.** Any module can be given the **lock role**: it stops
driving bulbs, and on a press it sends its own lock state to every switch in its
binding table. The role is runtime state, not a separate firmware — the same
binary runs on any board and the panel decides which does what, otherwise you
would have to remember which module carries which image.

It sends the **value**, not a per-node toggle: if one switch misses the command,
the next press resynchronizes it instead of leaving it inverted forever.

A lock switch cannot lock itself and does not appear in its own target list — if
it locked itself, you would have no way to unlock the rest from the wall. The
panel is the escape hatch in any case.

In Matter terms, a lock switch writes an attribute on endpoint 2 of the other
switches, so the targets need an ACL entry with Manage privilege — the switches
had no ACL at all before this, because nobody wrote anything to them. The panel
writes it when you save a lock's bindings.

### The switch shows no actions in Apple Home

On purpose. The firmware has no `Switch` cluster (0x003B), so it exposes no
press events — there is nothing to automate on top of it from Apple Home. The
switch drives the bulb **directly**, through the binding, and that is all.

The good consequence: it works with the HomePod unplugged. The bulbs behave
normally in Apple Home; only the switch is not a source of automations there.

If you ever want the opposite (single/double/long as triggers in Apple Home), you
add a Generic Switch endpoint (device type 0x000F). That changes the endpoint
composition, so it has to be done before the first commissioning.

### The accelerometer is powered down explicitly

We do not use it, and on battery that matters. But how we turn it off changed,
and the reason is worth stating.

**The first approach** disabled the `lis2dh` node in the devicetree. The driver
was no longer compiled, nobody talked to the chip, and we relied on the LIS2DH
powering up in power-down mode. The datasheet says so — but we had never read a
register on this board, so it was an assumption, not a measurement. And if the
module carries a different chip than we think, the assumption collapses and the
current draw goes up silently.

**The current approach** compiles the driver and powers the sensor down
explicitly. That is not a contradiction: the driver does start it at init — it
writes the ODR in `lis2dh_init()`,
`zephyr/drivers/sensor/st/lis2dh/lis2dh.c:431` — and we write ODR = 0, i.e.
power-down, immediately afterwards. The cost is a few milliseconds at every boot
and ~2.3 KB of flash; the gain is certainty.

The result of the power-down is kept in `sAccelPowerDownResult` so it can be read
over SWD. It is `volatile` on purpose: nothing in the firmware reads it, and
without that LTO removes it — i.e. the verification instrument disappears exactly
when you need it. **Verified on the board: 0**, so the power-down succeeded.

Gestures (tap / double tap) now have their own option,
`CONFIG_SMARTHOME_ACCEL_GESTURES`, off by default. They used to be enabled
automatically whenever the node was active in the devicetree — which would have
kept the sensor running permanently, exactly what we want to avoid. An
accelerometer listening for taps costs tens of µA, comparable to the entire
average budget of a Thread SED.

The SPI bus stays **on** deliberately: the bus driver holds the `cs-gpios` lines
as inactive outputs. If we shut the bus down, the sensor's CS would float, and a
floating CS can make an SPI interface read noise as activity — the opposite of
what we want for power.


## Setup

```bash
./scripts/bootstrap.sh
```

Build:

```bash
./scripts/build.sh holyiot_25015
```

Flash over SWD, with a Raspberry Pi Pico as the probe:

```bash
./scripts/flash.sh holyiot_25015
```

| Pico | Holyiot module |
|---|---|
| GP2 | SWCLK |
| GP3 | SWDIO |
| GND | GND |
| 3V3 | VCC (optional — or leave the battery in) |

Do not power the module from the Pico and the CR2032 at the same time.

Details verified with pyocd 0.45.1:

- The target name is **`nrf54l`**, not `nrf54l15` — the long form is rejected.
- Support is **built in** since 0.45; you do not need a CMSIS pack.
- pyocd has a dedicated `picoprobe` plugin alongside `cmsisdap` — it works both
  with the Pico's legacy firmware and with debugprobe (CMSIS-DAP).
- Its memory map (`0x0`, length `0x17D000`, block `0x1000`) confirms the 4 KB
  alignment of our partitions and the 256 KB of RAM we reclaim.
- `pyocd flash` erases only the sectors it writes by default, so
  `settings_storage` survives a reflash and **you do not lose commissioning**.
  Use `--erase chip` only when you really do want to wipe everything.

APPROTECT is disabled explicitly in the firmware (`CONFIG_NRF_APPROTECT_DISABLE=y`,
LOCK unset), so the debug port stays open. This matters: pyocd's nRF54L target
has no recovery path over CTRL-AP, so locking the port would leave you needing a
J-Link plus nrfjprog to unlock it.

Commissioning (bulbs already in IKEA):

```bash
./scripts/commission.sh check 1001    # how many fabric slots the bulb has left
./scripts/commission.sh switch <code> # the switch, shared from the IKEA app
./scripts/commission.sh bulb 1001 <code>
./scripts/commission.sh bind 1001
./scripts/commission.sh verify 1001
```

There are two layers here that are easy to confuse: the **Thread network**
decides who can reach whom, the **Matter fabric** decides who is allowed to
command. The switch and the bulb have to share both. A Matter device cannot be
moved to a different Thread network without a factory reset.

From here, two options.

### A. Stay on the IKEA network

You add the switch in the IKEA app (over BLE), then "share" each device and bring
it into our fabric as well. Nothing is reset, nothing leaves IKEA, no bulb has to
be touched.

It does depend on two things you have to check first: that the IKEA app really
does offer sharing to another Matter ecosystem, and that you can reach the
DIRIGERA's Thread network over IPv6.

### B. Your own Thread network

You need an OpenThread Border Router (Raspberry Pi + nRF52840 dongle) and one
factory reset per bulb, once. After that you own everything: the Thread dataset,
the fabric, the node IDs.

It also resolves the uncertainties of option A — the IPv6 routing is yours, so
commissioning, binding and the OTA provider all work.

The reset does not necessarily mean handling the bulb: in Matter, removing the
last fabric triggers a factory reset, so it is usually done from the IKEA app.
Recommissioning a factory-new device does go over BLE, though, so you have to be
in the same room as the bulb — but not touch it.

**This is not a dead end:** as an admin you can open a commissioning window
remotely at any time (`open-window`) and invite Apple Home, Home Assistant, even
IKEA back in. Independence does not burn bridges.

### What could still force physical access

Exactly one thing: losing `ota/state/`. That is where the fabric's CA key and the
node IDs live. In option B, where you are the only admin, without it you can
neither remove the fabric from a bulb nor open a commissioning window — and the
only way out is a physical reset of every bulb.

```bash
./scripts/commission.sh backup
```

Run this after every device you add, and keep the archive off this machine. It is
the one thing in the whole project that genuinely hurts to lose.

## Build status (measured)

Built with NCS v3.0.0, Zephyr SDK 0.17.4, macOS arm64:

| Board | FLASH | RAM |
|---|---|---|
| holyiot_25015 | 87.5% (621 KB / **710 KB slot**) | 66.5% (170 KB / 256 KB) |
| holyiot_25008 | 87.1% (619 KB / **710 KB slot**) | 66.5% (170 KB / 256 KB) |
| nrf54l15dk | 54.4% (795 KB / 1426 KB) | 67.5% (256 KB) |

On the Holyiot modules FLASH is reported against **one image slot**, not against
the whole RRAM: OTA needs two slots. The image without logging is 621 KB, so
~89 KB of headroom is left for the application to grow into.

Logging is compiled out on Holyiot (`CONFIG_LOG=n`) — the 83 KB that saves is
exactly what makes two image slots fit for OTA.

The module **does** have a UART on P1.04/P1.05, so a console could be brought out
on wires. Our board definition does not declare one. If you need logs while
debugging, add the UART node and re-enable `CONFIG_LOG` in a separate build — but
then OTA no longer fits. For development, `nrf54l15dk` stays more convenient.

The original board definitions left the Holyiot modules with **188 KB of SRAM and
1428 KB of RRAM** — the rest (68 KB SRAM + 96 KB RRAM) is reserved implicitly by
`nrf54l15.dtsi` for the **FLPR** coprocessor, which we do not use. RAM came out
at 89% then, with almost no headroom. `boards/holyiot_*.overlay` reclaims the
memory, exactly the way the sample's own overlay does for nrf54l15dk.

If you ever run code on FLPR, delete those overlays.

For what has and has not been exercised on real hardware, see
[What is still missing](#what-is-still-missing).

## The trap that cost the most: the system clock never started

On first bring-up the switch looked alive but did nothing: LED dark, no schedule,
no reaction. The kernel booted, the drivers initialized, BLE advertised. And yet
nothing that depended on time ever happened.

The cause was **a single missing line in the board definition**:

```
CONFIG_NRF_GRTC_START_SYSCOUNTER=y
```

On nRF54L, GRTC is shared between cores and its counter has to be started by
**exactly one** of them. Every official nRF54L board in Zephyr has this line in
its defconfig (`nrf54l15dk`, `nrf54l09pdk`, all variants). The community
definition we started from omitted it, so nobody started the counter.

### Why it was so hard to find

The symptoms did not resemble each other and pointed in different directions:

- **The device looked hung at startup.** The `main` thread sat in
  `zms_add_empty_ate -> flash_write -> nrf_flash_sync_exe -> mpsl_timeslot_request`.
  RRAM writes synchronize with the radio through MPSL, and MPSL could not grant
  timeslots without a clock. Every write waited out `FLASH_TIMEOUT_MS` = 25
  seconds for nothing, and ZMS does a lot of writes when it initializes an empty
  partition.
- **The LED never came on.** The animation ran from a delayed `k_work` that never
  fired.
- **Nothing could be debugged.** The console is off for the OTA budget, RTT would
  not start, and LTO had melted the function boundaries, so gdb had no symbols.

The definitive proof came from reading `curr_tick`, the kernel's tick counter,
over SWD: **it stayed 0 forever**. After the fix it advances normally and at the
correct rate.

The diagnosis required a build with `CONFIG_LTO=n` (see `firmware/debug-gdb.conf`)
so breakpoints could be set, and reading variables out of RAM at the addresses
from the ELF.

### The lesson

Two workarounds we had tried before — `CONFIG_SOC_FLASH_NRF_RADIO_SYNC_NONE` and
moving LFCLK to the internal oscillator — were treating symptoms. Both were
removed once the real cause was fixed: with the clock running, MPSL
synchronization of writes works normally, and the 32.768 kHz crystal is fine.

**When you inherit a board definition from outside the Zephyr tree, it is worth
comparing it line by line against the official defconfig of the closest SoC.**
The difference that cost us a day was a single line.

## Build traps encountered (resolved)

- **`gn` has to be exactly the revision pigweed pins**
  (`git_revision:05eed8f6252e2dd6b555e0b65192ef03e2c4a276`, version 2179).
  A newer one fails with `Duplicate output file` on `pw_chrono`. Homebrew has no
  `gn` formula; `bootstrap.sh` fetches it from CIPD.
- **Python 3.13+ does not work** — NCS wants <= 3.12. `bootstrap.sh` builds the
  venv on `python@3.12`.
- **On nRF54L15 settings go through ZMS, not NVS** (`CONFIG_NVS=n`).
- **The board definitions were written for NCS v3.2.4**; on v3.0 they are missing
  `<nordic/nrf54l15_partition.dtsi>` (replaced with an inline table) and
  `chosen { zephyr,ieee802154 }` (added).
- **`SB_CONFIG_MATTER_OTA` is force-propagated** over `boards/*.conf` — a
  `CONFIG_CHIP_OTA_REQUESTOR=n` there is ignored. Turn it off in
  `Kconfig.sysbuild`.

## What is still missing

- **Hardware testing is partial.** Verified on the board: the GRTC fix, the LED
  channels and colors, the accelerometer power-down (`sAccelPowerDownResult`
  reads 0), the switch booting into a fabric and signaling with the LED,
  lock/unlock through the rewritten cluster (written as a boolean and read back),
  and the Pi writing `OnLevel` plus color temperature into a bulb, with `Toggle`
  then lighting it at exactly those values. Not verified: a physical press
  actually reaching a bulb through the binding, the periodic startup-state
  writes, and the accelerometer gestures.
- **Thread role.** The current build comes out a **Sleepy End Device** (the
  `light_switch` sample default) — see the latency section above.
- **`scripts/commission.sh` has not been run.** Devices have been commissioned,
  but from the panel (`pairing ble-thread` / `pairing code-thread`); the script
  itself was written from documentation.
- **The OTA flow is untested** — see [ota/README.md](ota/README.md). The firmware
  side is verified (partition map, signed image, generated `matter.ota`); the
  host side needs hardware and a Thread network.

## Licensing

**This project's own code is Apache-2.0** — see `LICENSE`. That is deliberate:
the Matter SDK and Zephyr are both Apache-2.0, so the whole portable stack sits
under one licence and a port has no compatibility question to answer.

`firmware/` also contains files derived from the nRF Connect SDK's
`matter/light_switch` sample. They keep their original
`SPDX-License-Identifier: LicenseRef-Nordic-5-Clause` headers, and the full text
is in `LICENSE.nordic`. **Read clause 4 before you plan a port:**

> This software, with or without modification, must only be used with a Nordic
> Semiconductor ASA integrated circuit.

That is a field-of-use restriction, which means those particular files are not
open source in the OSI sense, and cannot be used on an ESP32-C6 or any other
non-Nordic radio.

### What that costs you, exactly

Less than it sounds, because the restriction lands on the shell rather than the
substance:

| | lines | licence |
|---|---|---|
| `light_ctrl`, `automation`, `status_led`, `lock_cluster`, `lock_state` | 1691 | Apache-2.0 (ours) |
| `light_switch.matter` — the data model | 2198 | Apache-2.0 (ours) |
| `main.cpp`, `app_task.{h,cpp}`, `chip_project_config.h` | 367 | Nordic 5-Clause |
| build config: `CMakeLists.txt`, `Kconfig*`, `prj.conf`, `sysbuild.conf` | — | Nordic 5-Clause |

The logic includes **no Nordic-specific API at all** — no `nrfx_`, no `nrf_`, no
`hal/nrf`. It reaches for Matter (`app/`, `controller/`, `platform/`) and Zephyr
(`drivers/pwm.h`, `drivers/sensor.h`, `kernel.h`) and nothing else, both of them
Apache-2.0.

So the Nordic-licensed part is precisely the part a port throws away anyway: an
ESP-IDF Matter application brings its own entry point, its own task skeleton and
its own build system. Take the 3889 lines above, write your own shell around
them, and nothing you ship is touched by clause 4.

### Third-party components

| what | where | licence |
|---|---|---|
| nRF Connect SDK sample (shell + build config) | `firmware/` (19 files) | LicenseRef-Nordic-5-Clause |
| Project CHIP / Matter SDK (ZAP-generated) | `firmware/src/default_zap/`, `firmware/snippets/` | Apache-2.0 |
| everything else | `panel/`, `deploy/`, `scripts/`, the rest of `firmware/src/` | Apache-2.0 |

The board definitions under `firmware/boards/holyiot/` are ours. Board files for
Nordic development kits the project does not build for were removed rather than
carried along.

*Not legal advice — read `LICENSE.nordic` yourself before redistributing.*
