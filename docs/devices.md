# Devices this panel has actually been used with

Not a compatibility promise. Every row here is a device that has been
commissioned onto a real fabric and driven from the panel, with what worked and
what did not written down next to it. Anything not listed may well work — the
panel asks a device what it is rather than being told — but nobody has checked.

The identity fields come from each device's own Basic Information cluster
(`0x0028`), not from the box.

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
  queues at its Thread parent and is delivered when the device next polls. Our
  polling interval is therefore a floor we cannot get under, and a read can
  simply fail — the panel shows `ok: false` and keeps the last known value.
  End to end, a change can take tens of seconds to appear on a tile.
- **So the panel subscribes to it instead of polling it.** `BooleanState` is in
  `SUB_KEYS`, so a `watcher` thread holds a subscription on a connection of its
  own and the poller skips the node entirely. Set `PANEL_SUBSCRIBE=0` to go back
  to polling.
- **A subscription owns the device's session.** While one is held, a direct read
  of that same node comes back empty — which is why the poller has to skip it,
  or the device would flap between "reporting" and "not answering".
- **Identify is accepted; the LED has not been seen here.** `Identify` lands —
  `IdentifyTime` counts down on the device afterwards — and the device reports
  `IdentifyType = 2`, VisibleIndicator. **`TriggerEffect` is not implemented at
  all**: it answers `UNSUPPORTED_COMMAND`. So plain `Identify` is the only
  mechanism this device has, which means IKEA's app must use it too, and the LED
  it blinks is the one this ought to blink. Unresolved.

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

Identify sends `Identify` and then `TriggerEffect` (Blink). Devices differ about
which one lights the lamp: the KAJPLATS bulb takes both, the MYGGBETT sensor
answers `UNSUPPORTED_COMMAND` to the effect and only has the duration. The effect
failing is logged and never fails the request, because by then the identify has
already succeeded.
