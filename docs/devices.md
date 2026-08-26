# Devices this panel has actually been used with

Not a compatibility promise. Every row here is a device that has been
commissioned onto a real fabric and driven from the panel, with what worked and
what did not written down next to it. Anything not listed may well work — the
panel asks a device what it is rather than being told — but nobody has checked.

The identity fields come from each device's own Basic Information cluster
(`0x0028`), not from the box.

All of these are commissioned onto **python-matter-server**, the panel's Matter
client.

Moving a device between Matter clients needs no factory reset — Matter allows
several fabrics at once, so you join the new one through a commissioning window
opened on the old, then remove the old. Two things bite if you do:

- **Binding and ACL are fabric-scoped.** A switch's binding table reads back as
  *empty* from a fabric that did not write it, while it is still perfectly bound
  on the one that did. Both have to be rewritten on the new fabric, and the old
  fabric has to go, or the switch sends every command twice — which for a
  `Toggle` means nothing appears to happen.
- **The binding table is one fixed pool shared by all fabrics.** With an old
  fabric's six entries still in place, only four of the new six fit — and the
  write reports success. Remove the old fabric first, then write.

## Supported

### IKEA KAJPLATS E27 CWS globe 1055lm — bulb

| | |
|---|---|
| Vendor / product | `IKEA of Sweden` / `KAJPLATS E27 CWS globe 1055lm` |
| Firmware seen | 1.1.0 |
| Device type | `0x010D` extended colour light |
| Clusters | Identify, Groups, OnOff, LevelControl, Descriptor, ColorControl |
| Transport | Matter over Thread, mains powered, acts as a Thread router |

Works: on/off, brightness, colour temperature, the daylight schedule, binding to
a switch so the wall button drives it with the Pi asleep, `OnLevel` so it comes
back at the right brightness after a power cut. Identify works both ways — it
accepts `Identify` and also `TriggerEffect` (Blink).

**It is a full RGB bulb and this project drives it as tunable white.** Its
ColorControl `FeatureMap` is `31` — hue/saturation, enhanced hue, colour loop,
XY and colour temperature, all present. Nothing here ever sends a hue command:
the panel's strip is a colour-temperature strip and the switch's long press
sends `MoveToColorTemperature`. The white range it reports is **1802 K – 6536 K**
(mireds 555–153).

### IKEA ALPSTUGA air quality monitor — sensor

| | |
|---|---|
| Vendor / product | `IKEA of Sweden` / `ALPSTUGA air quality monitor` |
| Firmware seen | 1.0.15 |
| Device type | `0x002C` air quality sensor |
| Clusters read | AirQuality `0x005B`, TemperatureMeasurement `0x0402`, RelativeHumidity `0x0405`, CO2 `0x040D`, PM2.5 `0x042A` |
| Transport | Matter over Thread, mains powered, acts as a Thread router |

Works: all five readings, polled and shown on the tile and in its sheet. The
tile headlines air quality because that is the summary the others feed.

Nothing commands it — it is read, not driven — so it gets no ACL and no binding.

### IKEA MYGGBETT door/window sensor — contact sensor

| | |
|---|---|
| Vendor / product | `IKEA of Sweden` / `MYGGBETT door/window sensor` |
| Firmware seen | 1.0.9 |
| Device type | `0x0015` contact sensor |
| Clusters | Identify `0x0003`, Descriptor `0x001D`, BooleanState `0x0045` |
| Transport | Matter over Thread, **battery**, intermittently connected |

Works: open/closed is read and shown. `StateValue` **false = open, true =
closed** — checked against the physical sensor, not taken from the spec, because
a door reported shut while it is open is the one reading somebody would act on
without going to look.

**Caveats, and they are the interesting part:**

- **It is battery powered, so it sleeps.** A read does not wake it; the request
  waits at its Thread parent and is picked up on the poll the device was going
  to make anyway. Answers take **0.4–6.5 s**, and sometimes never come — a read
  landing in the wrong moment simply fails. So polling is the wrong shape for the
  device and no amount of tuning fixes it: our interval is a floor we cannot get
  under, not a target.
