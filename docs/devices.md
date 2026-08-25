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
- **Identify is accepted and the protocol demonstrably works. The LED still does
  not light, and that is unexplained.** Sending `Identify(60)` and watching
  `IdentifyTime`:

  ```
   1.9s  ACCEPTED by the device
   2.6s  IdentifyTime = 60
  36.4s  IdentifyTime = 26
  63.3s  back to 0 - finished
  ```

  The device sits in identify mode for a full minute, counting down in real
  time, and never lights its LED. So the command lands, the endpoint is right,
  and the device state is right — it is *choosing* not to light it, while IKEA's
  own app blinks the same device.

  `TriggerEffect` is **not implemented at all** — it answers
  `UNSUPPORTED_COMMAND`, and `AcceptedCommandList` on the cluster is `[0]`, so
  plain `Identify` is the only mechanism this device has. `IdentifyType` is 2,
  VisibleIndicator.

  A low battery was ruled out: the cell reads 1.6 V and 100%, confirmed on a
  multimeter. Still open.

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