- **It is subscribed, not polled.** matter-server holds the subscription and the
  sensor transmits the moment the contact changes. Nothing is queued for it and
  nothing waits. Measured side by side against a poller: six state changes pushed
  here against four noticed there, tens of seconds late.
- **Whatever holds the subscription must actually stream reports.** A client
  that answers a subscribe request with the current value and then quietly drops
  the subscription is *worse* than polling, not merely no better: the priming
  report is indistinguishable from it working, and a node believed to be
  subscribed is not polled, so its value freezes for ever. A door sensor stuck on
  `closed` is precisely the reading somebody acts on without going to look.
  `PANEL_SUB_SILENCE` guards against it: a node not heard from for too long goes
  back to the poller, so a link that dies quietly costs latency and never a stale
  value presented as current.
- **Event devices are exempt from the cold shoulder.** `BULB_COLD_SEC` stops an
  unplugged bulb burning a full timeout on every page load: one failed read and
  it is not tried again for 300 s. Applied to a sleepy sensor, which misses reads
  as a matter of course, that is ruinous — a magnet takes minutes to register
  because the sensor keeps falling into a shoulder longer than anyone would stand
  there watching.
- **Battery.** A subscription costs the device a transmission only when the door
  actually moves. Polling costs one every ~11 s for ever, which is what the
  cluster exists to avoid.
- **Identify works. It is a brief double flash, and it arrives late.** This
  looked broken for a long time and was not.

  The LED does **not** blink for the requested duration. It flashes twice, for
  about a second, and then goes dark for the rest of the countdown — which is
  the right call on a coin cell, where fifteen seconds of LED costs far more
  than the radio does.

  And it arrives **6–15 seconds after you press**, because the device is asleep:
  the command waits at its Thread parent until the device next wakes. Measured
  end to end:

  ```
  10:47:23   panel sent Identify(15)
  10:47:32   device entered identify, IdentifyTime = 15     <- 9 s later
  10:47:33   IdentifyTime = 14 ... reported every second
  ```

  Put together, that is a two-second event happening at an unpredictable moment
  up to fifteen seconds after the button — which is why watching the sensor for
  a minute after pressing finds nothing. If the device happens to be awake
  already it starts within a second, so the delay varies with nothing you can
  see.

  The panel shows the **device's own** `IdentifyTime` on the button rather than
  a timer of its own, because a browser-side countdown measures the wrong
  fifteen seconds entirely.

  **Why two flashes looks like a fault.** Matter §1.2.6.1 says a device
  `SHALL` enter its identification state "in order to indicate to an observer",
  and that it is `RECOMMENDED` that the state "consists of flashing a light with
  a period of 0.5 seconds". Continuous flashing is the recommendation, not the
  requirement — so a brief acknowledgement is within the letter of the spec and
  looks nothing like what anyone expects. Certification does not settle it
  either: the only test that checks the light, TC-I-2.2, ends in a manual
  verification step gated behind `PICS_USER_PROMPT`, which is `0` in CI. Every
  machine-checkable part of identify passes on a device that merely runs the
  countdown.

  Note also that this LED already uses a double flash to mean "connected" during
  pairing, so identify appears to reuse the firmware's existing acknowledgement
  rather than implementing a distinct effect.

  `TriggerEffect` is **not implemented** — it answers `UNSUPPORTED_COMMAND`, and
  `AcceptedCommandList` on the cluster is `[0]`, so plain `Identify` is the only
  mechanism this device has. `IdentifyType` is 2, VisibleIndicator. Writing the
  `IdentifyTime` attribute works identically to sending the command.

- **It reports its battery, on endpoint 0**, and the panel shows it as a gauge on
  the tile. Two things to get right:

  - **The endpoint.** PowerSource (`0x002F`) is on endpoint 0, and the descriptor
    the panel records deliberately skips endpoint 0 — so readings are matched
    against the node's whole attribute set instead. A battery could never be
    found by walking the descriptor.
  - **The attribute ids**, which are easy to take one off and which fail
    *plausibly* rather than loudly. Reading `0x0C` as the voltage, `0x0E` as the
    percentage and `0x10` as the charge level yields "200 mV, 0%, critical" for a
    perfectly good cell — a complete and convincing account of a dead battery,
    assembled entirely from correct readings of the wrong attributes. The real
    layout:

    | id | attribute | note |
    |---|---|---|
    | `0x0B` | BatVoltage | mV — 1604 here |
    | `0x0C` | BatPercentRemaining | **half** percent, 0..200 |
    | `0x0E` | BatChargeLevel | 0 ok, 1 warning, 2 critical |
    | `0x10` | BatReplaceability | not a charge level |

    The voltage is worth surfacing for exactly this reason: it is the one battery
    figure a multimeter can check, and it is what caught the mistake.

### IKEA KLIPPBOK water leak sensor — water leak detector

| | |
|---|---|
| Vendor / product | `IKEA of Sweden` / `KLIPPBOK water leak sensor` |
| Part number | `E2493` |
| Firmware seen | 1.0.7 |
| Device type | `0x0043` water leak detector |
| Clusters | Identify `0x0003`, Descriptor `0x001D`, BooleanState `0x0045`, BooleanStateConfiguration `0x0080` |
| Transport | Matter over Thread, **battery**, intermittently connected |

Works: wet/dry is read and shown, and a detected leak turns the whole tile red.
`StateValue` **true = water present**.

- **It reports instantly.** Measured: `StateValue` flips and the panel has it in
  the same second. There is no wake-up delay because the device is *sending* —
  the 6–15 s lag that afflicts identify is the cost of a sleeping device
  *receiving*, and it does not apply in this direction.
- **It alarms on its own.** `AlarmsSupported` and `AlarmsEnabled` are both `3`
  (bit 0 visual, bit 1 audible), and on a leak `AlarmsActive` goes to `3` and
  back to `0` when dry — so the device flashes and beeps without being asked.
  Worth knowing before you assume the panel is the only thing that will notice.
- **It shares its cluster with the door sensor.** `BooleanState` is the same
  cluster a contact sensor uses, and Matter separates them by DEVICE TYPE. See
  `BOOLEAN_KINDS` in `panel/server.py`: mapping the cluster straight to
  "contact" showed a flood as `contact: open`.
- Battery reads 3.14 V at 100%.

### IKEA MYGGSPRAY motion sensor — occupancy + light sensor

| | |
|---|---|
| Vendor / product | `IKEA of Sweden` / MYGGSPRAY motion sensor |
| Device types | endpoint 1 `0x0106` light sensor, endpoint 2 `0x0107` occupancy sensor |
| Clusters | Identify `0x0003`, Descriptor `0x001D`, IlluminanceMeasurement `0x0400` (ep1), OccupancySensing `0x0406` (ep2) |
| Transport | Matter over Thread, **battery**, intermittently connected |

**It is two sensors in one**, on two endpoints, and it must be named after the
occupancy one — a device that also measures light is a motion sensor with a
light meter in it, not a light sensor. `TYPE_PRIORITY` in `panel/server.py`
decides that; without it the panel called it a "light sensor" and headlined its
tile `light 11 lx`.

**Motion is instant. Light is not. The difference is large and it is the
device's, not the panel's.** Measured end to end:

```
occupancy   device -> panel   same second
            panel  -> browser 0.3-0.7 s
illuminance device reports at irregular intervals: +52 s, +299 s, +100 s
```

So walking past shows up immediately, and a change in light takes up to a couple
of minutes to appear.

**Light reporting is irregular, and reports only on change.** Observed gaps
between illuminance reports, in seconds:

```
23   60   181   24   60   698   94
```

Some land one to three seconds after a motion event, which makes it tempting to
conclude the sensor only measures light when the PIR wakes it — that conclusion
was drawn here on three samples and is wrong. A later report arrived 62 s after
the last motion with the room already occupied, so it samples on its own as
well. The long gaps (181 s, 698 s) are periods when the reading did not change:
`min interval 0` means report on change, and an unchanged value is not a change.

What this means in practice: **turn a light on and the panel catches up within a
minute or two, not instantly.** A test that changes the light and then checks
the screen a few seconds later will always look broken. `Tolerance` is
`UnsupportedAttribute`, so the device does not say how big a change it considers
worth reporting either.

**Occupancy clears about 23 s after the LAST movement, and the hold is not
adjustable.**

Read the intervals carefully, because they are easy to misread — as they were
here at first. Observed detected-to-clear spans were 23, 31, 167 and 221 s,
which looks like a wildly variable hold. It is not. The sensor reports only the
*transition* to occupied; while movement continues it stays at 1 and sends
nothing further, so a long span means somebody was still in the room, re-arming
the timer. The true hold is the shortest span: **~23 s of quiet**.

Two useful consequences: a sustained `detected` really does mean continued
presence rather than a stuck flag, and anything built on this has an off-delay
floor of about twenty seconds, not a minute.

Matter defines four ways to configure that delay and this device implements
none of them:

```
AttributeList = [0, 1, 2, 65528, 65529, 65531, 65532, 65533]
  0x0003 HoldTime                      UnsupportedAttribute
  0x0004 HoldTimeLimits                UnsupportedAttribute
  0x0010 PIROccupiedToUnoccupiedDelay  UnsupportedAttribute
  0x0011 PIRUnoccupiedToOccupiedDelay  UnsupportedAttribute
```

So the hold is fixed in firmware and no controller can shorten it. It is also
not a defect: a PIR senses *change*, not presence, and one that reported "clear"
the moment you stopped moving would switch off every time you sat still.
Covering the sensor does not shortcut it either — to the sensor that is simply
"no new motion", so it waits out the same minute.

The practical shape, for anything built on it: **on immediately, off about
twenty seconds after the room goes quiet.** Logic in the panel can only ever
make that delay longer.

**Illuminance is logarithmic.** `MeasuredValue = 10000 x log10(lux) + 1`, so a
raw 10414 is 11 lux and not 10414 of anything. The device confirms its own
encoding: `MinMeasuredValue 1` and `MaxMeasuredValue 40001` decode to 1 lux and
10000 lux, which is a sane range where 1..40001 lux would not be. Decoded by
`lux_from_measured()`; 0 is the spec's "too dark to measure".

### IKEA BILRESA dual button — remote

| | |
|---|---|
| Vendor / product | `IKEA of Sweden` / `BILRESA dual button` |
| Part number | `E2489`, hardware `P2.0` |
| Firmware seen | 1.9.15 |
| Device type | `0x000F` generic switch, revision 3 — one per button |
| Clusters | Identify `0x0003`, Descriptor `0x001D`, Switch `0x003B` on endpoints 1 and 2 |
| Transport | Matter over Thread, **battery** (2x AAA), intermittently connected |
| SoC | Qorvo QPG6200L |

Works: both buttons drive whatever you tick for them in the panel. Each button
is its own endpoint, and the device names them itself — endpoint 1 carries the
label `button 1` in its `TagList`, endpoint 2 carries `button 2`.

- **It cannot be bound, and this is the one thing to understand about it.**
  There is no Binding cluster on any endpoint, and `ClientList` is empty on both
  buttons, so it cannot originate a command at all — it reports that it was
  pressed and nothing more. Either fact alone is enough. So the Pi is what acts
  on a press, and **the remote goes quiet whenever the Pi does**, unlike the
  switch we build, which keeps working because the instruction lives in the
  bulb-facing binding table.
- **A long press never sends `MultiPressComplete`.** Measured, from the device's
  own timestamps:

  ```
  short press   InitialPress -> ShortRelease (141 ms) -> MultiPressComplete count 1  (+519 ms)
  double press  the same twice, MultiPressOngoing between  -> MultiPressComplete count 2
  LONG PRESS    InitialPress -> LongPress (+700 ms) -> LongRelease.  Nothing else.
  ```

  Listening only for the completion event is the obvious design and it silently
  ignores every long press.
- **A double tap passes through `ShortRelease` twice.** Acting on the release is
  what makes a press feel instant — the completion event is half a second
  later — but done naively it fires the action twice on a double tap. The panel
  drops the repeat because `MultiPressOngoing` arrives before the second
  release.
- **`FeatureMap` is 30**: MS + MSR + MSL + MSM. `NumberOfPositions` 2,
  `MultiPressMax` 2, so single and double are the only counts it reports.
- **It cannot be reflashed.** The debug pads are there — `CLK`, `TX`, `TMS`,
  `EN` — and the open-source path exists: connectedhomeip's `qpg_platform.gni`
  names `qpg6200`, the Qorvo support library is a public GitLab repo, and
  `examples/light-switch-app/qpg` is the same sample this project already runs
  in its nrfconnect flavour. **Qorvo secure debug is what stops it**: the debug
  port answers, but every memory read faults with the AHB MEM-AP `CSW` reporting
  `DeviceEn = 0`, and UART prints `Secure boot Enabled`. Getting past it needs
  manufacturer-signed credentials.

## Our own hardware

### Holyiot nRF54L15 module — switch

| | |
|---|---|
| Vendor / product | `Nordic Semiconductor ASA` / `not-specified` |
| Firmware | this repository, `firmware/` |
| Transport | Matter over Thread, battery, sleepy end device (SIT, 15 s poll) |

Runs the firmware in this repo. Binds directly to bulbs so a press reaches them
with the Raspberry Pi switched off. A press toggles; a long press sets full
brightness at 4000 K. It also carries the lock role, and the schedule role
attribute the panel writes.

`ProductName` is `not-specified` because the firmware ships with the sample's
test credentials — see the *Before production* checklist in the main README.

The 15 s poll interval is why changing its role from the panel can take that
long to be acknowledged: the write waits at its Thread parent until the switch
next wakes. It is set per board in
`firmware/boards/holyiot_25015_nrf54l15_cpuapp.conf`
(`CONFIG_CHIP_ICD_SLOW_POLL_INTERVAL`), and trades directly against battery life.

## What the panel needs from a device

Nothing is hardcoded per product. On commissioning the panel reads the
Descriptor cluster and decides from that:

- a **light** (device types `0x0100`, `0x0101`, `0x010C`, `0x010D`) gets the
  bulb treatment: brightness, colour if it has ColorControl, and a place on the
  schedule
- anything else becomes a **read-only device**, and the panel asks it for every
  measurement in the `MEASURED` table it advertises

**One cluster can mean several things.** `BooleanState` is used by contact
sensors, water leak detectors, freeze detectors and rain sensors alike, and only
the device type says which. `BOOLEAN_KINDS` maps type to meaning, and
`boolean_kind()` resolves it per device — so the same bit reads as `closed` on a
door and `LEAK` on a flood sensor. Device type ids come from
`panel/matter_names.json`, generated from the SDK, because a hand-written table
had `0x0041`–`0x0044` labelled as CO2, CO, PM1 and PM2.5 sensors when they are
the freeze detector, water valve, leak detector and rain sensor.

Adding a new kind of reading is one line in `MEASURED` in `panel/server.py`.
Readings that are a state rather than a quantity — a contact, an occupancy — add
a second line in `MEASURE_WORDS` so they render as words instead of `true`, and
a third in `SUB_KEYS` if the reading is an *event* that should arrive the moment
it happens rather than on the next poll.

Readings are matched against everything the node reports, **including endpoint
0**, which is where PowerSource lives — so a battery is found without the device
having to be re-interviewed, and a stale stored descriptor cannot hide a reading.

A bulb needs nothing added: `BULB_ATTRS` already maps OnOff, LevelControl and
ColorControl from a pushed report into the same fields a read fills, so a light
the panel has never seen behaves correctly the moment it is commissioned.

Identify sends `Identify` and then `TriggerEffect` (Blink). Devices differ about
which one lights the lamp: the KAJPLATS bulb takes both, the MYGGBETT sensor
answers `UNSUPPORTED_COMMAND` to the effect and only has the duration. The effect
failing is logged and never fails the request, because by then the identify has
already succeeded.
