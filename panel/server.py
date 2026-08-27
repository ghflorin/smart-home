#!/usr/bin/env python3
"""
Local admin panel for the Matter network.

What it does:
  - lists the devices (from devices.json)
  - reads the switch's binding table: which switch controls which bulbs
  - sends Identify to a bulb or to a switch, so you can see which one is which

How it talks to the network: through chip-tool started in "interactive server"
mode, which exposes a WebSocket. Compared to running chip-tool once per command,
this keeps the CASE sessions warm - which matters most for the switch, a sleepy
device that would otherwise renegotiate on every click.

The protocol is simple: you send the command as text ("identify identify 10 1001
1") and get back JSON with the results and the status.

Start with: ./run.sh
"""

import collections
import json
import math
import os
import pathlib
import queue
import re
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from websockets.sync.client import connect as ws_connect
except ImportError:
    raise SystemExit(
        "The 'websockets' package is missing.\n"
        "  pip install websockets\n"
    )

# Beside this file, so it shares the panel's dependencies and nothing else.
from matter_link import MatterCall, MatterError, MatterLink

try:
    import segno
except ImportError:
    raise SystemExit(
        "The 'segno' package is missing (QR code generation).\n"
        "  pip install segno\n"
    )

HERE = pathlib.Path(__file__).parent
DEVICES_FILE = HERE / "devices.json"
THREAD_DATASET = pathlib.Path(
    os.environ.get("THREAD_DATASET",
                   str(HERE.parent / "ota" / "state" / "thread-dataset.hex")))
PANEL_PORT = int(os.environ.get("PANEL_PORT", "8080"))

# Skips attestation certificate verification during commissioning.
#
# WHY THIS EXISTS. The controller checks the device's signature against the
# attestation roots (PAA) it ships with. That set holds 40 authorities and does
# NOT include IKEA (vendor 0x117C), so every IKEA bulb fails with
#
#   Failed in verifying 'Attestation Information': err 101   (kPaaNotFound)
#
# WHAT YOU GIVE UP. That check is the defense against a counterfeit device
# posing as something else. With it off, you commission whatever answers you.
# For a bulb you bought yourself and are holding in your hand that is a
# reasonable call; for something you were given or found, it is not.
#
# HOW TO PUT IT BACK: remove PANEL_BYPASS_ATTESTATION from
# smarthome-panel.service and put the real certificates in ota/paa-root-certs
# (see deploy/paa-certs.sh).
BYPASS_ATTESTATION = os.environ.get("PANEL_BYPASS_ATTESTATION", "") not in ("", "0", "false")

# ------------------------------------------- what used to be here, and why not
#
# A freezer: a thread that SIGSTOPped chip-tool when nothing had used it for two
# minutes, and SIGCONTed it just before each command.
#
# chip-tool burns a whole CPU core while completely idle, and it is not doing
# work: examples/common/websocket-server/WebSocketServer.cpp runs
# lws_service(ctx, -1) in a loop, and libwebsockets inverts the POSIX convention
# - a negative timeout means "do not block" rather than "block forever", so the
# loop spins. Reported upstream as connectedhomeip#29971 in October 2023, still
# open. Measured here: 100% of one core for eight days straight, which also held
# this Pi 3B+ at its 60 C soft-throttle point, running at 1200 MHz instead of
# 1400 - roughly 1.5 W and 14% of the clock, to do nothing.
#
# Freezing a process to work around its event loop is not a fix, and it brought
# its own hazard: a frozen chip-tool is indistinguishable from a dead one, so
# every path had to thaw first and never freeze mid-command.
#
# All of it is gone with chip-tool itself. matter-server idles at 0.2%.



# -------------------------------------------------------------------- the log
#
# The switch has NO console in the production build: CONFIG_SERIAL is off so
# that two image slots fit for OTA. So the only place you can see what is
# happening is right here - what the Pi sent and what the device answered. On
# the device itself, the feedback is the RGB LED.
#
# An in-memory ring buffer, not a file: what matters is the last few minutes
# while you are watching the panel, not a history. Every line carries an
# increasing id, so the UI only asks for what it has not seen yet.
LOG_MAX = 400
_log = collections.deque(maxlen=LOG_MAX)
_log_seq = 0
_log_lock = threading.Lock()


def log(msg: str, level: str = "info", **fields):
    global _log_seq
    with _log_lock:
        _log_seq += 1
        _log.append({"id": _log_seq, "t": time.time(), "level": level,
                     "msg": msg, **fields})


def log_since(since: int) -> dict:
    with _log_lock:
        lines = [x for x in _log if x["id"] > since]
        return {"lines": lines, "next": _log_seq}








def load_devices() -> dict:
    return json.loads(DEVICES_FILE.read_text())


def save_devices(devices: dict):
    """Atomically. This file is the registry: node IDs, names, rooms. Truncate
    it half way through a write and the panel comes back knowing about no
    devices at all, on a fabric that still has them - and there is no way to
    read the names back out of the network, because Matter never had them."""
    tmp = DEVICES_FILE.with_name(DEVICES_FILE.name + ".tmp")
    tmp.write_text(json.dumps(devices, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(DEVICES_FILE)


def all_devices(devices: dict) -> list:
    """Every device, for the things that treat them alike - a room contains
    whatever is in it, and a drag moves whatever it grabbed."""
    return (switches(devices) + list(devices.get("bulbs", []))
            + list(devices.get("devices", [])))


# A remote is a switch that cannot be wired to anything.
#
# Ours has the Binding cluster and an OnOff client, so a binding table written
# into it makes it command the bulb directly - and it keeps working with the Pi
# switched off, because the instruction lives in the device. A bought remote
# like the BILRESA has neither: its endpoints implement only Identify,
# Descriptor and Switch, and their ClientList is empty, so it cannot originate a
# command at all. It reports that it was pressed and nothing more.
#
# It is still a switch as far as the panel is concerned - the same editor picks
# what it controls - but the mapping lives HERE and the Pi is what acts on it.
REMOTE_TYPES = {0x000F}


def is_remote(entry: dict) -> bool:
    if entry.get("remote"):
        return True
    return bool(set((entry.get("desc") or {}).get("types") or []) & REMOTE_TYPES)


def migrate_remotes(devices: dict) -> bool:
    """Move any remote filed as a sensor into the switch list. Returns whether
    anything moved.

    Commissioning asks the device what it is and files a non-light under
    `devices`, which is right for everything that is read and wrong for a thing
    that is pressed. Done here rather than by hand so an install that already
    has one does not need editing.
    """
    moved = []
    keep = []
    for d in devices.get("devices") or []:
        (moved if is_remote(d) else keep).append(d)
    if not moved:
        return False
    devices["devices"] = keep
    sws = devices.setdefault("switches", [])
    for d in moved:
        d["remote"] = True
        sws.append(d)
    return True


def remote_buttons(entry: dict) -> dict:
    """Which bulbs each button drives: {"1": [node, ...], "2": [...]}."""
    out = {}
    for ep, cfg in (entry.get("buttons") or {}).items():
        if isinstance(cfg, list):          # the short form, bulbs only
            out[str(ep)] = [int(n) for n in cfg]
        elif isinstance(cfg, dict):
            out[str(ep)] = [int(n) for n in cfg.get("bulbs") or []]
    return out


def switches(devices: dict) -> list:
    """The list of switches. The old single-'switch' format is still accepted so
    existing installs keep working."""
    out = list(devices.get("switches", []))
    one = devices.get("switch")
    if one and not any(s["node"] == one["node"] for s in out):
        out.insert(0, one)
    return out


# --------------------------------------------------- anything, not just bulbs
#
# The panel started with two kinds of device because that is what the house had:
# a switch and a bulb, each with its own array in devices.json and its own
# commissioning path. Everything else - a sensor, a plug, a blind - had nowhere
# to go, and adding a kind meant adding a branch everywhere.
#
# So there is now a third, open list. A device commissioned generically lands in
# `devices` with whatever it turned out to be, and the panel asks IT what it is
# rather than being told in advance. Switches and bulbs keep their own arrays:
# the binding table, the ACL and the schedule are genuinely bulb-and-switch
# logic, and collapsing them into one list would buy nothing but a `kind` check
# in front of each of them.

DESCRIPTOR_CLUSTER = 0x001D
DESC_DEVICE_TYPE_ATTR = 0x0000
DESC_SERVER_LIST_ATTR = 0x0001
DESC_PARTS_LIST_ATTR = 0x0003

# The Matter device types worth naming on a tile. Anything not here still works;
# it just shows its numeric type until somebody adds a line.
DEVICE_TYPE_NAMES = {
    0x000A: "door lock", 0x000B: "door lock controller",
    0x0010: "on/off plug", 0x0100: "on/off light", 0x0101: "dimmable light",
    0x010A: "on/off plug-in unit", 0x010B: "dimmable plug-in unit",
    0x010C: "colour temperature light", 0x010D: "extended colour light",
    0x0103: "on/off switch", 0x0104: "dimmer switch",
    0x0105: "colour dimmer switch", 0x0106: "light sensor",
    0x0107: "occupancy sensor", 0x0015: "contact sensor",
    0x0302: "temperature sensor", 0x0307: "humidity sensor",
    0x0305: "pressure sensor", 0x0306: "flow sensor",
    0x0076: "smoke/CO alarm", 0x002C: "air quality sensor",
    0x002B: "fan", 0x0301: "thermostat", 0x0202: "window covering",
}

# What a cluster measures, how to read it and how to say it. `scale` turns the
# raw attribute into the unit people use: Matter reports temperature in
# hundredths of a degree, humidity in hundredths of a percent.
MEASURED = [
    # cluster, attribute, key,        label,          unit,   scale
    (0x0402, 0x0000, "temperature",  "temperature",  "\u00b0C", 0.01),
    (0x0405, 0x0000, "humidity",     "humidity",     "%",        0.01),
    (0x040D, 0x0000, "co2",          "CO\u2082",    "ppm",      1),
    (0x042A, 0x0000, "pm25",         "PM2.5",        "\u00b5g/m\u00b3", 1),
    (0x042C, 0x0000, "pm10",         "PM10",         "\u00b5g/m\u00b3", 1),
    (0x042E, 0x0000, "voc",          "VOC",          "",         1),
    (0x005B, 0x0000, "airquality",   "air quality",  "",         1),
    (0x0403, 0x0000, "pressure",     "pressure",     "hPa",      1),
    # OccupancySensing. The attribute is a BITMAP, not a boolean - bit 0 is
    # "occupied" - so a device that ever sets another bit would read as a
    # number here rather than a word, which is the honest failure.
    #
    # Above illuminance on purpose: this table's order decides which reading
    # headlines a tile, and a device that senses both is a motion sensor with a
    # light meter in it. "light 11 lx" is not what you look at it to find out.
    (0x0406, 0x0000, "occupancy",    "motion",       "",         1),
    (0x0400, 0x0000, "illuminance",  "light",        "lx",       1),
    # BooleanState - ONE cluster, several completely different meanings.
    #
    # A door sensor, a water leak detector, a freeze detector and a rain sensor
    # all report through 0x0045, and Matter tells them apart by DEVICE TYPE, not
    # by cluster. So the cluster alone cannot say what `true` means: on a door
    # it is "closed", on a leak detector it is water on your floor. Mapping the
    # cluster straight to "contact" showed a flood sensor as "contact: open",
    # which is a reading somebody would glance at and be reassured by.
    #
    # These four entries exist so the interface has a label and words for each
    # kind. Which one a given device gets is decided by boolean_kind(), from the
    # device's own descriptor.
    (0x0045, 0x0000, "contact",      "contact",      "",         1),
    (0x0045, 0x0000, "leak",         "water",        "",         1),
    (0x0045, 0x0000, "freeze",       "freeze",       "",         1),
    (0x0045, 0x0000, "rain",         "rain",         "",         1),
    # PowerSource, on ENDPOINT 0 - see read_measurements. A battery device that
    # will not say how much it has left is a device that stops one day without
    # warning.
    #
    # MIND THE ATTRIBUTE IDS. They are easy to get wrong by one, and getting
    # them wrong does not look like an error - it looks like a reading. Taking
    # 0x0C for the voltage, 0x0E for the percentage and 0x10 for the charge
    # level produced "200 mV, 0%, critical" for a cell measuring 1.6 V on a
    # multimeter, which is an entirely believable story about a dead battery.
    #   0x0B BatVoltage             mV
    #   0x0C BatPercentRemaining    HALF percent, 0..200
    #   0x0E BatChargeLevel         0 ok, 1 warning, 2 critical
    #   0x10 BatReplaceability      not a charge level at all
    (0x002F, 0x000C, "battery",      "battery",         "%",     0.5),
    (0x002F, 0x000E, "batteryState", "battery state",   "",      1),
    (0x002F, 0x000B, "batteryVolts", "battery voltage", "V",     0.001),
]

# Readings that are a state rather than a quantity. The index is the value:
# False is 0, True is 1.
#
# The direction is checked against hardware, not against the spec: an IKEA
# MYGGBETT with the two halves apart reports False. Guessing this wrong is worse
# than not showing it at all - a door reported shut while it is open is the one
# reading somebody would act on without looking.
# Readings that are read but not LISTED. They have a better presentation of
# their own - the battery is a gauge on the tile, where a bar says more at a
# glance than a row of digits - and repeating them as text turned a door
# sensor's sheet into four lines of battery and one line about the door.
# Everything is still in the property inspector, which is the view that promises
# to hide nothing.
MEASURE_HIDDEN = {"batteryState", "batteryVolts"}


def lux_from_measured(v):
    """Matter stores illuminance LOGARITHMICALLY, and it is easy to miss.

    MeasuredValue = 10000 x log10(lux) + 1, so a reading of 10414 is about
    11 lux and not 10414 of anything. Shown raw it looked like bright daylight
    in a room that was dim - a number too large to question rather than
    obviously broken.

    The device confirms the encoding itself: this sensor reports
    MinMeasuredValue 1 and MaxMeasuredValue 40001, which decode to 1 lux and
    10000 lux - a sane range for a light sensor, where 1..40001 lux would not be.

    0 is the spec's "too dark to measure", not "no reading".
    """
    if not isinstance(v, (int, float)) or v <= 0:
        return 0
    return round(10 ** ((v - 1) / 10000), 1)


# Readings that need more than a multiplier to become the thing they claim to
# be. Applied after `scale`, and only where the wire format is not the unit.
MEASURE_FN = {
    "illuminance": lux_from_measured,
}

MEASURE_WORDS = {
    "contact": ["open", "closed"],
    # Motion is not an emergency, so it is not shouted the way a leak is. And
    # the words are not the label repeated - "motion: motion" says nothing.
    "occupancy": ["clear", "detected"],
    # For these three, `true` is the thing you did not want to happen, so the
    # word is shouted. The panel is quiet everywhere else; this is the one place
    # a reading is an emergency.
    "leak":    ["dry", "LEAK"],
    "freeze":  ["clear", "FREEZING"],
    "rain":    ["dry", "raining"],
    # PowerSource BatChargeLevel: 0 OK, 1 Warning, 2 Critical.
    "batteryState": ["ok", "low", "critical"],
}

AIR_QUALITY_WORDS = ["unknown", "good", "fair", "moderate",
                     "poor", "very poor", "extremely poor"]

# The device types that get the light treatment - an ACL so a switch may command
# them, a place in a switch's binding table, and a place in the schedule.
#
# Nobody declares this at the form. A device is commissioned, asked what it is,
# and put in the right list; "is it a bulb?" is a question the bulb can answer
# and the person holding the box often cannot.
LIGHT_TYPES = {0x0100, 0x0101, 0x010C, 0x010D}


def describe_device(node: int, ms=None) -> dict:
    """Ask a device what it is: its endpoints, their types and their clusters.

    Everything else in the panel is told what a device is when it is added.
    This asks, which is the only way a generic "add anything" can work - and it
    is also the only description that cannot go stale, because it comes from the
    device rather than from whoever typed the form.
    """
    out = {"types": [], "endpoints": {}}
    # One cache read covers the whole device. This used to be one round trip for
    # PartsList and then another PER ENDPOINT, each of which had to reach the
    # device - up to nine waits on a sleepy one, which is why adding a battery
    # device took the best part of a minute and sometimes gave up half-described.
    attrs, _err = m_attrs(node, timeout=30.0, ms=ms)
    parts = m_get(attrs, 0, DESCRIPTOR_CLUSTER, DESC_PARTS_LIST_ATTR)
    eps = [int(e) for e in parts] if isinstance(parts, list) else []
    # Endpoint 0 is the node itself and holds no application device type worth
    # showing; if PartsList came back empty we still try endpoint 1, which is
    # where a single-function device puts everything.
    if not eps:
        eps = [1]

    for ep in eps[:8]:          # a sensor with more than eight parts is not a
        types = m_get(attrs, ep, DESCRIPTOR_CLUSTER, DESC_DEVICE_TYPE_ATTR)
        servers = m_get(attrs, ep, DESCRIPTOR_CLUSTER, DESC_SERVER_LIST_ATTR)
        ids = []
        for t in (types or []):
            # DeviceTypeStruct: "deviceType" named, or field 0 by id.
            tid = t.get("deviceType", t.get("0")) if isinstance(t, dict) else t
            if isinstance(tid, int):
                ids.append(tid)
        clusters = [int(c) for c in (servers or []) if isinstance(c, int)]
        if ids or clusters:
            out["endpoints"][str(ep)] = {"types": ids, "clusters": clusters}
        for tid in ids:
            if tid not in out["types"]:
                out["types"].append(tid)
    return out


# When a device declares several application types, this is the order we name
# it by. A device is called after what it is FOR: the MYGGSPRAY carries a light
# sensor on endpoint 1 and an occupancy sensor on endpoint 2, and calling it a
# "light sensor" because that endpoint came first describes the accessory rather
# than the product somebody bought.
TYPE_PRIORITY = (0x0107, 0x0015, 0x0043, 0x0041, 0x0044, 0x0076, 0x002C)


def device_type_name(desc: dict) -> str:
    """One phrase for what this is, for a tile that has no icon of its own.

    The generated table first, the hand-written one only for the few phrasings
    we prefer to the SDK's own - and neither is allowed to invent a name for an
    id it does not know, because a confident wrong label is worse than a number.
    """
    types = desc.get("types") or []
    types = sorted(types, key=lambda t: (TYPE_PRIORITY.index(t)
                                         if t in TYPE_PRIORITY else len(TYPE_PRIORITY)))
    for t in types:
        name = DEVICE_TYPE_NAMES.get(t) or MATTER_DEVICE_TYPES.get(str(t))
        if name:
            return name
    return f"device type 0x{types[0]:04X}" if types else "device"


def read_measurements(dev: dict) -> dict:
    """Every measurement this device exposes, from what it actually reports.

    Read from the node's own attribute set rather than from the descriptor we
    recorded when it was added, and the difference is not tidiness.

    The descriptor deliberately skips ENDPOINT 0 - that endpoint is the node
    itself and carries no application device type worth showing - and endpoint 0
    is exactly where PowerSource lives. So a battery reading could never be
    found by walking the descriptor, no matter what was added to MEASURED.

    It also means a device whose stored descriptor is stale, or which was added
    before a reading existed in the table, starts reporting it without having to
    be re-interviewed. The cost is nothing: this is one cache read either way.
    """
    held, _err = m_attrs(dev["node"])
    if not held:
        return {}

    # BooleanState is resolved per device, not from this table - see
    # boolean_kind - so it is deliberately left out of the lookup.
    want = {(c, a): (key, scale) for c, a, key, _lab, _unit, scale in MEASURED
            if c != BOOLEAN_CLUSTER}
    kind = boolean_kind(dev)
    out = {}
    # Sorted so that a device exposing the same cluster on several endpoints
    # always reports the lowest one, rather than whichever the dict happened to
    # yield last.
    for path in sorted(held, key=lambda p: [int(x) if x.isdigit() else 0
                                            for x in p.split("/")]):
        bits = path.split("/")
        if len(bits) != 3:
            continue
        try:
            _ep, cluster, attr = (int(b) for b in bits)
        except ValueError:
            continue
        val = held[path]
        if val is None:
            continue
        if (cluster, attr) == (BOOLEAN_CLUSTER, 0x0000):
            out.setdefault(kind, val)
            continue
        hit = want.get((cluster, attr))
        if hit is None:
            continue
        key, scale = hit
        if key in out:
            continue
        val = round(val * scale, 2) if scale != 1 else val
        fn = MEASURE_FN.get(key)
        out[key] = fn(val) if fn else val
    return out




# ColorControl (0x0300) feature bits, checked against cluster-enums.h.
CC_HUE_SAT = 0x01
CC_XY = 0x08
CC_COLOR_TEMP = 0x10


def detect_caps(node: int, endpoint: int = 1, ms=None) -> dict:
    """What the bulb can do: color temperature, RGB color, or white only.

    We read the FeatureMap on ColorControl. If the bulb does not have the
    cluster the request fails - and that failure is the answer.
    """
    attrs, err = m_attrs(node, ms=ms)
    fmap = m_get(attrs, endpoint, COLOR_CLUSTER_ID, 0xFFFC)
    if err or fmap is None:
        # Unknown, not "the bulb is plain white". The difference matters: a bulb
        # with color temperature reported as white loses half the schedule.
        return {"ct": None, "color": None}

    # chip-tool answered in nested JSON that had to be walked for the number;
    # here the attribute IS the number.
    fm = int(fmap)
    return {"ct": bool(fm & CC_COLOR_TEMP),
            "color": bool(fm & (CC_HUE_SAT | CC_XY)),
            "featureMap": fm}


def clusters_for(bulb: dict) -> tuple:
    """The clusters we bind for a bulb. ColorControl only if the bulb really has
    it - otherwise the color commands would fail with UNSUPPORTED_CLUSTER and
    fill up the log."""
    caps = bulb.get("caps") or {}
    out = [6, 8]  # OnOff, LevelControl
    if caps.get("ct") or caps.get("color"):
        out.append(768)  # ColorControl 0x0300
    return tuple(out)


def binding_entries(switch: dict, bulbs: list) -> list:
    """A switch's binding table, as a list. ALWAYS complete - the write replaces
    the entire contents, so a partial one would erase the rest."""
    entries = []
    for b in bulbs:
        for cluster in clusters_for(b):
            entries.append({"node": b["node"],
                            "endpoint": b.get("endpoint", 1),
                            "cluster": cluster})
    return entries




def verhoeff(number: str) -> int:
    """The check digit a Matter manual pairing code ends with."""
    d = [[0,1,2,3,4,5,6,7,8,9],[1,2,3,4,0,6,7,8,9,5],[2,3,4,0,1,7,8,9,5,6],
         [3,4,0,1,2,8,9,5,6,7],[4,0,1,2,3,9,5,6,7,8],[5,9,8,7,6,0,4,3,2,1],
         [6,5,9,8,7,1,0,4,3,2],[7,6,5,9,8,2,1,0,4,3],[8,7,6,5,9,3,2,1,0,4],
         [9,8,7,6,5,4,3,2,1,0]]
    perm = [[0,1,2,3,4,5,6,7,8,9],[1,5,7,6,2,8,3,0,9,4],[5,8,0,3,7,9,6,1,4,2],
            [8,9,1,6,0,4,3,5,2,7],[9,4,5,3,1,2,6,8,7,0],[4,2,8,6,5,7,3,9,0,1],
            [2,7,9,3,8,0,6,4,1,5],[7,0,4,6,9,1,3,2,5,8]]
    inverse = [0,4,3,2,1,5,6,7,8,9]
    c = 0
    for i, ch in enumerate(reversed(number)):
        c = d[c][perm[(i + 1) % 8][int(ch)]]
    return inverse[c]


def manual_code(passcode: int, discriminator: int) -> str:
    """The 11-digit code for a passcode and discriminator, per Matter 5.1.4.1.

    chip-tool took the two numbers separately on its command line. matter-server
    takes a pairing code, the way a phone would, so the panel has to produce one
    for the switch - whose credentials are compiled into the firmware and never
    printed on a label.

    Checked against the published CHIP test vector: passcode 20202021 with
    discriminator 3840 gives 34970112332. Worth checking rather than trusting,
    because a wrong Verhoeff digit produces a code that is rejected with no hint
    as to which of the two numbers was wrong.
    """
    top = (discriminator >> 10) & 0x03
    middle = ((discriminator & 0x300) << 6) | (passcode & 0x3FFF)
    tail = (passcode >> 14) & 0x1FFF
    body = f"{top:01d}{middle:05d}{tail:04d}"
    return body + str(verhoeff(body))


ADMIN_NODE = int(os.environ.get("ADMIN_NODE", "112233"))
# The test credentials compiled into the switch firmware. See ota/config.sh.
SWITCH_PASSCODE = os.environ.get("SWITCH_PASSCODE", "20202021")
SWITCH_DISCRIMINATOR = os.environ.get("SWITCH_DISCRIMINATOR", "3840")


def known_rooms(devices: dict) -> list:
    """The known rooms: the ones declared explicitly plus the ones devices use.

    THE DECLARED LIST IS THE ORDER, and it is returned in the order it is
    written. It used to be sorted here on the way out, which is why the page
    always read alphabetically no matter what the file said - and why arranging
    the rooms was impossible rather than merely missing.

    A room that only a device mentions is still listed, but after everything
    declared and alphabetically among its own kind: a room that turns up by
    itself belongs at the end, not in the middle of an arrangement somebody
    made. The comparison is case-insensitive, so 'Bedroom' does not show up next
    to 'bedroom'.
    """
    seen, out = {}, []
    for r in devices.get("rooms", []):
        r = (r or "").strip()
        if r and r.casefold() not in seen:
            seen[r.casefold()] = r
            out.append(r)
    # Every device, switches included. Reading only the bulbs meant a room
    # holding nothing but switches vanished from the list the moment you stopped
    # declaring it explicitly.
    extra = []
    for d in all_devices(devices):
        r = (d.get("where") or "").strip()
        if r and r.casefold() not in seen:
            seen[r.casefold()] = r
            extra.append(r)
    return out + sorted(extra, key=str.casefold)


RE_SVG_SIZE = re.compile(r'<svg\s+width="(\d+)"\s+height="(\d+)"')


def qr_svg(payload: str) -> str:
    """A scalable SVG for a Matter payload.

    segno emits <svg width="29" height="29"> with NO viewBox. Without a viewBox,
    enlarging the element from CSS does not scale the contents - the drawing
    stays 29 units wide in a corner. We replace the fixed dimensions with a
    viewBox, so CSS decides how big it is.
    """
    svg = segno.make(payload, error="m").svg_inline(
        scale=1, dark="#0a0b0d", light="#ffffff", border=2)

    m = RE_SVG_SIZE.search(svg)
    if m:
        w, h = m.group(1), m.group(2)
        svg = svg[:m.start()] + f'<svg viewBox="0 0 {w} {h}"' + svg[m.end():]
    return svg


RE_MANUAL = re.compile(r"Manual pairing code:\s*\[(\d+)\]")
RE_QR = re.compile(r"SetupQRCode:\s*\[(MT:[A-Z0-9.\-$%*+./:]+)\]")


# Why this is needed: over the WebSocket chip-tool returns nothing but
# "FAILURE". The real reason - which step failed and why - goes to stdout, so
# into the log. "FAILURE" on its own helps nobody: a bulb that was never reset,
# one that is too far away, and one rejected at attestation verification all
# look the same, and the three are fixed in completely different ways.
RE_FAILED_STEP = re.compile(
    r"Error on commissioning step '([A-Za-z0-9_]+)'.*?(CHIP Error 0x[0-9A-Fa-f]+: [^'\n\x1b]*)")
RE_GENERAL_FAILURE = re.compile(r"Device commissioning Failure: .*?(CHIP Error 0x[0-9A-Fa-f]+: [^'\n\x1b]*)")

# The step it failed at -> what that means, in the words of whoever has to fix it.
STEP_EXPLANATIONS = {
    "AttestationVerification":
        "the device was rejected at attestation verification. Its attestation "
        "root is missing from --paa-trust-store-path, or it is not signed by a "
        "known one. See deploy/paa-certs.sh",
    "AttestationRevocationCheck":
        "attestation verification did not pass (this step follows "
        "'AttestationVerification', which already failed)",
    "FindOperational":
        "it commissioned, but we could not find it on the Thread network "
        "afterwards - check the range to the border router",
    "WiFiNetworkEnable":
        "the device wants Wi-Fi, not Thread",
    "ThreadNetworkEnable":
        "the device could not join the Thread network with the dataset it was given",
    "SendNOC":
        "the device refused our operational certificate - usually it has no free "
        "fabric slot",
}








# ------------------------------------------------------------------- schedule
#
# The custom cluster 0xFFF1FC30 on endpoint 2 belongs to the switch. It was
# added to carry the schedule blob the switch used to execute; the switch no
# longer executes it, so all that is read and written here now is locked and
# role.
#
# The schedule itself moved to the Pi and is applied to the bulbs - see the
# section further down. The limits below are the ones the editor enforces.
SCHED_CLUSTER = "0xFFF1FC30"
SCHED_ENDPOINT = 2
LOCK_ATTR = "0xFFF10002"
ROLE_ATTR = "0xFFF10003"
SCHED_BLOB_VERSION = 1
# The editor divides the day into equal columns, so this is the finest
# resolution it can offer. It used to be 12 because the schedule was a blob
# written into the switch, which had a fixed-size table; the switch does not
# hold the schedule any more, so the only cost of a higher number is the writes
# the Pi makes at each boundary. 24 is an hour per column.
SCHED_MAX_POINTS = 24

# The curve the firmware used to ship with (kDefaults in the since-removed
# schedule.cpp). Shown in the editor until a schedule is saved on the Pi.
SCHED_DEFAULT = [
    # The recommended day, the same curve the panel's reset button loads.
    # Warm and nearly out overnight, cold and full over the middle of the
    # day, warming back to candle by bedtime. Levels are the Matter scale;
    # the percentages beside them are how bright each one LOOKS, which on
    # this hardware is simply level/254 - the bulb carries the curve.
    #
    # Nothing here goes below LEVEL_MIN. The overnight points used to be
    # level 1, chosen as "the dimmest a bulb can be": on the real bulb that
    # is OFF, so the recommended night setting switched the lamp out
    # instead of dimming it.
    {"min":    0, "level":  10, "fade": 20, "mireds": 454},   # 01:00     4%   2200 K
    {"min":  120, "level":  10, "fade": 20, "mireds": 454},   # 03:00     4%   2200 K
    {"min":  240, "level":  10, "fade": 20, "mireds": 454},   # 05:00     4%   2200 K
    {"min":  360, "level":  89, "fade": 20, "mireds": 370},   # 07:00    35%   2700 K
    {"min":  480, "level": 203, "fade": 20, "mireds": 250},   # 09:00    80%   4000 K
    {"min":  600, "level": 254, "fade": 20, "mireds": 217},   # 11:00   100%   4600 K
    {"min":  720, "level": 254, "fade": 20, "mireds": 208},   # 13:00   100%   4800 K
    {"min":  840, "level": 241, "fade": 20, "mireds": 222},   # 15:00    95%   4500 K
    {"min":  960, "level": 203, "fade": 20, "mireds": 263},   # 17:00    80%   3800 K
    {"min": 1080, "level": 152, "fade": 20, "mireds": 333},   # 19:00    60%   3000 K
    {"min": 1200, "level": 102, "fade": 20, "mireds": 370},   # 21:00    40%   2700 K
    {"min": 1320, "level":  38, "fade": 20, "mireds": 417},   # 23:00    15%   2400 K
]


def validate_schedule(points: list):
    """Reject an invalid schedule before it gets saved.

    The same rules as before, when the blob was written into the switch: points
    sorted, no duplicates, values inside the allowed ranges. They are checked
    here because the file on the Pi is now the only source, and a broken
    schedule would no longer be refused by anyone.
    """
    if not points:
        raise ValueError("the schedule is empty")
    if len(points) > SCHED_MAX_POINTS:
        raise ValueError(f"at most {SCHED_MAX_POINTS} points, got {len(points)}")

    seen = set()
    last = -1
    for p in points:
        m = int(p.get("min", -1))
        if not 0 <= m < 1440:
            raise ValueError(f"minute outside the day: {m}")
        if m in seen:
            raise ValueError(f"two points at the same minute: {m}")
        if m <= last:
            raise ValueError("the points must be in increasing order")
        seen.add(m)
        last = m

        lvl = int(p.get("level", 0))
        if not 1 <= lvl <= 254:
            raise ValueError(f"level outside the range 1-254: {lvl}")
        mir = int(p.get("mireds", 0))
        if mir and not 100 <= mir <= 700:
            raise ValueError(f"color temperature out of range: {mir} mireds")



# ----------------------------------------------------------------------- lock
#
# Two small pieces of state on every switch, in the same custom cluster as the
# schedule:
#   Locked  - the switch ignores presses and turns its status LED fully off
#   Role    - 0 light, 1 lock
#
# Both live IN the switch, not here: if you lock the switches and the power
# fails, you do not want them unlocking themselves when it comes back.
ROLE_LIGHT, ROLE_LOCK = 0, 1
ROLE_NAMES = {ROLE_LIGHT: "light", ROLE_LOCK: "lock"}


def set_lock(node: int, locked: bool) -> dict:
    log(f"node {node}: {'locking' if locked else 'unlocking'}", "step")
    # A real bool, not 1/0: the attribute is BOOLEAN in the firmware
    # (DECLARE_DYNAMIC_ATTRIBUTE(kLockedAttr, BOOLEAN, ...)) and the device
    # refuses an integer with CONSTRAINT_ERROR. It cost an afternoon on the old
    # path, where the value went over the wire as whatever chip-tool made of the
    # word on its command line.
    e = m_write(node, SCHED_ENDPOINT, SCHED_CLUSTER_ID, _ATTRS["locked"],
                bool(locked), timeout=60.0)
    if e:
        return {"node": node, "ok": False, "error": e}

    # Read it back. A confirmed write does not guarantee the value landed, and
    # the mistake is expensive here: you would believe you locked and you did
    # not.
    back, _ = m_read(node, SCHED_ENDPOINT, SCHED_CLUSTER_ID, _ATTRS["locked"])
    if back is not None and bool(back) != locked:
        log(f"node {node}: the write went through but the value reads back as "
            f"{back} - it is NOT locked", "err")
        return {"node": node, "ok": False, "error": "the value did not apply"}

    log(f"node {node}: {'locked' if locked else 'unlocked'}", "ok")
    state_put(node, values={"locked": locked},
              meta={"readAt": time.time(), "okAt": time.time(),
                                     "ok": True, "err": None})
    return {"node": node, "ok": True, "locked": locked}


def set_role(node: int, role: int) -> dict:
    log(f"node {node}: role -> {ROLE_NAMES.get(role, role)}", "step")
    e = m_write(node, SCHED_ENDPOINT, SCHED_CLUSTER_ID, _ATTRS["role"],
                int(role), timeout=60.0)
    if e:
        return {"node": node, "ok": False, "error": e}
    state_put(node, values={"role": role},
              meta={"readAt": time.time(), "okAt": time.time(),
                                     "ok": True, "err": None})
    return {"node": node, "ok": True, "role": role}






# ------------------------------------------------------- state kept on the Pi
#
# Why this exists. The switch is a sleepy device: it does not listen
# continuously, it wakes about every 15 s to pick up its messages. A read does
# not leave "now", it waits for the switch's next wake-up. Measured on the real
# install, a page load that re-read everything from the device cost ~27 seconds
# of waiting - for data that only changes when we change it ourselves.
#
# So the Pi holds the state and the panel answers from it instantly. Reading
# from the device happens in the background: at startup, after each write of
# ours, and rarely otherwise.
#
# How rarely: the binding table, the role and the schedule never change on their
# own - only we write them. The one thing that can change behind our back is the
# locked state, when a switch in the lock role toggles it. That one is updated
# immediately anyway, by the very write that produces it. So the long cycle is
# only a safety net, and it is worth keeping rare: every read is radio traffic
# to a battery-powered device.
STATE_FILE = pathlib.Path(
    os.environ.get("PANEL_STATE", str(HERE.parent / "ota" / "state" / "panel-state.json")))
REFRESH_SEC = int(os.environ.get("PANEL_REFRESH_SEC", str(6 * 3600)))

SCHED_CLUSTER_ID = int(SCHED_CLUSTER, 16)
BINDING_CLUSTER_ID = 0x1E
_ATTRS = {name: int(val, 16) for name, val in (
    ("locked", LOCK_ATTR), ("role", ROLE_ATTR))}

_state_lock = threading.Lock()
_state = {"rev": 0, "nodes": {}}
_state_wake = threading.Event()
_matter_ok = True   # did the last attempt to talk to matter-server succeed?


# Held across the whole of state_save. ALWAYS taken before _state_lock, never
# the other way round.
_save_lock = threading.Lock()


def state_load():
    try:
        d = json.loads(STATE_FILE.read_text())
    except OSError:
        return                      # first boot: nothing saved yet, and that is fine
    except ValueError as exc:
        # A file that exists and will not parse is a different thing entirely,
        # and silence about it is how a lost schedule memo and a lost hold get
        # blamed on something else. Keep the wreckage; it is small and it is the
        # only evidence.
        try:
            STATE_FILE.replace(STATE_FILE.with_name(STATE_FILE.name + ".bad"))
        except OSError:
            pass
        log(f"the state file would not parse, starting empty (kept as "
            f"{STATE_FILE.name}.bad): {exc}", "err")
        return
    if isinstance(d, dict) and isinstance(d.get("nodes"), dict):
        _state["nodes"] = d["nodes"]
        _state["rev"] = int(d.get("rev") or 0)
        # The schedule's memo of what it last wrote, and which lights are being
        # held by hand. It was saved and never loaded, so a restart made the
        # schedule believe it had written nothing - it rewrote every bulb on the
        # first tick, and any hold was silently forgotten.
        if isinstance(d.get("bulbs"), dict):
            _state["bulbs"] = d["bulbs"]


def state_save():
    """Write the state out, atomically, from any thread.

    Three things have to be true at once, and the first version had none of them.

    ONE WRITER AT A TIME. Every saver used the same fixed `.tmp` path with no
    lock, and there are seven call sites across the matter link, the schedule
    tick, the refresher and the HTTP threads. Two savers then share one file:
    B truncates the tmp A is about to rename, so A publishes a half-written
    document - and B's own rename afterwards fails with ENOENT, because A
    already moved the inode away. The visible half was a warning in the log; the
    silent half defeated the atomic write this function exists to provide, and
    left a state file that state_load could only discard - taking the schedule's
    memo and every hold with it.

    A CONSISTENT SNAPSHOT. json.dumps walked the live `_state` with no lock while
    other threads inserted into it. Besides serialising a torn picture, that can
    raise RuntimeError("dictionary changed size during iteration"), which is not
    an OSError and so escaped the handler below entirely.

    A NAME OF ITS OWN. The lock settles it inside one process; a unique name also
    covers two panels overlapping across a restart.
    """
    with _save_lock:
        try:
            with _state_lock:
                blob = json.dumps(_state, ensure_ascii=False)
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_name(
                f"{STATE_FILE.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                tmp.write_text(blob)
                tmp.replace(STATE_FILE)
            finally:
                # A kill between write and replace would otherwise leave one
                # behind per thread, for ever.
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        except OSError as exc:  # noqa: BLE001
            log(f"cannot save the state to disk: {exc}", "warn")


def state_of(node) -> dict:
    with _state_lock:
        return dict(_state["nodes"].get(str(node), {}))


# Woken whenever a value actually changes. It is what lets the browser wait for
# news instead of asking for it on a timer: a poll interval is a floor on how
# late you can be, and picking one is choosing between traffic and lag. Waiting
# costs neither.
_state_bump = threading.Condition(_state_lock)


def state_rev() -> int:
    with _state_lock:
        return _state["rev"]


def wait_for_change(since: int, timeout: float) -> int:
    """Block until the state moves past `since`, or the timeout runs out.

    Returns either way; the caller sends whatever is current. A long poll that
    times out is not a failure, it is "nothing happened", and the browser simply
    asks again.
    """
    end = time.monotonic() + timeout
    with _state_bump:
        while _state["rev"] <= since:
            left = end - time.monotonic()
            if left <= 0:
                break
            _state_bump.wait(left)
        return _state["rev"]


def state_put(node, values: dict = None, meta: dict = None) -> list:
    """Update one node's state.

    The revision counter goes up ONLY when a value actually differs from what we
    had. That is the entire mechanism by which the UI learns something changed:
    it does not care that the Pi did a read, only whether the read came back
    different.

    `values` takes part in the comparison, `meta` does not - the time of the last
    read changes on every attempt and would make the counter useless.
    """
    changed = []
    with _state_lock:
        cur = _state["nodes"].setdefault(str(node), {})
        for key, val in (values or {}).items():
            if cur.get(key) != val:
                cur[key] = val
                changed.append(key)
        cur.update(meta or {})
        if changed:
            _state["rev"] += 1
            _state_bump.notify_all()
    if changed:
        log(f"node {node}: {', '.join(changed)} changed", "ok")
        # Only when something actually differs. The meta fields - readAt, okAt,
        # ok, err - are liveness, regenerated by the first read after a restart,
        # and saving for them alone meant a whole-file write for every pushed
        # report from every device: tens a minute with nobody watching.
        state_save()
    return changed






def read_switch_state(node: int, endpoint: int = 1) -> dict:
    """A switch's entire state in a SINGLE request.

    Three paths: the binding table on endpoint 1, plus locked and role from our
    cluster on endpoint 2.

    Batching them mattered a great deal on the old path, and matters not at all
    on this one. Through chip-tool these were three reads that had to reach a
    sleepy switch, and sequential reads do not land on the same wake-up - three
    wake-ups is ~45 s against ~15 s for one. Here every value is already in
    matter-server's cache, kept current by its subscription, so none of them
    touches the radio and the whole read returns in milliseconds.
    """
    return m_attrs(node, timeout=30.0)


def refresh_switch(sw: dict) -> list:
    """Read a switch and update the state. Returns which facets changed."""
    global _matter_ok
    node = sw["node"]
    endpoint = sw.get("endpoint", 1)
    attrs, err = read_switch_state(node, endpoint)

    if attrs is None:
        _matter_ok = False
        state_put(node, meta={"readAt": time.time(), "ok": False, "err": err})
        return []
    _matter_ok = True

    # None = we did not find out. Different from [] or false, which are answers.
    binding_raw = m_get(attrs, endpoint, BINDING_CLUSTER_ID, 0x0000)
    locked_raw = m_get(attrs, SCHED_ENDPOINT, SCHED_CLUSTER_ID, _ATTRS["locked"])
    role_raw = m_get(attrs, SCHED_ENDPOINT, SCHED_CLUSTER_ID, _ATTRS["role"])
    values = {}
    if binding_raw is not None:
        # Translated back into our numbering. The device answers in the node ids
        # of the fabric that asked, so an untranslated table would compare
        # against devices.json and match nothing - a bound switch reported as
        # controlling no bulbs at all.
        values["binding"] = translate_binding(binding_raw)
    if locked_raw is not None:
        values["locked"] = bool(locked_raw)
    if isinstance(role_raw, int):
        values["role"] = int(role_raw)

    # None of the paths answered = the switch did not answer. We keep the old
    # values but mark the read as failed: better "unknown since when" than
    # reporting zero bindings for a switch that is asleep.
    ok = bool(values)
    return state_put(node, values=values,
                     meta={"readAt": time.time(), "ok": ok,
                           "err": None if ok else "no response"})


# ----------------------------------------------- what the bulbs are doing now
#
# The tiles show on/off, so the panel has to know it. Nothing else here did:
# every other value we hold is one WE wrote, and could therefore be cached for
# hours. A bulb's on/off is the opposite - the wall switch sends Toggle straight
# to the bulb over Thread, the Pi never sees it, so our copy is wrong within a
# second of anybody touching a switch.
#
# So this one is read live, and the freshness rules are different:
#
#   - it is only read while somebody is actually looking at the panel. The page
#     asks; nothing asks on a timer. Close the tab and the reads stop, and
#     chip-tool goes back to being frozen.
#   - several browsers, or a page that reloads twice, are coalesced: a read
#     newer than BULB_TTL_SEC is served from memory.
#   - a bulb that did not answer is not asked again for BULB_COLD_SEC. Otherwise
#     one unplugged bulb would hold the chip-tool lock for its whole timeout on
#     every poll, and the panel would feel broken for every other device.
#
# This is cheap in a way the switch reads are not: bulbs are mains-powered Thread
# routers, always listening, and answer in about 40 ms.
BULB_TTL_SEC = int(os.environ.get("PANEL_BULB_TTL_SEC", "12"))
# How long a device may fail to answer before the panel says so. Long enough to
# ride out the ordinary misses of a sleepy device, short enough that a device
# genuinely gone is reported while you still care.
# The longest a page load may spend asking devices for readings. Past this the
# rest are served from what we already know.
BULB_SWEEP_MAX = float(os.environ.get("PANEL_BULB_SWEEP_MAX", "6"))

SLEEPY_GRACE = int(os.environ.get("PANEL_SLEEPY_GRACE", "150"))
BULB_COLD_SEC = int(os.environ.get("PANEL_BULB_COLD_SEC", "300"))
BULB_READ_TIMEOUT = float(os.environ.get("PANEL_BULB_TIMEOUT", "15"))

ONOFF_CLUSTER_ID = 0x06
LEVEL_CLUSTER_ID = 0x08
LEVEL_ONLEVEL_ATTR = 0x0011
COLOR_CLUSTER_ID = 0x0300
COLOR_TEMP_ATTR = 0x0007
COLOR_CT_MIN_ATTR = 0x400B   # ColorTempPhysicalMinMireds - the COOLEST it goes
COLOR_CT_MAX_ATTR = 0x400C   # ...MaxMireds - the warmest

_bulb_lock = threading.Lock()


def refresh_bulb(bulb: dict) -> list:
    """Read one bulb's on/off, brightness and colour in a single request.

    Colour is in here because brightness alone does not tell you how much light
    there is: a tunable-white bulb at 2200 K is running one channel of its LEDs
    and puts out a fraction of what the same level does at 4000 K. "Level 254
    and still dim" is a sentence that only makes sense once you can see both.

    A bulb with no ColorControl simply answers nothing for that path, which
    costs nothing - the paths that did answer are unaffected.
    """
    node = bulb["node"]
    endpoint = int(bulb.get("endpoint", 1))
    attrs, err = m_attrs(node)
    on = m_get(attrs, endpoint, ONOFF_CLUSTER_ID, 0)
    level = m_get(attrs, endpoint, LEVEL_CLUSTER_ID, 0)
    onlevel = m_get(attrs, endpoint, LEVEL_CLUSTER_ID, LEVEL_ONLEVEL_ATTR)
    mireds = m_get(attrs, endpoint, COLOR_CLUSTER_ID, COLOR_TEMP_ATTR)
    ct_min = m_get(attrs, endpoint, COLOR_CLUSTER_ID, COLOR_CT_MIN_ATTR)
    ct_max = m_get(attrs, endpoint, COLOR_CLUSTER_ID, COLOR_CT_MAX_ATTR)

    if on is None:
        # No answer. We keep the last value rather than showing "off" for a bulb
        # that is merely out of range - "stale" is honest, "off" is a lie you
        # would act on.
        return state_put(node, meta={"readAt": time.time(), "ok": False,
                                     "err": err or "no response"})

    values = {"on": bool(on)}
    if isinstance(level, int):
        values["level"] = int(level)
    if isinstance(mireds, int) and mireds:
        values["mireds"] = int(mireds)
    # What an On command will bring it to. It is the number every "why did it
    # come up dim" question is actually about, and until now it was the one
    # number nothing in the panel could show.
    if isinstance(onlevel, int):
        values["onlevel"] = int(onlevel)
    # How far this particular bulb can actually go. Without it the colour
    # control offers travel the bulb cannot reach: this one stops at 454 mireds
    # and silently clamps anything warmer, so the bottom eighth of the slider
    # did nothing and the value sprang back the moment it was read again.
    if isinstance(ct_min, int) and ct_min:
        values["ctMin"] = int(ct_min)
    if isinstance(ct_max, int) and ct_max:
        values["ctMax"] = int(ct_max)
    # Whoever turned it off - the panel, the wall switch, a power cut - this is
    # where the panel finds out, so this is where a hold ends.
    note_power(node, bool(on))

    return state_put(node, values=values,
                     meta={"readAt": time.time(), "okAt": time.time(),
                                     "ok": True, "err": None})


# ---------------------------------------------------------------- subscriptions
#
# Polling is the wrong shape for a battery sensor and no amount of tuning fixes
# it. A read does not wake the device: it queues at its Thread parent and is
# delivered when the device next polls, so OUR interval is a floor we cannot get
# under, and a read that arrives while it is asleep simply fails. A door sensor
# whose whole job is to report the instant a window opens was arriving tens of
# seconds late, or not at all.
#
# Matter's answer is a subscription: the device reports when the value changes,
# on its own schedule, and stays quiet otherwise. It is both faster and kinder to
# the battery than any polling.
#
# matter-server holds those subscriptions - to every attribute on every node -
# and the panel receives what they report over one socket. See matter_link.py.
# What is left here is the bookkeeping that decides whether a node is being
# heard from, and therefore whether the ordinary poller should leave it alone.
#
# This used to be attempted through chip-tool and it did not work, in a way
# worth remembering: `subscribe-by-id` on its interactive server behaves like a
# one-shot read - the command returns the current value as its result and the
# subscription is torn down with it. The priming report looks exactly like a
# subscription working, and nothing ever follows. Worse than polling rather than
# merely no better, because a node believed to be subscribed is not polled, so
# its value freezes at the priming report for ever. A door sensor stuck on
# "closed" is exactly the reading somebody acts on without going to look.
#
# Hence SUB_SILENCE below: liveness is fed only by data ACTUALLY ARRIVING. If
# matter-server dies or a subscription lapses, the node goes quiet, falls out of
# `subscribed()`, and the poller picks it up again. A broken link costs latency,
# never a stale value presented as current.

# Which readings are an EVENT rather than a quantity. A temperature that drifts
# is fine on a poll; a door is not, and a flood certainly is not.
SUB_KEYS = {"contact", "leak", "freeze", "rain", "occupancy"}

# BooleanState's meaning, by the device type that carries it. Matter device
# types, from the Device Library.
BOOLEAN_CLUSTER = 0x0045

BOOLEAN_KINDS = {
    0x0015: "contact",   # Contact Sensor        true = closed
    0x0041: "freeze",    # Water Freeze Detector true = freezing
    0x0043: "leak",      # Water Leak Detector   true = water present
    0x0044: "rain",      # Rain Sensor           true = raining
}


def boolean_kind(dev: dict) -> str:
    """What this device's BooleanState is ABOUT.

    Falls back to "contact", which is what every device answered before there
    was anything else - and being wrong in that direction is the safe way round,
    because an unknown sensor reading "contact" is obviously odd, while a flood
    reading "closed" is quietly reassuring.
    """
    types = set((dev.get("desc") or {}).get("types") or [])
    for type_id, kind in BOOLEAN_KINDS.items():
        if type_id in types:
            return kind
    return "contact"

# node -> when it last told us something. Not a flag: a subscription that has
# gone quiet has to hand the node back to the poller, or a silent failure
# freezes the value for ever.
_subscribed = {}
_sub_lock = threading.Lock()
# How long a subscription may say nothing before we stop trusting it. Twice the
# keep-alive: a healthy one reports at least that often even when nothing
# changes.
SUB_SILENCE = int(os.environ.get("PANEL_SUB_SILENCE", str(2 * 600)))


def subscribed(node) -> bool:
    """Is this node covered by a subscription that is still talking?"""
    with _sub_lock:
        last = _subscribed.get(int(node))
    return last is not None and (time.time() - last) < SUB_SILENCE


def sub_heard(node):
    with _sub_lock:
        _subscribed[int(node)] = time.time()




# How often to ask a device whose reading is an EVENT. Not the same question as
# how often to ask a thermometer: a door that opened two minutes ago is news
# nobody can use.
#
# Cheap in practice. The read does not wake the device - it waits at its Thread
# parent and is picked up on the poll the device was going to make anyway - so
# this buys latency rather than spending battery. Measured on the MYGGBETT,
# answers come back in 0.4-6.5 s, which is the real floor here.
EVENT_POLL_SEC = int(os.environ.get("PANEL_EVENT_POLL_SEC", "5"))


def is_event_device(dev: dict) -> bool:
    """Does this device report anything that is an event rather than a level?"""
    desc = dev.get("desc") or {}
    for info in (desc.get("endpoints") or {}).values():
        have = set(info.get("clusters") or [])
        for cluster, _attr, key, _lab, _unit, _scale in MEASURED:
            if key in SUB_KEYS and cluster in have:
                return True
    return False


def event_watch():
    """Poll event sensors on a tight loop, in the background.

    Server side on purpose. It used to depend entirely on a browser asking, and
    a browser that nobody has touched for three minutes asks slowly - so the one
    case that matters, somebody standing in front of the panel watching a door,
    was the case that updated slowest. Now the Pi keeps these current whether
    anything is looking or not, and the page only decides how quickly it draws
    what the Pi already knows.
    """
    while True:
        try:
            for dev in load_devices().get("devices", []):
                if not is_event_device(dev) or subscribed(dev["node"]):
                    continue
                with _bulb_lock:
                    refresh_device(dev)
        except Exception as exc:  # noqa: BLE001 - one bad read does not stop the loop
            log(f"event watch: {exc}", "warn")
        time.sleep(EVENT_POLL_SEC)


# ------------------------------------------------------- python-matter-server
#
# The read path for devices that must not be polled. See matter_link.py for why
# polling cannot be prompt for a battery sensor, and panel/README.md for the
# shape of the migration - chip-tool still does everything else.
MATTER_WS = os.environ.get("MATTER_WS", "ws://127.0.0.1:5580/ws")
MATTER_LINK = os.environ.get("PANEL_MATTER_LINK", "1").lower() not in ("0", "no", "off")


# The command half. Same process, second socket - see MatterCall for why it is
# not shared with the listener.
MS = MatterCall(MATTER_WS, log)


def ms_of(node) -> int | None:
    """Our node id -> matter-server's, or None if it was never commissioned there."""
    for dev in all_devices(load_devices()):
        if int(dev.get("node", 0)) == int(node):
            ms = dev.get("msNode")
            return int(ms) if ms is not None else None
    return None


# chip-tool took the action as a word on the command line; the SDK wants the
# command class by name.
ONOFF_CMD = {"on": "On", "off": "Off", "toggle": "Toggle"}


def m_cmd(node, endpoint, cluster, name, payload=None, timeout=60.0):
    """Send one cluster command. Returns an error string, or None on success.

    The return convention matches `chip_error(chip(...))` on purpose, so a call
    site converts by swapping two lines rather than being restructured.
    """
    ms = ms_of(node)
    if ms is None:
        return f"node {node} is not on matter-server"
    try:
        MS.call("device_command", {"node_id": ms, "endpoint_id": int(endpoint),
                                   "cluster_id": int(cluster),
                                   "command_name": name,
                                   "payload": payload or {}}, timeout=timeout)
        return None
    except MatterError as exc:
        return str(exc)


def m_write(node, endpoint, cluster, attr, value, timeout=45.0):
    """Write one attribute. Same return convention as m_cmd."""
    ms = ms_of(node)
    if ms is None:
        return f"node {node} is not on matter-server"
    try:
        MS.call("write_attribute",
                {"node_id": ms,
                 "attribute_path": f"{int(endpoint)}/{int(cluster)}/{int(attr)}",
                 "value": value}, timeout=timeout)
        return None
    except MatterError as exc:
        return str(exc)


# Which fabric matter-server is on, as the DEVICE numbers them. chip-tool
# commissioned everything first and took index 1; matter-server joined second
# and is 2. It matters because Binding and ACL are both fabric-scoped: an entry
# written under one fabric index is invisible to - and unusable by - the other.
# That is not a detail, it is the whole shape of this migration. The switch's
# existing bindings live on fabric 1 and read back as an EMPTY table here, which
# is exactly what a switch that controls nothing also looks like.
MS_FABRIC = int(os.environ.get("MS_FABRIC_INDEX", "2"))


def our_of(ms_node) -> int | None:
    """matter-server's node id -> ours. The inverse of ms_of.

    Needed because a binding table read back from a device is expressed in the
    node ids of the fabric that read it. Everything else in the panel - the
    schedule, devices.json, the UI - speaks our ids, so the translation happens
    here rather than being scattered through the comparisons.
    """
    for dev in all_devices(load_devices()):
        if dev.get("msNode") is not None and int(dev["msNode"]) == int(ms_node):
            return int(dev["node"])
    return None


def write_acl(node, subject_nodes, timeout=60.0):
    """Who may drive this device: the admin, plus the listed switches.

    Manage (4), not Operate (3). On/off would work with Operate, but writing
    StartUpOnOff and OnLevel needs Manage, and a switch that can turn a lamp on
    but cannot say what brightness it comes up at is half a switch.

    The whole attribute is replaced, so the admin entry has to be restated every
    time. Leaving it out removes our own access to the device, and the only way
    back from that is a factory reset.
    """
    ms = ms_of(node)
    if ms is None:
        return f"node {node} is not on matter-server"
    subjects = [m for m in (ms_of(n) for n in subject_nodes) if m is not None]
    entries = [{"privilege": 5, "authMode": 2, "subjects": [ADMIN_NODE],
                "targets": None, "fabricIndex": MS_FABRIC}]
    if subjects:
        entries.append({"privilege": 4, "authMode": 2, "subjects": subjects,
                        "targets": None, "fabricIndex": MS_FABRIC})
    try:
        MS.call("set_acl_entry", {"node_id": ms, "entry": entries}, timeout=timeout)
        return None
    except MatterError as exc:
        return str(exc)


def write_binding(switch, entries, timeout=60.0):
    """A switch's binding table. Always the complete table - this replaces it."""
    ms = ms_of(switch["node"])
    if ms is None:
        return f"node {switch['node']} is not on matter-server"
    targets = []
    for e in entries:
        target = ms_of(e["node"])
        if target is None:
            return f"binding target {e['node']} is not on matter-server"
        targets.append({"node": target, "group": None,
                        "endpoint": int(e.get("endpoint", 1)),
                        "cluster": int(e["cluster"]), "fabricIndex": MS_FABRIC})
    try:
        MS.call("set_node_binding",
                {"node_id": ms, "endpoint": int(switch.get("endpoint", 1)),
                 "bindings": targets}, timeout=timeout)
        return None
    except MatterError as exc:
        return str(exc)


def translate_binding(raw) -> list:
    """A raw binding table in matter-server's node ids -> ours."""
    out = []
    for e in raw or []:
        # By field id, which is how the cache holds a struct: 1=node,
        # 3=endpoint, 4=cluster.
        target = e.get("node", e.get("1"))
        ours = our_of(target) if target is not None else None
        out.append({"node": ours if ours is not None else target,
                    "endpoint": e.get("endpoint", e.get("3", 1)),
                    "cluster": e.get("cluster", e.get("4"))})
    return out


def read_binding(node, endpoint=1):
    """A switch's binding table, in OUR node ids. [] means it is bound to nothing."""
    attrs, err = m_attrs(node)
    if attrs is None:
        return None, err
    raw = m_get(attrs, endpoint, BINDING_CLUSTER_ID, 0x0000)
    if raw is None:
        return None, "no binding table"
    return translate_binding(raw), None


# Cluster and attribute names, generated from the Matter SDK by
# scripts/gen-matter-names.py. 140 clusters and 1854 attributes: hand-writing
# that would be wrong within a release, and the SDK already knows all of it.
# Absent, the inspector still works and simply shows numbers.
NAMES_FILE = HERE / "matter_names.json"
try:
    _NAMES = json.loads(NAMES_FILE.read_text())
except (OSError, ValueError):
    _NAMES = {}
MATTER_NAMES = _NAMES.get("clusters") or {}
# Device type ids -> names, generated too. A hand-written table had four of
# these wrong in one block - 0x0041..0x0044 were labelled as CO2, CO, PM1 and
# PM2.5 sensors when they are actually the water freeze detector, water valve,
# water leak detector and rain sensor - and nothing about a wrong name looks
# wrong. It read "PM1 sensor" on a flood sensor and nobody blinked.
MATTER_DEVICE_TYPES = _NAMES.get("deviceTypes") or {}


# The attribute ids every cluster carries. They are noise in an inspector that
# is trying to show what a device DOES, so they go last and are marked.
GLOBAL_ATTRS = {0xFFF8, 0xFFF9, 0xFFFA, 0xFFFB, 0xFFFC, 0xFFFD}


# What the device says it is doing during an update. From the Matter spec's
# OtaSoftwareUpdateRequestor UpdateStateEnum.
OTA_STATES = {
    0: "unknown", 1: "idle", 2: "checking", 3: "waiting to download",
    4: "downloading", 5: "applying", 6: "waiting to apply",
    7: "rolling back", 8: "waiting for consent",
}

# Checking means asking the Distributed Compliance Ledger over the internet, so
# it is slow and worth remembering. An hour is far shorter than the interval at
# which a vendor publishes firmware.
_fw_cache = {}
FW_TTL = 3600
_fw_lock = threading.Lock()

# Nodes with an update running. Held HERE rather than in the browser, because
# the question "is this already updating" has one answer for the whole house:
# a flag in one tab does not stop a second tab, or the same tab after a reload,
# from offering the button again and starting a second update.
#
# It also covers the gap the browser cannot see. The device stays `idle` for a
# while after the request - the image has to be fetched before it is even
# offered - and during that window nothing in the device's own state says an
# update is under way, so the button came back and invited a second press.
_ota_running = set()

# One line of truth per node about an update, composed HERE rather than in the
# browser. The interface was assembling it from three sources - our "is it
# running" flag, the device's UpdateState and its progress - and got it wrong in
# the way that matters: it showed "preparing" throughout and then, when the
# attempt ended, silently put the button back with no word about what had
# happened. A failed update that looks exactly like an update never started is
# the one outcome worth ruling out by construction.
_ota_status = {}

# The last progress figure seen for a node, and when. A stalled transfer keeps
# reporting the percentage it reached - the device said "downloading 13%" once
# and then stopped answering entirely - so a row that just echoes the device
# looks like an update still going somewhere. It is not.
_ota_seen = {}
OTA_STALL = 180


def ota_note(node, phase, text):
    _ota_status[node] = {"phase": phase, "text": text, "at": time.time()}
    log(f"node {node}: {text}", "err" if phase == "failed" else "step")


def ota_status(node) -> dict:
    """What to show for this node: the device's own words while it is working,
    and the verified outcome once it is not."""
    st = _ota_status.get(node)
    if node in _ota_running:
        s = state_of(node)
        code = s.get("otaState")
        if isinstance(code, int) and code > 1:
            pct = s.get("otaProgress")
            word = OTA_STATES.get(code, "updating")
            seen, now = _ota_seen.get(node), time.time()
            if not seen or seen[0] != pct:
                _ota_seen[node] = (pct, now)
            elif now - seen[1] > OTA_STALL:
                mins = int((now - seen[1]) / 60)
                return {"phase": "running",
                        "text": f"stuck at {pct}% for {mins} min - "
                                f"the device stopped answering"}
            return {"phase": "running",
                    "text": word + (f" {pct}%" if isinstance(pct, int) else "")}
        return {"phase": "running", "text": "preparing the update\u2026"}
    # Outcomes are worth showing for a while after the fact, and then not.
    if st and time.time() - st["at"] < 900:
        return {"phase": st["phase"], "text": st["text"]}
    return {"phase": "idle", "text": ""}


def all_nodes() -> list:
    """Every node this house owns, of any kind."""
    try:
        d = load_devices()
    except (OSError, ValueError):
        return []
    out = []
    for key in ("switches", "bulbs", "devices"):
        for item in d.get(key) or []:
            n = item.get("node")
            if isinstance(n, int):
                out.append(n)
    return out


def firmware_offer(node):
    """Is an update on offer? From cache ONLY - this never blocks.

    The tiles ask about every device at once, and finding out for real means a
    request to the Distributed Compliance Ledger over the internet. A page that
    waits on seven of those before it can draw is a page that looks broken. So
    the answer here is whatever the background sweep has already learned, and
    None - draw nothing - until it has.
    """
    with _fw_lock:
        hit = _fw_cache.get(node)
    if not hit or time.time() - hit[0] > FW_TTL:
        return None
    return bool(hit[1].get("available"))


def firmware_watch():
    """Keep the firmware cache warm, slowly and in the background.

    Cheap on the radio: the installed version comes from matter-server's
    subscription cache, so the only cost is the ledger lookup. Spread out
    anyway - there is no hurry about a figure that changes twice a year, and
    nothing here should compete with a transfer that is already running.
    """
    time.sleep(20)
    while True:
        for node in all_nodes():
            if not _ota_running:
                try:
                    firmware_for(node)
                except Exception as exc:  # noqa: BLE001 - one node, not the sweep
                    log(f"node {node}: firmware check failed: {exc}", "warn")
            time.sleep(20)
        time.sleep(300)


def fw_live(node, out) -> dict:
    """A cached firmware answer, plus the two fields that must never be cached.

    Whether an update is running, and what it is doing, change by the second;
    what the ledger offers changes about twice a year. Folding the first into
    the second's hour-long cache froze the status line - the interface showed
    "up to date, [update]" throughout a running update, and then held the last
    words of the attempt for an hour after it ended.
    """
    d = dict(out)
    d["updating"] = node in _ota_running
    d["status"] = ota_status(node)
    return d


def radio_up_for() -> float:
    """Seconds since otbr-agent last started, or -1 if it cannot be told."""
    try:
        out = subprocess.run(
            ["systemctl", "show", "otbr-agent",
             "-p", "ActiveEnterTimestampMonotonic", "--value"],
            capture_output=True, text=True, timeout=5).stdout.strip()
        return time.monotonic() - int(out) / 1e6
    except Exception:  # noqa: BLE001 - a missing answer is not worth a failure
        return -1.0


def firmware_for(node, fresh=False) -> dict:
    """What is installed on a node, and what the vendor is offering.

    Two entirely separate paths end up here. Our OWN switch is signed with our
    own key and updated from ota/ - the DCL knows nothing about it and correctly
    offers nothing. Everything bought is signed by its vendor and published to
    the DCL, which is the only way a third-party controller can ever update it:
    the image is signed, so nobody can push their own firmware to somebody
    else's device, which is the point rather than a limitation.
    """
    ms = ms_of(node)
    if ms is None:
        return {"node": node, "error": f"node {node} is not on matter-server"}

    with _fw_lock:
        hit = _fw_cache.get(node)
        if hit and not fresh and time.time() - hit[0] < FW_TTL:
            return fw_live(node, hit[1])

    attrs, _err = m_attrs(node)
    out = {
        "node": node,
        "installed": m_get(attrs, 0, 0x0028, 0x000A),
        "updatePossible": m_get(attrs, 0, 0x002A, 0x0001),
    }
    try:
        upd = MS.call("check_node_update", {"node_id": ms}, timeout=120.0)
    except MatterError as exc:
        out["error"] = str(exc)
        return fw_live(node, out)

    if upd:
        out["available"] = upd.get("software_version_string")
        out["availableCode"] = upd.get("software_version")
        out["notes"] = upd.get("release_notes_url")
    with _fw_lock:
        _fw_cache[node] = (time.time(), out)
    return fw_live(node, out)


def firmware_update(node, version):
    """Run the update. Long - the image is fetched and then served to the device."""
    ms = ms_of(node)
    if ms is None:
        return
    with _fw_lock:
        _ota_running.add(node)
    _ota_status.pop(node, None)
    _ota_seen.pop(node, None)
    before = (firmware_for(node) or {}).get("installed")
    began = time.time()
    try:
        ota_note(node, "running", f"updating firmware from {before}")
        # No timeout worth naming: this downloads an image and then waits for a
        # sleepy device to fetch and apply it. The device's own UpdateState is
        # what the panel watches, not this call returning.
        MS.call("update_node", {"node_id": ms, "software_version": version},
                timeout=3600.0)
    except MatterError as exc:
        with _fw_lock:
            _ota_running.discard(node)
        ota_note(node, "failed", f"update failed: {exc}")
        return
    finally:
        with _fw_lock:
            _fw_cache.pop(node, None)

    # VERIFY, because "finished" from the layer below is not evidence.
    #
    # matter-server declares success when the device's UpdateState returns to
    # idle - "assuming done" is its own wording - and a transfer that dies
    # halfway also ends at idle. Observed: a contact sensor stopped at 8% with a
    # BDX timeout and was reported as finished, still on its old version. So the
    # only thing worth believing is the version, read back afterwards.
    with _fw_lock:
        _ota_running.discard(node)
    if not before:
        # Nothing to compare against, so say that rather than guess. Claiming
        # success off a single post-hoc read would be the same mistake one level
        # up: any value at all would look like a change.
        ota_note(node, "done", "update finished - version not confirmed")
        return
    ota_note(node, "running", "checking whether the new firmware took")
    radio_went = 0 < radio_up_for() < time.time() - began
    after, until = before, time.time() + 360
    while time.time() < until:
        time.sleep(15)
        attrs, _e = m_attrs(node)
        got = m_get(attrs, 0, 0x0028, 0x000A)
        if got and got != before:
            after = got
            break
    if after != before:
        ota_note(node, "done", f"updated to {after}")
    elif radio_went:
        # The one explanation worth offering, because it is the one actually
        # observed: a sustained transfer keeps the radio talking, the 5V rail
        # sags under it, the co-processor wedges and the whole Thread network
        # goes with it. Naming it beats inventing a reason about the device.
        ota_note(node, "failed",
                 f"the Thread radio restarted during the transfer, so it never "
                 f"finished - still on {before}")
    else:
        ota_note(node, "failed", f"the update did not take - still on {before}")

    # Settle what the tiles are told, now that the device has rebooted and can
    # be believed. The cache was dropped when the transfer ended, but the open
    # sheet polls this every few seconds and would have refilled it from a
    # device that had not restarted yet - leaving "update available" on a bulb
    # that had just taken one, for the whole hour the cache lives.
    try:
        firmware_for(node, fresh=True)
    except Exception as exc:  # noqa: BLE001 - the update itself already stands
        log(f"node {node}: firmware re-check failed: {exc}", "warn")


def inspect_node(node) -> dict:
    """Everything a device exposes, decoded: endpoints, clusters, attributes.

    Read from matter-server's cache, so it costs no radio traffic and cannot
    fail because the device is asleep - which matters most for exactly the
    devices you want to inspect.
    """
    attrs, err = m_attrs(node, timeout=30.0)
    if attrs is None:
        return {"node": node, "error": err or "no answer", "endpoints": []}

    eps = {}
    for path, val in attrs.items():
        bits = path.split("/")
        if len(bits) != 3:
            continue
        try:
            ep, cluster, attr = (int(b) for b in bits)
        except ValueError:
            continue
        info = MATTER_NAMES.get(str(cluster)) or {}
        cl = eps.setdefault(ep, {}).setdefault(cluster, {
            "id": cluster,
            "name": info.get("name") or f"cluster 0x{cluster:04X}",
            "attributes": [],
        })
        # A read that FAILED is cached as an object describing the failure, not
        # as a value, and rendering it as one would put
        # "InteractionModelError: UnsupportedAttribute" in a table of readings
        # as though the device had said it. Whether a device refuses an
        # attribute is worth showing - it is half of what an inspector is for -
        # but it has to be shown as a refusal.
        why = None
        if isinstance(val, dict) and "Reason" in val and "TLVValue" in val:
            why = str(val.get("Reason") or "").split(":")[-1].strip() or "unsupported"
            val = None
        cl["attributes"].append({
            "id": attr,
            "name": (info.get("attributes") or {}).get(str(attr))
                    or f"attribute 0x{attr:04X}",
            "value": val,
            "unsupported": why,
            "global": attr in GLOBAL_ATTRS,
        })

    out = []
    for ep in sorted(eps):
        clusters = []
        for cid in sorted(eps[ep]):
            cl = eps[ep][cid]
            # A cluster whose every attribute refused is not on the device at
            # all - it is the residue of somebody having asked. Drop it, or the
            # inspector invents capabilities.
            if all(a["unsupported"] for a in cl["attributes"]):
                continue
            # Named attributes first and in id order; the global ones after,
            # because they are the same on every cluster in the house.
            cl["attributes"].sort(key=lambda a: (a["global"], a["id"]))
            cl["count"] = sum(1 for a in cl["attributes"] if not a["global"])
            clusters.append(cl)
        out.append({"endpoint": ep, "clusters": clusters})
    return {"node": node, "endpoints": out}


def m_attrs(node, timeout=20.0, ms=None):
    """Everything matter-server currently knows about a node.

    This is a CACHE READ, and that is the whole point of it. matter-server holds
    a subscription to every attribute on every commissioned node - 265 of them
    on a bulb here - and keeps them current from the device's own reports. So
    this costs no radio traffic, cannot fail because the device was asleep, and
    returns in milliseconds.

    Against the old path the difference is not a tuning improvement. Reading a
    sleepy switch through chip-tool meant queuing the request at its Thread
    parent and waiting up to a full 15 s poll interval for it to be collected -
    and the read simply failed if it landed wrong. Here the switch's values are
    already in hand before anyone asks.

    `ms` may be passed when the caller already knows matter-server's id for this
    node and devices.json does not yet - which is the case for the few seconds
    between commissioning a device and saving it. Without it, a freshly
    commissioned device could not be read at all: ms_of() looks the mapping up
    on DISK, and the entry carrying it has not been written yet. The symptom was
    a new device arriving with an empty descriptor and no capabilities, which
    then made every later decision about it wrong - a water leak sensor whose
    device type was unknown reported its flood as a door.

    Returns (attributes, error). Attribute keys are "endpoint/cluster/attribute",
    all decimal.
    """
    if ms is None:
        ms = ms_of(node)
    if ms is None:
        return None, f"node {node} is not on matter-server"
    try:
        r = MS.call("get_node", {"node_id": ms}, timeout=timeout)
    except MatterError as exc:
        return None, str(exc)
    if not r:
        return None, "no such node"
    if not r.get("available"):
        # Known, but not reachable. Distinct from "no answer to this read",
        # which is what the poller used to report for a sleeping device.
        return None, "node is not reachable"
    return r.get("attributes") or {}, None


def m_get(attrs, endpoint, cluster, attr, default=None):
    """One attribute out of an m_attrs() dict."""
    if not attrs:
        return default
    v = attrs.get(f"{int(endpoint)}/{int(cluster)}/{int(attr)}")
    return default if v is None else v


def m_read(node, endpoint, cluster, attr, timeout=45.0):
    """Read one attribute. Returns (value, error) - value is None on failure."""
    ms = ms_of(node)
    if ms is None:
        return None, f"node {node} is not on matter-server"
    try:
        return MS.call("read_attribute",
                       {"node_id": ms,
                        "attribute_path": f"{int(endpoint)}/{int(cluster)}/{int(attr)}"},
                       timeout=timeout), None
    except MatterError as exc:
        return None, str(exc)


def matter_map(devices: dict) -> dict:
    """matter-server's node id -> ours, from devices.json.

    Two fabrics means two sets of node ids for the same hardware: the sensor is
    1004 to chip-tool and 1 to matter-server. `msNode` on the device entry is
    the only thing that ties them together.
    """
    out = {}
    for dev in all_devices(devices):
        if dev.get("msNode") is not None:
            out[int(dev["msNode"])] = int(dev["node"])
    return out


# What a bulb pushes, and where it lands in state. Same fields the chip-tool
# read-back fills, so nothing downstream can tell which path a value came by.
#
# This is what makes the WALL SWITCH visible. The switch commands the bulb
# directly over Thread with the Pi nowhere in the path, so the panel never hears
# about it - it could only find out by asking, on whatever interval it had
# picked. Subscribed, the bulb reports the change itself, and the tile moves
# while your finger is still on the button.
BULB_ATTRS = {
    # Not a bulb attribute, and it earns its place here anyway: this is the
    # device's OWN identify countdown, pushed as it ticks. It is the only
    # honest source for "is it identifying" - a timer started in the browser
    # when the request returns is measuring the wrong thing entirely, because a
    # sleeping device does not receive the command for another six to fifteen
    # seconds and then runs its full duration from there.
    (0x0003, 0x0000): ("identify", int),
    # The device's own account of an update in progress. Shown rather than a
    # spinner of our own: only the device knows whether it is downloading,
    # applying, or waiting - and an update is long enough that "something is
    # happening" is not good enough.
    (0x002A, 0x0002): ("otaState", int),
    (0x002A, 0x0003): ("otaProgress", int),
    (0x0006, 0x0000): ("on", bool),
    (0x0008, 0x0000): ("level", int),
    (0x0008, 0x0011): ("onlevel", int),
    (0x0300, 0x0007): ("mireds", int),
    (0x0300, 0x400B): ("ctMin", int),
    (0x0300, 0x400C): ("ctMax", int),
}


# A bulb's on/off, on its way to note_power - and NOT down the listener thread.
#
# matter_value runs on the one thread whose job is to keep reading the socket.
# note_power can end in rearm, which sends two Matter commands with 20-second
# timeouts; running that inline would stall every subscription in the house
# behind one light being switched off, and the reports it provokes would queue
# up behind the reader that is waiting for them.
_power_q = queue.Queue()


def power_watch() -> None:
    """Turn observed on/off transitions into note_power, off the hot path."""
    while True:
        node, on = _power_q.get()
        try:
            note_power(node, on)
        except Exception as exc:  # noqa: BLE001 - one bulb does not stop the rest
            log(f"bulb {node}: power watch failed: {exc}", "warn")


SWITCH_CLUSTER = 0x003B

# Measured on a BILRESA, whose two buttons are endpoints 1 and 2. The gesture is
# only ever known from these; the cluster's CurrentPosition attribute flips 0/1
# around every press and cannot tell one tap from two.
#
#   short press   InitialPress -> ShortRelease -> MultiPressComplete count 1
#   double press  the same twice, MultiPressOngoing between, then count 2
#   LONG PRESS    InitialPress -> LongPress -> LongRelease, and NO
#                 MultiPressComplete at all - so anything that listens only for
#                 the completion event silently ignores every long press.
SWITCH_EVENTS = {
    0x00: "latched", 0x01: "down", 0x02: "long", 0x03: "up",
    0x04: "long-up", 0x05: "counting", 0x06: "complete",
}

# Whether to wait out the multi-press window, so a double tap can be told from
# a single one.
#
# Measured on the BILRESA: ShortRelease lands 141 ms after the press, and
# MultiPressComplete - the only event carrying the count - lands 519 ms after
# THAT. Waiting for it is the only way to know a second tap is not coming, and
# it is the whole of the delay a person can feel. There is no third option: at
# the first tap nothing in the protocol says whether another is on its way.
#
# Off here, because this house does not use a double tap on this remote and an
# instant light is worth more than a gesture nobody presses. Turning it back on
# costs one line and half a second.
WAIT_FOR_DOUBLE = False

# Endpoints part-way through a repeat tap, and when that was noticed.
_repeat = {}
REPEAT_GRACE = 2.0


def gesture_of(node, endpoint, event_id, data):
    """The press a person made, or None for the events that only lead up to one."""
    if event_id == 0x02:
        # Long press is unaffected by any of this: it reports LongPress and
        # LongRelease and never a MultiPressComplete at all.
        return "long"

    if WAIT_FOR_DOUBLE:
        if event_id == 0x06:
            n = data.get("totalNumberOfPressesCounted")
            if n == 1:
                return "press"
            if n == 2:
                return "double"
            return f"press-{n}" if isinstance(n, int) else None
        return None

    # Acting on the release instead, which is where the half second goes.
    #
    # Done naively that fires TWICE on a double tap - two releases, two actions,
    # and a light that ends up back where it started. It does not have to:
    # MultiPressOngoing arrives BEFORE the second release (measured 4 ms
    # before), so the repeat is known in time to drop it. A double tap then
    # acts once, promptly, instead of acting twice or not at all.
    key = (node, endpoint)
    now = time.time()
    if event_id == 0x05:
        _repeat[key] = now
        return None
    if event_id == 0x03:
        when = _repeat.pop(key, 0)
        # A stale flag - the release that should have cleared it never arrived -
        # must not swallow the next real press.
        if when and now - when < REPEAT_GRACE:
            return None
        return "press"
    return None


# What each button does, when the file does not say. A two-button remote in a
# room with lights wants the obvious thing, and anything cleverer should be a
# choice somebody made rather than a default they have to discover.
#
# `toggle` rather than on/off because the same button may be pointed at one
# room or at three, and a toggle is right either way. Whether the group is on is
# decided ONCE and sent to all of them, so two bulbs on one button cannot end up
# opposite each other - the same reason our own switch sends its state rather
# than a toggle each.
DEFAULT_GESTURES = {"press": "toggle", "long": "full"}
DEFAULT_GESTURES_2 = {"press": "toggle", "long": "full"}
LEVEL_STEP = 25

# "full" is what our own wall switch does on a long press, and it is copied here
# deliberately rather than approximated: level 254 and 4000 K, written once.
#
# What makes it TEMPORARY is what it does NOT do. It never writes OnLevel, so
# the bulb still comes back to the schedule's brightness the next time it is
# switched on, and it never updates the schedule's memo, so the tick still
# believes it last sent the curve's value. An observed off re-arms the bulb.
# Going through the panel's ordinary "set this bulb" path would write OnLevel
# and the memo, and full brightness would then stick for good - which is the one
# thing "provisionally" has to rule out.
FULL_LEVEL = 254
FULL_MIREDS = 250          # 1e6 / 4000 K

_press_q = queue.Queue()


def remote_action(entry, button, gesture):
    cfg = (entry.get("buttons") or {}).get(str(button))
    if isinstance(cfg, dict) and gesture in cfg:
        return cfg[gesture]
    table = DEFAULT_GESTURES_2 if str(button) == "2" else DEFAULT_GESTURES
    return table.get(gesture)


def press_watch():
    """Act on presses, off the listener's thread.

    Commanding a bulb takes a Matter round trip, and the socket this arrived on
    is the one every other device reports through. Doing the work here is what
    keeps one slow bulb from delaying a door sensor.
    """
    while True:
        node, button, gesture = _press_q.get()
        try:
            remote_act(node, button, gesture)
        except Exception as exc:  # noqa: BLE001 - a press is not worth dying for
            log(f"node {node} button {button}: {gesture} failed: {exc}", "err")


def remote_act(node, button, gesture):
    try:
        devices = load_devices()
    except (OSError, ValueError):
        return
    entry = next((x for x in switches(devices) if x["node"] == node), None)
    if entry is None:
        return
    targets = remote_buttons(entry).get(str(button)) or []
    action = remote_action(entry, button, gesture)
    if not targets or not action:
        return
    bulbs = {b["node"]: b for b in devices.get("bulbs", [])}
    if action == "toggle":
        # One decision for the whole group, taken before anything is sent.
        want_on = not any(state_of(n).get("on") for n in targets if n in bulbs)
        action = "on" if want_on else "off"
    for n in targets:
        b = bulbs.get(n)
        if b is None:
            continue
        ep = int(b.get("endpoint", 1))
        if action == "full":
            err = m_cmd(n, ep, 0x0008, "MoveToLevelWithOnOff",
                        {"level": FULL_LEVEL, "transitionTime": 0,
                         "optionsMask": 0, "optionsOverride": 0})
            # Colour second, and only if the bulb has any: a plain dimmable bulb
            # has no ColorControl and would answer with an error that means
            # nothing went wrong.
            st = state_of(n)
            if not err and st.get("mireds") is not None:
                want = FULL_MIREDS
                lo, hi = st.get("ctMin"), st.get("ctMax")
                if isinstance(lo, int):
                    want = max(want, lo)
                if isinstance(hi, int):
                    want = min(want, hi)
                e2 = m_cmd(n, ep, 0x0300, "MoveToColorTemperature",
                           {"colorTemperatureMireds": want, "transitionTime": 4,
                            "optionsMask": 1, "optionsOverride": 1})
                if e2:
                    log(f"bulb {n}: colour on long press failed: {e2}", "warn")
        elif action in ONOFF_CMD:
            err = m_cmd(n, ep, 0x0006, ONOFF_CMD[action])
        elif action in ("brighter", "dimmer"):
            err = m_cmd(n, ep, 0x0008, "StepWithOnOff",
                        {"stepMode": 0 if action == "brighter" else 1,
                         "stepSize": LEVEL_STEP, "transitionTime": 2,
                         "optionsMask": 0, "optionsOverride": 0})
        else:
            log(f"node {node} button {button}: unknown action {action!r}", "warn")
            return
        if err:
            log(f"bulb {n}: {action} from button {button} failed: {err}", "warn")
    log(f"node {node} button {button}: {action} on "
        f"{len(targets)} bulb{'' if len(targets) == 1 else 's'}", "ok")


def matter_event(node, endpoint, cluster, event_id, data):
    """A button was pressed. Nothing else here reports through this path yet."""
    if cluster != SWITCH_CLUSTER:
        return
    name = SWITCH_EVENTS.get(event_id, f"event {event_id}")
    gesture = gesture_of(node, endpoint, event_id, data)
    if gesture is None:
        # Logged at debug volume rather than dropped, because the run-up is what
        # tells you WHY a gesture did not arrive when somebody says the button
        # did nothing.
        log(f"node {node} button {endpoint}: {name}", "dim")
        return
    log(f"node {node} button {endpoint}: {gesture}", "ok")
    # The press itself is proof the device is alive, exactly as a pushed reading
    # would be, and it is the only thing a button ever sends.
    state_put(node, {"readAt": time.time(), "okAt": time.time(), "ok": True,
                     "press": {"button": endpoint, "gesture": gesture,
                               "at": time.time()}})
    _press_q.put((node, endpoint, gesture))


def matter_value(node, cluster, attr, val, pushed=True):
    """One reading from matter-server, folded into state as a poll would be.

    `pushed` says whether a DEVICE sent this, or whether it came out of
    matter-server's cache on our periodic sweep. Only the former is evidence
    that anything is still alive, so only the former touches liveness - see
    matter_link._absorb.
    """
    live = {"readAt": time.time(), "okAt": time.time(),
            "ok": True, "err": None} if pushed else {}
    hit = BULB_ATTRS.get((cluster, attr))
    if hit is not None:
        key, cast = hit
        if val is None:
            return
        try:
            v = cast(val)
        except (TypeError, ValueError):
            return
        if pushed:
            sub_heard(node)
        state_put(node, values={key: v}, meta=live)
        # The wall switch is the only thing that turns these lights on and off
        # most of the time, and this is the only place the panel hears about it.
        # Without this the hold never ends and the colour never rejoins the
        # curve - see note_power.
        if pushed and (cluster, attr) == (0x0006, 0x0000):
            _power_q.put((node, v))
        return

    for c, a, key, _lab, _unit, scale in MEASURED:
        if c != cluster or a != attr:
            continue
        # Same resolution as the read path: the cluster says a bit changed, the
        # device type says what the bit is about.
        if cluster == BOOLEAN_CLUSTER:
            dev = next((d for d in all_devices(load_devices())
                        if int(d.get("node", 0)) == int(node)), None)
            if dev is None:
                return
            key = boolean_kind(dev)
        v = round(val * scale, 2) if isinstance(val, (int, float)) and scale != 1 else val
        fn = MEASURE_FN.get(key)
        if fn:
            v = fn(v)
        cur = dict(state_of(node).get("measured") or {})
        if pushed:
            sub_heard(node)
        if cur.get(key) == v:
            if live:
                state_put(node, meta=live)
            return
        cur[key] = v
        log(f"node {node}: {key} -> {v}", "step")
        state_put(node, values={"measured": cur}, meta=live)
        return


def matter_watch():
    if not MATTER_LINK:
        log("matter-server link is off (PANEL_MATTER_LINK=0)", "info")
        return
    link = MatterLink(MATTER_WS, matter_value, log, on_event=matter_event)

    def remap():
        while True:
            try:
                link.set_map(matter_map(load_devices()))
            except (OSError, ValueError):
                pass
            time.sleep(30)

    threading.Thread(target=remap, name="matter-map", daemon=True).start()
    link.run()






def _absorb_report(raw, by_path: dict):
    """Turn one streamed report into state, if that is what it is."""
    if not isinstance(raw, str) or not raw.lstrip().startswith("{"):
        return
    try:
        msg = json.loads(raw)
    except ValueError:
        return
    for res in msg.get("results", []):
        if "clusterId" not in res or "value" not in res:
            continue
        try:
            key = (int(res.get("nodeId", 0)) or None, int(res["clusterId"]),
                   int(res["endpointId"]), int(res["attributeId"]))
        except (TypeError, ValueError):
            continue
        # chip-tool does not always echo the node on a report, so fall back to
        # the only subscription that matches the rest of the path.
        hit = by_path.get(key)
        if hit is None:
            for (n, c, e, a), v in by_path.items():
                if (c, e, a) == key[1:]:
                    key = (n, c, e, a)
                    hit = v
                    break
        if hit is None:
            continue
        name, scale = hit
        val = res["value"]
        val = round(val * scale, 2) if isinstance(val, (int, float)) and scale != 1 else val
        node = key[0]
        cur = dict(state_of(node).get("measured") or {})
        if cur.get(name) == val:
            sub_heard(node)
            state_put(node, meta={"readAt": time.time(), "okAt": time.time(),
                                     "ok": True, "err": None})
            continue
        cur[name] = val
        sub_heard(node)
        log(f"node {node}: {name} -> {val}", "step")
        state_put(node, values={"measured": cur},
                  meta={"readAt": time.time(), "okAt": time.time(),
                                     "ok": True, "err": None})


def refresh_device(dev: dict) -> list:
    """A generic device's readings, in one request."""
    try:
        vals = read_measurements(dev)
    except Exception as exc:  # noqa: BLE001
        return state_put(dev["node"], meta={"readAt": time.time(), "ok": False,
                                            "err": str(exc)})
    if not vals:
        # Either it did not answer, or it exposes nothing we know how to read.
        # Those are different, and the descriptor is what tells them apart.
        knows = any((info.get("clusters") or [])
                    for info in ((dev.get("desc") or {}).get("endpoints") or {}).values())
        return state_put(dev["node"],
                         meta={"readAt": time.time(), "ok": not knows,
                               "err": "no response" if knows else None})
    return state_put(dev["node"], values={"measured": vals},
                     meta={"readAt": time.time(), "okAt": time.time(),
                                     "ok": True, "err": None})


def refresh_bulbs(force: bool = False) -> dict:
    """What every bulb and every generic device is doing, coalesced and rate
    limited. One map, because the page draws them side by side."""
    try:
        devices = load_devices()
    except (OSError, ValueError):
        return {}

    # Remotes are read as well as pressed. They live with the switches because
    # that is what they are, but a battery reading is a reading like any other,
    # and leaving them out of here is what hid the battery on a device that runs
    # on two AAA cells.
    watched = ([("bulb", b) for b in devices.get("bulbs", [])]
               + [("device", d) for d in devices.get("devices", [])]
               + [("device", r) for r in switches(devices) if is_remote(r)])

    now = time.time()
    # A page load must never wait on the radio, and during an OTA it will if we
    # let it. A firmware transfer saturates the Thread mesh: pushes stop
    # arriving, devices fall out of subscribed(), and this loop starts making
    # real reads that queue behind the image. Measured on a live update,
    # /api/bulbs took 139 SECONDS - the interface simply stopped.
    #
    # There is nothing to gain by asking anyway. The pushes fill state in
    # whenever the mesh has room, so serving what we already know is both
    # faster and no less true. The readings show their age through readAt like
    # they always did.
    deadline = now + BULB_SWEEP_MAX
    quiet = bool(_ota_running)
    if quiet:
        log("firmware update in progress - serving readings without polling",
            "info")
    with _bulb_lock:
        for kind, dev in watched:
            # ... and a ceiling even when nothing is updating, so one slow
            # device cannot hold the whole page open.
            if quiet or time.time() > deadline:
                continue
            # A subscribed device is not polled. Its value arrives on its own,
            # and a direct read of a node while a subscription owns its session
            # comes back empty - which would flap it between "reporting" and
            # "not answering" for no reason.
            if subscribed(dev["node"]):
                continue
            st = state_of(dev["node"])
            age = now - float(st.get("readAt") or 0)
            if not force:
                if age < BULB_TTL_SEC:
                    continue
                # Cold: it did not answer last time. Do not spend a full timeout
                # on it every time somebody opens the page.
                #
                # Except for a device whose readings are events. A sleepy sensor
                # misses reads as a matter of course, and one miss used to put it
                # in a FIVE MINUTE cold shoulder - so a door sensor that failed
                # once went quiet for longer than anybody would keep watching.
                # That, not the poll interval, is what made a magnet take two
                # minutes to register.
                if (st.get("ok") is False and age < BULB_COLD_SEC
                        and not is_event_device(dev)):
                    continue
            try:
                (refresh_bulb if kind == "bulb" else refresh_device)(dev)
            except Exception as exc:  # noqa: BLE001 - one device does not stop the rest
                log(f"node {dev['node']}: read failed: {exc}", "warn")

    out = {}
    for kind, dev in watched:
        st = state_of(dev["node"])
        # One missed read is not a missing device.
        #
        # A battery sensor is asleep most of the time, so a read that arrives in
        # the wrong moment simply fails - routinely, and with nothing wrong. Left
        # as-is that flipped the tile to "not answering" seconds after a perfectly
        # good reading, which is the panel crying wolf about its own timing.
        # It has to have been quiet for a while before we say so.
        ok = st.get("ok")
        if ok is False and time.time() - float(st.get("okAt") or 0) < SLEEPY_GRACE:
            ok = True
        # The device's own identify countdown, so the button can show what the
        # DEVICE is doing rather than a timer the browser started. It applies to
        # every kind, which is why it sits above the split.
        row = {"ok": ok, "readAt": st.get("readAt"),
               "identify": st.get("identify"),
               "otaState": st.get("otaState"),
               "otaProgress": st.get("otaProgress"),
               "otaBusy": dev["node"] in _ota_running}
        if kind == "bulb":
            row.update({"on": st.get("on"), "level": st.get("level"),
                        "mireds": st.get("mireds"), "onlevel": st.get("onlevel"),
                        "ctMin": st.get("ctMin"), "ctMax": st.get("ctMax"),
                        "held": overridden(dev["node"])})
        else:
            row["measured"] = st.get("measured")
        out[str(dev["node"])] = row
    return out


def refresh_all(reason: str = "") -> int:
    changes = 0
    try:
        sws = switches(load_devices())
    except (OSError, ValueError) as exc:
        log(f"cannot read devices.json: {exc}", "err")
        return 0
    if not sws:
        return 0
    if reason:
        log(f"background refresh ({reason}): {len(sws)} switches", "step")
    for sw in sws:
        try:
            changes += len(refresh_switch(sw))
        except Exception as exc:  # noqa: BLE001 - a dead switch does not stop the rest
            log(f"node {sw['node']}: refresh failed: {exc}", "warn")
    return changes


# ----------------------------------------- the schedule, applied to the bulbs
#
# Who holds the schedule, and why it moved here.
#
# The switch used to hold the schedule and send the level and the color
# temperature for the current hour on every press. That had three defects: the
# switch kept its state in a local variable that went stale if you turned the
# bulb on from somewhere else; the bulb ramped from the old brightness to the
# new one, so you saw a flash; and a battery-powered device sent three commands
# per press.
#
# Now the Pi writes INTO THE BULB, into its persistent attributes, what it
# should come up at:
#   OnLevel            - the level an On command brings it to
#   color temperature  - written with ExecuteIfOff, so it applies while off too
# The switch only sends Toggle. The bulb already knows the rest.
#
# Each of those is paired with something that reaches a bulb which is already
# lit, because OnLevel alone changes nothing until the next time the bulb is
# switched on - and "I changed the schedule and the lamp ignored me" is not a
# subtlety anybody should have to learn. The colour command carries ExecuteIfOff
# and so covers both cases by itself; the level needs a MoveToLevel alongside
# the attribute write.
#
# Verified on hardware before building on it: with the bulb off, we write 454
# mireds and OnLevel 200; on Toggle it comes up at exactly 454 and 200.
#
# CADENCE: written ONLY when the slot changes, that is seven times a day, not
# periodically. OnLevel is persistent in the IKEA bulb; writing it every few
# minutes would mean over 100 000 writes a year into its memory, worn out for
# nothing since the result is identical.
#
# WHAT YOU LOSE: with the Raspberry Pi off, the bulbs stay at the last value
# they received instead of following the clock. Degradation, not failure - but
# it is a change from the original promise that the switch works on its own, and
# it was a deliberate choice.
#
# BONUS: local time comes from the Pi, so the time zone and daylight saving are
# correct with nothing extra. The switch had the offset fixed at compile time.
SCHED_TICK_SEC = int(os.environ.get("PANEL_SCHED_TICK_SEC", "60"))

# The schedule lives on the Pi, not in the switch.
#
# It lived in the switch for as long as the switch executed it. It no longer
# does - the Pi writes straight into the bulbs - so keeping it there would mean
# a battery-powered device storing and syncing data it never uses.
SCHEDULE_FILE = pathlib.Path(
    os.environ.get("PANEL_SCHEDULE", str(HERE.parent / "ota" / "state" / "schedule.json")))


# ------------------------------------------------------ one house, many bulbs
#
# There is a HOUSE schedule, and any bulb may have its OWN instead. That is the
# whole model, and it is deliberately the simplest one that answers the question
# people actually have - "all my lights do this, except the bedside lamp".
#
# Named schedules that several bulbs subscribe to would be more powerful and
# would need a screen of their own to manage; a bulb either follows the house or
# it does not, which needs one word on the bulb's sheet.
#
# The file keeps its old shape - a bare {"points": [...]} is still a valid house
# schedule with no overrides - so nothing has to be migrated.
#
#   {"points": [...],  "bulbs": {"1001": {"points": [...]}}}


def load_schedule_file() -> dict:
    try:
        d = json.loads(SCHEDULE_FILE.read_text())
    except (OSError, ValueError):
        d = {}
    if isinstance(d, list):                  # the oldest shape of all
        d = {"points": d}
    if not isinstance(d, dict):
        d = {}
    pts = d.get("points")
    house = pts if isinstance(pts, list) and pts else list(SCHED_DEFAULT)
    bulbs = {}
    for node, entry in (d.get("bulbs") or {}).items():
        own = (entry or {}).get("points")
        if isinstance(own, list) and own:
            bulbs[str(node)] = own
    return {"points": house, "bulbs": bulbs}


def load_schedule() -> list:
    """The house schedule. Kept for everything that only ever wanted that."""
    return load_schedule_file()["points"]


def schedule_for(node, sched: dict = None) -> list:
    """The points that govern one bulb: its own if it has any, else the house's."""
    sched = sched or load_schedule_file()
    return sched["bulbs"].get(str(node)) or sched["points"]


def save_schedule_file(sched: dict):
    SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
    body = {"points": sched["points"]}
    if sched.get("bulbs"):
        body["bulbs"] = {k: {"points": v} for k, v in sched["bulbs"].items()}
    tmp = SCHEDULE_FILE.with_name(SCHEDULE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(SCHEDULE_FILE)


def save_schedule(points: list):
    """Replace the house schedule, leaving any per-bulb ones alone."""
    sched = load_schedule_file()
    sched["points"] = points
    save_schedule_file(sched)


# ------------------------------------------------ the curve, as the chart draws it
#
# The editor draws a smooth line through the middle of each column, and people
# read it the way you read any chart: the height above a time is the value at
# that time. It used to be a lie. The Pi applied a step function - a column's
# value held flat for the whole column - so dragging the 12:00 column bent the
# drawn line downwards over 11:40 while the light at 11:40 did not move.
#
# Rather than make the chart uglier, the Pi now follows the curve. Same spline,
# same maths, evaluated here at the current minute.
#
# It is CYCLIC: a day is a loop, so the sample before the first column is the
# last one and the light glides through midnight instead of stepping at it.
#
# The brightness axis is PERCEPTUAL for the same reason it is on the chart -
# The curve is interpolated in the same units the chart draws, so the schedule
# the Pi follows is the schedule you drew. Colour temperature is interpolated in
# mireds, which is what its axis already is; a spline is affine-invariant, so
# that matches the drawing exactly.

# The dimmest level worth sending.
#
# MEASURED, not assumed: at level 1 this bulb switches OFF - it reports
# OnOff=false and goes dark - while levels 2 and 3 stay lit. Its own MinLevel
# attribute claims 1, so the device is describing a level it does not actually
# honour, and nothing but trying it would have found that out. Sending 1 as a
# "very dim" value silently extinguishes the lamp.
LEVEL_MIN = int(os.environ.get("PANEL_LEVEL_MIN", "2"))


def perceived(level: int) -> float:
    """How bright a Matter level looks, 0..1.

    Simply level/254, because the BULB already applies the perceptual curve.

    This used to apply L* from CIE Lab, on the reasoning that a Matter level is
    proportional to emitted light and the eye is not. Matter does not say that -
    the spec's only word on the physical meaning of a level is that it "is
    device dependent" - and for this hardware it is false. Measured with a
    fixed-exposure camera against the real bulb: at level 64 it emits 3.7% of
    its level-254 output. Proportional-to-light demands 25%. "Level is already
    perceived brightness" predicts 4.5%, which is what came back.

    So the correction was being applied twice, and the compounded scale was
    worse than either alone: the top of the range moved 41 points of perceived
    lightness while the bottom moved 2. Every other controller - Google Home,
    Home Assistant, zigbee2mqtt, SmartThings - shows level/254 and applies no
    curve at all.
    """
    return max(0.0, min(1.0, level / 254))


def level_from_perceived(p: float) -> int:
    return max(LEVEL_MIN, min(254, round(max(0.0, min(1.0, p)) * 254)))


def _mono_tangents(y: list) -> list:
    """Fritsch-Carlson tangents: a curve that never leaves the interval between
    two neighbouring values.

    Catmull-Rom, which this used to be, takes a point's tangent from its
    neighbours - so the first column of a plateau inherits a large slope from the
    ramp before it and the curve sails past the plateau before settling back onto
    it. On a chart that is a hump on a flat top and a kink where it lands. On a
    light schedule it is also wrong: a bump above a 100% plateau is a brightness
    the bulb cannot produce, and a dip under a 10% valley is one the floor
    forbids.
    """
    n = len(y)
    d = [y[(i + 1) % n] - y[i] for i in range(n)]
    m = [(d[(i - 1) % n] + d[i]) / 2 for i in range(n)]

    # A flat segment pins both its ends: this is what keeps plateaus flat.
    for i in range(n):
        if d[i] == 0:
            m[i] = 0.0
            m[(i + 1) % n] = 0.0
    # The circle condition, which keeps everything else inside its interval.
    for i in range(n):
        if d[i] == 0:
            continue
        j = (i + 1) % n
        a = m[i] / d[i]
        b = m[j] / d[i]
        q = a * a + b * b
        if q > 9:
            t = 3 / math.sqrt(q)
            m[i] = t * a * d[i]
            m[j] = t * b * d[i]
    return m


def _sample(vals: list, minute: float) -> float:
    """The spline through evenly spaced column centres, at a minute of the day."""
    n = len(vals)
    if n == 1:
        return vals[0]
    m = _mono_tangents(vals)
    u = minute / (1440 / n) - 0.5     # 0 at the first column's centre
    i = math.floor(u)
    t = u - i
    i0 = i % n
    i1 = (i0 + 1) % n
    t2 = t * t
    t3 = t2 * t
    return ((2 * t3 - 3 * t2 + 1) * vals[i0]
            + (t3 - 2 * t2 + t) * m[i0]
            + (-2 * t3 + 3 * t2) * vals[i1]
            + (t3 - t2) * m[i1])


def curve_at(points: list, minute: float) -> tuple:
    """(level, mireds) the schedule asks for at this minute of the day."""
    if not points:
        return None, 0
    lvl = level_from_perceived(
        _sample([perceived(int(p.get("level", 128))) for p in points], minute))
    mireds = 0
    if any(int(p.get("mireds", 0)) for p in points):
        raw = _sample([float(p.get("mireds") or 370) for p in points], minute)
        mireds = max(100, min(700, round(raw)))
    return lvl, mireds


def slot_for(points: list, minute: int) -> dict:
    """The schedule entry in effect at the given minute.

    Same rule as in the firmware: the last entry that starts at or before the
    requested minute, and if no entry has started yet (after midnight, before
    the first point) the LAST one in the table is used - that is the one that
    covers the wrap past midnight.
    """
    if not points:
        return {}
    slot = points[-1]
    for p in points:
        if int(p.get("min", 0)) <= minute:
            slot = p
        else:
            break
    return slot


def bulbs_of(sw: dict, devices: dict) -> list:
    """The bulbs a switch controls, per its actual binding table."""
    st = state_of(sw["node"])
    nodes = {e["node"] for e in (st.get("binding") or []) if "node" in e}
    return [b for b in devices.get("bulbs", []) if b["node"] in nodes]


# How far a value has to drift before each kind of write is worth making.
#
# OnLevel is a PERSISTENT attribute - it survives a power cut, which is the
# whole reason the bulb can come up correctly with the Pi switched off - and
# persistent means flash. Written every minute it would be over half a million
# writes a year into a lamp. So it moves in steps, and only when the curve has
# gone somewhere.
#
# The other two are commands. They cost a packet and nothing else, so they can
# follow the curve as closely as the tick allows.
# How long a hand on the light can keep the schedule off it.
#
# The hold ends when the light is switched off - that is the rule, and it is the
# right one for a lamp somebody is using. It has one blind spot: a lamp that is
# never switched off. Leave the kitchen light on all day, nudge it once at nine
# in the morning, and it sits out the whole day, because the one event that ends
# a hold never comes.
#
# So the hold has a ceiling as well. Whichever arrives first ends it: the light
# goes off, or this runs out. Set it to 0 to make switching off the only way.
HOLD_MAX_SEC = int(os.environ.get("PANEL_HOLD_MAX_SEC", str(3 * 3600)))

ONLEVEL_STEP = int(os.environ.get("PANEL_ONLEVEL_STEP", "8"))
MIRED_STEP = int(os.environ.get("PANEL_MIRED_STEP", "3"))


def set_override(node, on: bool):
    """Remember that somebody set this bulb by hand - or stopped.

    Only sending on change was not enough. It stops the schedule stamping on a
    manual change WHILE THE CURVE IS FLAT, and does nothing at a slot boundary:
    set a lamp to full at 14:29 and at 14:30 the curve moves, so the schedule
    ramps it back down over the next minute. From under the lamp that is the
    light changing by itself a minute after you set it, with the panel still
    showing what you asked for.

    So a hand on the light wins outright, and keeps winning until the light is
    switched off. Switching off is the natural end of "I am using this lamp
    now", and it is also the moment OnLevel starts mattering again.

    `heldLit` is what stops that rule eating its own tail. A change made while
    the lamp is DARK is not somebody using the lamp - it is somebody choosing
    what it will come up at, and releasing on the next observed off would throw
    it away seconds later, before the lamp had ever been switched on. So a hold
    is only ever released once the lamp has been seen lit under it.
    """
    lit = state_of(node).get("on") is True if on else False
    with _state_lock:
        entry = _state.setdefault("bulbs", {}).setdefault(str(node), {})
        if on:
            entry["override"] = time.time()
            if lit or "heldLit" not in entry:
                entry["heldLit"] = lit
        else:
            entry.pop("override", None)
            entry.pop("heldLit", None)
    state_save()


def release_hold(node, why: str) -> bool:
    """Drop the hold and the memo together.

    Together, because the memo is what the schedule compares against and the
    manual value was written into it. Leave it behind and the next tick measures
    the curve against the MANUAL brightness, finds the gap under its threshold
    and sends nothing at all - a released bulb that never moves.
    """
    with _state_lock:
        entry = _state.get("bulbs", {}).get(str(node))
        if not entry or "override" not in entry:
            return False
        entry.pop("override", None)
        entry.pop("heldLit", None)
        for k in ("onlevel", "level", "mireds"):
            entry.pop(k, None)
    state_save()
    log(f"bulb {node}: {why} - back on the schedule", "step")
    return True


def overridden(node) -> bool:
    with _state_lock:
        started = _state.get("bulbs", {}).get(str(node), {}).get("override")
    if started is None:
        return False
    # Otherwise a lamp that is never switched off is held for ever.
    if HOLD_MAX_SEC and time.time() - float(started) >= HOLD_MAX_SEC:
        release_hold(node, f"held its {HOLD_MAX_SEC // 60} min")
        return False
    return True


def note_power(node, on: bool) -> None:
    """Every observation of a bulb's on/off runs through here.

    The hold ends when the light is switched off - and in this house that is
    nearly always the WALL SWITCH, which talks to the bulb directly over Thread
    with the Pi nowhere in the command path. So the release cannot live in the
    request handler that turned the light off, the way it used to: it has to
    live where an off is OBSERVED, whoever caused it. Before this, a lamp set by
    hand and then switched off at the wall stayed held for good - skipped by
    every tick, coming up at the manual value for ever, with nothing on screen
    saying why.

    The memo goes with the flag. Leaving it behind is what let the schedule
    decide the bulb was already where the curve wanted it: the manual value had
    been written into the memo, so the next tick compared the curve against the
    manual value, found the difference under the threshold, and sent nothing.

    AN OFF RE-ARMS THE BULB WHETHER OR NOT A HOLD WAS IN PLAY. The panel only
    ever knows about a manual change it made itself, and the wall switch is not
    that: a long press writes brightness and colour straight into the bulb over
    Thread, and no hold exists to end. Brightness recovers on its own, because
    OnLevel decides what the bulb comes back to and the long press does not touch
    it - but COLOUR HAS NO OnLevel. The bulb simply keeps the last colour it was
    given, so a long press left it at 4000 K against a curve asking for 2400 K,
    and the only thing that eventually corrected it was the curve drifting far
    enough for the schedule to re-send. Measured: four and a half minutes at
    dusk, and on the midday plateau the curve does not drift at all, so it would
    have stood for hours.

    Re-arming on the observed off is the whole of the fix, and it is also the
    rule the house already runs on: a value set by hand lasts until you switch
    the light off. Doing it on the OFF, rather than on a comparison every tick,
    is what keeps a light somebody is deliberately using from undoing itself a
    minute after they set it.
    """
    release = rejoin = False
    with _state_lock:
        entry = _state.setdefault("bulbs", {}).setdefault(str(node), {})
        # Only a TRANSITION counts. Without this the re-arm below fires on every
        # read of an already-dark bulb - and /api/light's settle loop alone reads
        # ten to twenty times in a second and a half.
        if entry.get("lit") is bool(on):
            return
        entry["lit"] = bool(on)
        held = "override" in entry
        if on:
            # A hold set while the bulb was dark waits its turn: it is ended by
            # the NEXT off, not by the one it was born under.
            if held and not entry.get("heldLit"):
                entry["heldLit"] = True
        elif held and entry.get("heldLit"):
            release = True
        elif not held:
            rejoin = True
    if release:
        release_hold(node, "switched off")
        rearm(node)
    elif rejoin:
        rearm(node)
    else:
        state_save()


def rearm(node) -> None:
    """Put the curve's OnLevel and colour back the moment a hold ends.

    Waiting for the next tick left the manual value standing in the bulb for up
    to SCHED_TICK_SEC - which is precisely the walk from the panel to the light
    switch. Whoever flicked it on inside that window got the manual value and
    every reason to think the release had not worked.

    No MoveToLevel: the bulb is off, that is the whole point. And no read-back,
    because this is called from inside the read path.
    """
    try:
        devices = load_devices()
    except (OSError, ValueError):
        return
    bulb = next((b for b in devices.get("bulbs", []) if b["node"] == node), None)
    if bulb is None:
        return
    lt = time.localtime()
    minute = lt.tm_hour * 60 + lt.tm_min
    level, mireds = curve_at(schedule_for(node, load_schedule_file()), minute)
    if level is None:
        return

    ep = int(bulb.get("endpoint", 1))
    sent = {}
    e = m_write(node, ep, 0x0008, 0x0011, level, timeout=20.0)
    if e:
        log(f"bulb {node}: re-arming OnLevel failed: {e}", "warn")
    else:
        sent["onlevel"] = level
    if mireds:
        # ExecuteIfOff, so the colour lands on a dark bulb too.
        if not m_cmd(node, ep, 0x0300, "MoveToColorTemperature",
                     {"colorTemperatureMireds": mireds, "transitionTime": 4,
                      "optionsMask": 1, "optionsOverride": 1}, timeout=20.0):
            sent["mireds"] = mireds
    if sent:
        with _state_lock:
            _state.setdefault("bulbs", {}).setdefault(str(node), {}).update(sent)
        state_save()
        log(f"bulb {node}: comes up at {level}"
            + (f", {mireds} mireds" if mireds else ""), "ok")


def watch_held() -> None:
    """Read the bulbs being held by hand, so that their off is noticed.

    Nothing else polls a bulb unless a browser is open: refresh_all only touches
    switches, and refresh_bulbs is reached from the page's own request. A held
    bulb is exactly the one whose off we are waiting for, and there is rarely
    more than one, so this is a read or two per tick rather than a sweep of the
    house.
    """
    try:
        devices = load_devices()
    except (OSError, ValueError):
        return
    for b in devices.get("bulbs", []):
        if not overridden(b["node"]):
            continue
        st = state_of(b["node"])
        age = time.time() - float(st.get("readAt") or 0)
        if st.get("ok") is False and age < BULB_COLD_SEC:
            continue    # not answering; do not spend a timeout on it every tick
        try:
            refresh_bulb(b)
        except Exception as exc:  # noqa: BLE001 - one bulb does not stop the rest
            log(f"bulb {b['node']}: hold watch failed: {exc}", "warn")


def apply_light_schedule(force: bool = False, only=None) -> dict:
    """Put the curve's value for right now into the bulbs.

    Three different writes, because they answer three different questions:

      OnLevel      what the bulb comes up at when the switch is pressed. A
                   persistent attribute, so it moves in steps of ONLEVEL_STEP.
      MoveToLevel  what a bulb that is ALREADY LIT should be doing. Deliberately
                   not MoveToLevelWithOnOff: with optionsMask and optionsOverride
                   at 0 the bulb consults its own Options attribute, where
                   ExecuteIfOff is clear - so a lit bulb follows and a dark room
                   stays dark. No state to check and nothing to get wrong.
      colour       carries ExecuteIfOff, so one command covers both cases.

    Only sent when the target has actually moved. That is what keeps a manual
    change from the panel from being stamped on a minute later: while the curve
    is flat the schedule says nothing at all, and only takes the light back when
    it is genuinely ramping.

    The transition time is one tick, so between two samples the bulb glides
    rather than stepping. The schedule is a curve now; it should look like one.

    Returns what happened, not just a count. A bulb that is OFF takes the writes
    and shows nothing - MoveToLevel is ignored while off, on purpose - so
    "applied to 1 bulb" is a true sentence that reads as a lie to somebody
    standing in front of a dark lamp. The caller needs to be able to say which
    it was.

    `only` narrows it to a set of node ids. A save touches the bulbs the saved
    schedule governs and nobody else - writing the house curve into a bulb that
    has its own would undo the override for a minute, which is the sort of thing
    you see once and never trust again.
    """
    try:
        devices = load_devices()
    except (OSError, ValueError):
        return {"written": 0, "lit": 0, "dark": 0, "unknown": 0}

    lt = time.localtime()
    minute = lt.tm_hour * 60 + lt.tm_min
    sched = load_schedule_file()
    out = {"written": 0, "level": None, "mireds": 0,
           "lit": 0, "dark": 0, "unknown": 0, "held": 0}
    if not sched["points"]:
        return out
    only = None if only is None else {int(n) for n in only}
    # How long the bulb takes to get there. Short, and well inside the tick.
    #
    # This used to be one whole tick - 60 deciseconds shy of a minute - on the
    # theory that the light should glide from one sample to the next rather than
    # step. The theory was fine and the arithmetic was not: across twelve columns
    # the curve moves about ONE LEVEL a minute, so there was never anything to
    # glide across. What the long fade actually bought was a minute in which the
    # bulb was in transit while everything that reads it had already arrived -
    # the panel included.
    #
    # That is invisible while the bulb is only a level away from the curve, and
    # ugly the moment it is not. Any real gap to close - a hold released, a
    # restart, a bulb rejoining the schedule after being set by hand - became a
    # full minute of the panel saying 92% over a lamp that was still at 5% and
    # climbing. Measured on the bulb: `move-to-level 254 600` from level 5 read
    # 12, 28, 56, 101 over the first twenty seconds. Every one of those readings
    # was honest, and none of them was what the panel had already decided to
    # show.
    #
    # Two seconds is longer than a step and shorter than a poll, so a fade
    # always finishes before anything looks.
    #
    # On a save, faster still. Somebody has just pressed a button and is looking
    # at the lamp; a slow fade to a value a few percent away is indistinguishable
    # from nothing happening, which is precisely the conclusion they will draw.
    tenths = 4 if force else 20

    # Every bulb in the house, whether a switch drives it or not.
    #
    # This used to walk the switches and then their binding tables, which
    # quietly made "is on the schedule" mean "is wired to a wall switch". Those
    # are two different relationships and only one of them involves a switch: a
    # binding is how the WALL SWITCH commands the bulb with the Pi asleep, and
    # the schedule is the PI commanding the bulb directly over Thread. The Pi
    # never needed a switch in order to do that.
    #
    # It went unnoticed while every new bulb bound itself to whatever switch
    # happened to be listed first. The moment that stopped, a newly added lamp
    # sat outside the schedule entirely - you pressed save globally, it said it
    # had saved, and that one bulb never moved.
    for b in devices.get("bulbs", []):
        key = str(b["node"])
        if only is not None and b["node"] not in only:
            continue
        # Nothing here overrides a hold - not even a save. A save is
        # somebody asking for the schedule out loud, so it RELEASES the
        # holds first and then applies; force means "write even if the memo
        # says the bulb is already there", and nothing more. Conflating the
        # two meant a restart stamped the curve onto a lamp somebody was
        # using, because startup applies with force.
        if overridden(b["node"]):
            out["held"] = out.get("held", 0) + 1
            continue

        # Its own schedule if it has one, otherwise the house's.
        level, mireds = curve_at(schedule_for(b["node"], sched), minute)
        if level is None:
            continue
        out["level"] = level
        out["mireds"] = mireds
        with _state_lock:
            sent = dict(_state.setdefault("bulbs", {}).get(key, {}))
        ep = b.get("endpoint", 1)
        did = False

        # What it comes up at. Stepped, because this one is flash.
        last_on = sent.get("onlevel")
        if force or last_on is None or abs(last_on - level) >= ONLEVEL_STEP:
            e = m_write(b["node"], ep, 0x0008, 0x0011, level)
            if e:
                log(f"bulb {b['node']}: OnLevel failed: {e}", "err")
            else:
                sent["onlevel"] = level
                did = True

        # What it should be doing right now, if it is lit at all.
        if force or sent.get("level") != level:
            e = m_cmd(b["node"], ep, 0x0008, "MoveToLevel",
                      {"level": level, "transitionTime": tenths,
                       "optionsMask": 0, "optionsOverride": 0})
            if e:
                log(f"bulb {b['node']}: live level failed: {e}", "warn")
            else:
                sent["level"] = level
                did = True

        if mireds:
            last_ct = sent.get("mireds")
            if force or last_ct is None or abs(last_ct - mireds) >= MIRED_STEP:
                # optionsMask=1, optionsOverride=1 -> ExecuteIfOff, so the
                # colour lands with the bulb off as well, which is exactly
                # when we need it.
                e = m_cmd(b["node"], ep, 0x0300, "MoveToColorTemperature",
                          {"colorTemperatureMireds": mireds,
                           "transitionTime": tenths,
                           "optionsMask": 1, "optionsOverride": 1})
                if e:
                    log(f"bulb {b['node']}: colour failed: {e}", "err")
                else:
                    sent["mireds"] = mireds
                    did = True

        if did:
            log(f"bulb {b['node']}: {minute // 60:02d}:{minute % 60:02d} "
                f"-> level {level}" + (f", {mireds} mireds" if mireds else ""),
                "step")
            sent["at"] = time.time()
            with _state_lock:
                _state.setdefault("bulbs", {})[key] = sent
            state_save()
            out["written"] += 1

        # Whether any of that was visible. Only worth a read on a manual
        # save, when somebody is watching and about to draw a conclusion.
        if force:
            try:
                refresh_bulb(b)
            except Exception:  # noqa: BLE001 - a dead bulb is not an error here
                pass
        on = state_of(b["node"]).get("on")
        out["lit" if on is True else "dark" if on is False else "unknown"] += 1

    return out


def refresher():
    """Background thread: switch state, rarely; bulb schedule, every minute.

    The schedule is checked often but WRITTEN rarely - only when the slot
    changes. The check is an in-memory comparison, it costs nothing.
    """
    refresh_all("startup")
    apply_light_schedule(force=True)
    next_refresh = time.monotonic() + REFRESH_SEC

    while True:
        requested = _state_wake.wait(SCHED_TICK_SEC)
        _state_wake.clear()

        # Held bulbs first: the tick below skips them, so this is the only
        # thing in the house that would ever notice one being switched off.
        try:
            watch_held()
        except Exception as exc:  # noqa: BLE001
            log(f"hold watch: {exc}", "warn")

        try:
            apply_light_schedule()
        except Exception as exc:  # noqa: BLE001 - a dead bulb does not stop the thread
            log(f"bulb schedule: {exc}", "warn")

        if requested or time.monotonic() >= next_refresh:
            refresh_all("after a write" if requested else "periodic cycle")
            next_refresh = time.monotonic() + REFRESH_SEC


def state_wake():
    _state_wake.set()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # less noise in the console
        pass

    def _send(self, payload, status=200, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Nothing here is cacheable, and one case makes that urgent rather than
        # tidy: a long poll asks the same URL every time, so a browser that is
        # allowed to cache answers it instantly from the last response and never
        # reaches us. The poll looks like it is running - same requests, same
        # rate - and learns nothing, for ever.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send((HERE / "index.html").read_bytes(), ctype="text/html; charset=utf-8")

        elif self.path == "/api/devices":
            d = load_devices()
            d.setdefault("devices", [])
            d["rooms"] = known_rooms(d)
            # What each measurement is called and in what unit, so the interface
            # does not need a second copy of the table.
            d["measures"] = [{"key": k, "label": lab, "unit": u,
                              "words": MEASURE_WORDS.get(k),
                              "hidden": k in MEASURE_HIDDEN}
                             for _, _, k, lab, u, _ in MEASURED]
            d["airQualityWords"] = AIR_QUALITY_WORDS
            self._send(d)

        elif self.path == "/api/updates":
            self._send({str(n): bool(o) for n in all_nodes()
                        for o in [firmware_offer(n)] if o is not None})

        elif self.path.startswith("/api/firmware"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                node = int((q.get("node") or ["0"])[0])
            except ValueError:
                node = 0
            fresh = "fresh=1" in self.path
            self._send(firmware_for(node, fresh=fresh))

        elif self.path.startswith("/api/inspect"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                node = int((q.get("node") or ["0"])[0])
            except ValueError:
                node = 0
            dev = next((d for d in all_devices(load_devices())
                        if int(d.get("node", 0)) == node), None)
            if dev is None:
                return self._send({"error": f"unknown device: {node}"}, status=404)
            out = inspect_node(node)
            out["name"] = dev.get("name")
            out["kind"] = dev.get("kind")
            self._send(out)

        elif self.path == "/api/bindings":
            # From the state held on the Pi, so it is instant. Reading from the
            # device happens in the background - see the "state kept on the Pi"
            # section.
            _devs = load_devices()
            sw_list = switches(_devs)
            out = {}
            # A remote keeps its mapping here, not in the device, so it has no
            # table to read back. The flat list is the union of its buttons, so
            # anything that only wants "what does this control" still works.
            by_button = {}
            remotes = []
            # "reachable" stays honest: it counts the switches whose LAST read
            # succeeded, not the switches we happen to hold an old opinion
            # about. A switch unplugged an hour ago is "not answering", even
            # though we still know its table.
            health = {"total": len(sw_list), "reachable": 0,
                      "matter": _matter_ok}
            for sw in sw_list:
                st = state_of(sw["node"])
                key = str(sw["node"])
                if is_remote(sw):
                    remotes.append(sw["node"])
                    per = remote_buttons(sw)
                    by_button[key] = per
                    seen, flat = set(), []
                    for nodes in per.values():
                        for n in nodes:
                            if n not in seen:
                                seen.add(n)
                                flat.append({"node": n})
                    out[key] = flat
                else:
                    out[key] = st.get("binding") or []
                if st.get("ok"):
                    health["reachable"] += 1
            self._send({"bySwitch": out, "byButton": by_button,
                        "remotes": remotes, "raw": None, "health": health,
                        "rev": state_rev()})

        elif self.path.startswith("/api/lock"):
            out = {}
            for sw in switches(load_devices()):
                st = state_of(sw["node"])
                out[str(sw["node"])] = {"locked": st.get("locked"),
                                        "role": st.get("role")}
            self._send({"bySwitch": out, "rev": state_rev()})

        elif self.path.startswith("/api/bulbs"):
            # Live, unlike everything else here - see "what the bulbs are doing
            # now". `fresh=1` skips the coalescing window, for right after a
            # command when you want the tile to catch up at once.
            fresh = "fresh=1" in self.path
            # A long poll, when the browser asks for one. It says which revision
            # it already has; if that is still the current one we hold the
            # request open until something changes, and answer the moment it
            # does. The tile then moves when the LIGHT moves, rather than on the
            # next tick of whatever interval we picked.
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            wait = float(q.get("wait", [0])[0] or 0)
            since = int(q.get("since", [-1])[0] or -1)
            if wait > 0 and since >= 0:
                wait_for_change(since, min(wait, 30.0))
            self._send({"byNode": refresh_bulbs(force=fresh), "rev": state_rev()})

        elif self.path.startswith("/api/log"):
            since = 0
            if "?" in self.path:
                q = dict(pair.split("=", 1) for pair in self.path.split("?", 1)[1].split("&")
                         if "=" in pair)
                since = int(q.get("since", 0) or 0)
            # The revision counter rides along on the log request, which the UI
            # makes periodically anyway. That way the UI learns something
            # changed with no extra request, and without showing that the Pi is
            # reading.
            self._send({**log_since(since), "rev": state_rev()})

        elif self.path.startswith("/api/schedule"):
            # There is a single schedule, the house's, kept on the Pi. The
            # response keeps the per-switch shape so the UI does not have to
            # change.
            sched = load_schedule_file()
            pts = sched["points"]
            devices = load_devices()
            out = {str(sw["node"]): {"points": pts, "revision": None, "source": "server"}
                   for sw in switches(devices)}

            # Which bulbs a schedule governs, and which schedule governs each -
            # the two questions the editor exists to answer before it lets you
            # change anything. Every bulb, whether a switch drives it or not -
            # the Pi writes the schedule straight to the bulb over Thread, so a
            # binding has nothing to do with which lamps it reaches.
            governed = [{"node": b["node"], "name": b.get("name", ""),
                         "where": b.get("where", ""),
                         "own": str(b["node"]) in sched["bulbs"]}
                        for b in devices.get("bulbs", [])]
            # The clock comes from the Pi, not from the browser. The Pi is what
            # runs the schedule, and the two do not have to agree - a phone
            # roaming abroad would put the "now" marker hours off the slot that
            # is actually in effect.
            lt = time.localtime()
            self._send({"bySwitch": out, "points": pts, "default": SCHED_DEFAULT,
                        "bulbs": sched["bulbs"], "governed": governed,
                        "maxPoints": SCHED_MAX_POINTS, "rev": state_rev(),
                        "now": lt.tm_hour * 60 + lt.tm_min,
                        "tz": time.strftime("%Z")})

        else:
            self._send({"error": "not found"}, status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send({"error": "invalid JSON"}, status=400)

        if self.path == "/api/lock":
            devices = load_devices()
            all_nodes = [x["node"] for x in switches(devices)]
            targets = all_nodes if body.get("all") else [int(n) for n in body.get("switches", [])]
            if not targets:
                return self._send({"error": "no switch selected"}, status=400)

            log(f"--- {'locking' if body.get('locked') else 'unlocking'} "
                f"{len(targets)} switch{'' if len(targets) == 1 else 'es'} ---", "step")
            locked = bool(body.get("locked"))
            results = [set_lock(n, locked) for n in targets]
            failed = [r for r in results if not r.get("ok")]
            return self._send({"results": results, "ok": not failed},
                              status=200 if not failed else 207)

        if self.path == "/api/role":
            node = int(body.get("switch") or 0)
            role = int(body.get("role", ROLE_LIGHT))
            if role not in (ROLE_LIGHT, ROLE_LOCK):
                return self._send({"error": f"unknown role: {role}"}, status=400)
            log(f"--- changing the role of switch {node} ---", "step")
            r = set_role(node, role)

            # devices.json is the local mirror: without it, the panel could not
            # draw the bindings editor with switches instead of bulbs until it
            # had queried every device.
            if r.get("ok"):
                devices = load_devices()
                for sw in devices.get("switches", []):
                    if sw["node"] == node:
                        sw["role"] = ROLE_NAMES[role]
                save_devices(devices)

            return self._send(r, status=200 if r.get("ok") else 502)

        if self.path == "/api/room":
            # Rooms exist only here. Matter has no idea what a room is - it is
            # our grouping, kept in devices.json next to the names, and the whole
            # reason devices.json exists at all.
            #
            # Every operation rewrites the file whole and atomically. There is no
            # partial state to leave behind: renaming a room means touching the
            # room list AND every device in it, and half of that is worse than
            # none of it.
            op = str(body.get("op") or "").strip().lower()
            name = str(body.get("name") or "").strip()
            devices = load_devices()
            rooms = devices.setdefault("rooms", [])
            fold = {r.casefold(): r for r in rooms}

            def members_of(room: str) -> list:
                return [d["node"] for d in all_devices(devices)
                        if (d.get("where") or "").casefold() == room.casefold()]

            def entries(nodes: set):
                """Every entry for those nodes, wherever it lives in the file.

                There are THREE lists a device can be in, and `all_devices`
                knows it. The two writers below each knew about two, so anything
                in the generic list - a sensor, a plug - could be FOUND by a room
                operation and then not moved by it. Deleting its room left it
                pointing at a room that no longer existed, known_rooms derived
                the room straight back from it, and the room reappeared intact
                after a delete that reported success. The same hole meant a
                sensor could not be dragged between rooms at all.
                """
                for group in ("switches", "bulbs", "devices"):
                    for d in devices.get(group, []):
                        if d["node"] in nodes:
                            yield d
                one = devices.get("switch")           # the single-switch layout
                if one and one.get("node") in nodes:
                    yield one

            def move(nodes: set, where: str):
                """Set 'where' on the listed nodes, wherever they live."""
                for d in entries(nodes):
                    d["where"] = where

            if op == "arrange":
                # The order the rooms appear in, top to bottom. Rooms only, not
                # what is in them - `order` below is the one that moves devices.
                #
                # A name we do not know is ignored rather than created, and a
                # room the caller left out keeps its place at the end instead of
                # disappearing: an arrangement sent by a page that was open
                # while a room was added elsewhere must not delete that room.
                want, seen = [], set()
                for r in body.get("rooms") or []:
                    r = str(r).strip()
                    real = fold.get(r.casefold())
                    if real and real.casefold() not in seen:
                        seen.add(real.casefold())
                        want.append(real)
                want += [r for r in known_rooms(devices)
                         if r.casefold() not in seen]
                devices["rooms"] = want
                save_devices(devices)
                log(f"rooms arranged: {', '.join(want) or 'none'}", "ok")
                return self._send({"ok": True, "rooms": want, "devices": devices})

            if op == "order":
                # What a drop is: here is a room, and here is everything in it,
                # in the order it should appear. One op covers both moving a
                # device into a room and rearranging the room, because a drop
                # does both at once and splitting them would mean two writes
                # with a wrong state in between.
                #
                # An empty target is "no room" - a real destination, not a
                # missing argument - so this runs before the name guard.
                want = [int(n) for n in (body.get("nodes") or [])]
                known = {d["node"] for d in all_devices(devices)}
                unknown = [n for n in want if n not in known]
                if unknown:
                    return self._send(
                        {"error": "unknown node(s): " +
                                  ", ".join(str(n) for n in unknown)}, status=400)
                if name and name.casefold() not in fold:
                    rooms.append(name)

                def put(node: int, pos: int):
                    for d in entries({node}):
                        d["where"] = name
                        d["pos"] = pos

                # Only the listed devices are touched. The room a device came
                # FROM keeps its own positions, gaps and all - nothing reads the
                # numbers, only their order.
                for i, node in enumerate(want):
                    put(node, i)
                save_devices(devices)
                devices["rooms"] = known_rooms(devices)
                log(f"{name or 'no room'}: {len(want)} device(s) reordered", "ok")
                return self._send({"ok": True, "rooms": devices["rooms"],
                                   "devices": devices})

            if not name:
                return self._send({"error": "the room has no name"}, status=400)

            if op == "add":
                if name.casefold() in fold:
                    return self._send({"error": f"'{fold[name.casefold()]}' already exists"},
                                      status=409)
                rooms.append(name)
                log(f"room added: {name}", "ok")

            elif op == "rename":
                to = str(body.get("to") or "").strip()
                if not to:
                    return self._send({"error": "the new name is empty"}, status=400)
                if to.casefold() != name.casefold() and to.casefold() in fold:
                    return self._send({"error": f"'{fold[to.casefold()]}' already exists"},
                                      status=409)
                rooms[:] = [to if r.casefold() == name.casefold() else r for r in rooms]
                if to.casefold() not in {r.casefold() for r in rooms}:
                    rooms.append(to)
                # The devices come along. A room is a name, and the devices only
                # ever hold a copy of it - leave them behind and they end up in a
                # room that no longer exists.
                move(set(members_of(name)), to)
                log(f"room renamed: {name} -> {to}", "ok")

            elif op == "remove":
                rooms[:] = [r for r in rooms if r.casefold() != name.casefold()]
                # The devices are not deleted, they merely stop being anywhere.
                # Deleting a room must never look like deleting what is in it.
                freed = members_of(name)
                move(set(freed), "")
                log(f"room deleted: {name} ({len(freed)} device(s) left with no room)", "ok")

            elif op == "members":
                want = {int(n) for n in (body.get("nodes") or [])}
                known = {d["node"] for d in all_devices(devices)}
                unknown = want - known
                if unknown:
                    return self._send(
                        {"error": "unknown node(s): " +
                                  ", ".join(str(n) for n in sorted(unknown))}, status=400)
                if name.casefold() not in fold:
                    rooms.append(name)
                current = set(members_of(name))
                move(want - current, name)
                move(current - want, "")   # taken out of this room, put in none
                log(f"room {name}: {len(want)} device(s)", "ok")

            else:
                return self._send({"error": f"unknown operation: {op or '(none)'}"},
                                  status=400)

            save_devices(devices)
            devices["rooms"] = known_rooms(devices)
            return self._send({"ok": True, "rooms": devices["rooms"],
                               "devices": devices})

        if self.path == "/api/schedule":
            # The schedule is saved on the Pi and applied to the bulbs right
            # away. It is no longer sent to the switch: the switch does not
            # execute it any more, and storing it there would mean syncing
            # unused data to a battery-powered device.
            # A save always has a scope, and it decides two things: what gets
            # written to the file, and which bulbs are touched afterwards.
            #
            #   nodes: [...]        give these bulbs their own schedule
            #   nodes + follow      take it away again; they go back to the house
            #   anything else       the house schedule
            #
            # Writing the house curve into a bulb that has its own would undo
            # the override for a minute, so a scoped save touches only the bulbs
            # the saved schedule actually governs.
            points = body.get("points") or []
            nodes = [int(n) for n in (body.get("nodes") or [])]
            follow = bool(body.get("follow"))
            sched = load_schedule_file()
            devices = load_devices()

            if nodes and follow:
                log(f"--- {len(nodes)} bulb(s) back on the house schedule ---", "step")
                for n in nodes:
                    sched["bulbs"].pop(str(n), None)
                    # Putting a bulb back on the house schedule is asking for
                    # the schedule out loud, exactly as saving one is - so it
                    # releases the hold too. Without this the apply below
                    # silently skipped the bulb it was called for.
                    set_override(n, False)
                save_schedule_file(sched)
                r = apply_light_schedule(force=True, only=nodes)
                return self._send({"ok": True, "scope": "house", "follow": True,
                                   "points": sched["points"], "nodes": nodes,
                                   "applied": r["written"], **r})

            try:
                validate_schedule(points)
            except ValueError as exc:
                return self._send({"error": str(exc)}, status=400)

            if nodes:
                known = {b["node"] for b in devices.get("bulbs", [])}
                unknown = [n for n in nodes if n not in known]
                if unknown:
                    return self._send(
                        {"error": "unknown bulb(s): " +
                                  ", ".join(str(n) for n in unknown)}, status=400)
                log(f"--- saving a schedule for {len(nodes)} bulb(s) ---", "step")
                for n in nodes:
                    sched["bulbs"][str(n)] = points
                save_schedule_file(sched)
                touched = nodes
                scope = "bulbs"
            else:
                log("--- saving the house schedule ---", "step")
                sched["points"] = points
                save_schedule_file(sched)
                # Everyone except the bulbs that opted out of it.
                touched = [b["node"] for b in devices.get("bulbs", [])
                           if str(b["node"]) not in sched["bulbs"]]
                scope = "house"

            # Pressing save is asking for the schedule out loud, so it also
            # releases any light being held by hand.
            for n in touched:
                set_override(n, False)
            r = apply_light_schedule(force=True, only=touched)
            n = r["written"]
            log(f"schedule saved ({scope}), written to {n} bulb"
                f"{'' if n == 1 else 's'} ({r['lit']} lit, {r['dark']} off)",
                "ok" if n else "warn")
            return self._send({"ok": True, "scope": scope, "points": points,
                               "nodes": touched, "applied": n, **r})

        if self.path == "/api/bindings":
            # Writes which bulbs a switch controls. Takes the COMPLETE list.
            sw_node = int(body.get("switch") or 0)
            want = [int(n) for n in body.get("bulbs", [])]
            devices = load_devices()
            sw = next((s for s in switches(devices) if s["node"] == sw_node), None)
            log(f"--- binding switch {sw_node} to {len(want)} bulb"
                f"{'' if len(want) == 1 else 's'} ---", "step")
            if not sw:
                log(f"unknown switch: {sw_node}", "err")
                return self._send({"error": f"unknown switch: {sw_node}"},
                                  status=400)

            if is_remote(sw):
                # Nothing is written to the device, because there is nowhere to
                # write it: no Binding cluster, and no client cluster that could
                # send a command even if there were. Saving the file IS the
                # whole operation, and it takes effect on the next press.
                try:
                    button = str(int(body.get("button")))
                except (TypeError, ValueError):
                    return self._send({"error": "which button?"}, status=400)
                known = {b["node"] for b in devices.get("bulbs", [])}
                keep = [n for n in want if n in known]
                cfg = sw.setdefault("buttons", {})
                if isinstance(cfg.get(button), dict):
                    cfg[button]["bulbs"] = keep
                else:
                    cfg[button] = keep
                save_devices(devices)
                log(f"remote {sw_node} button {button} now drives "
                    f"{len(keep)} bulb{'' if len(keep) == 1 else 's'}", "ok")
                state_wake()
                return self._send({"switch": sw_node, "button": int(button),
                                   "bulbs": keep, "remote": True})
            # A lock does not control bulbs, it controls switches. Different
            # cluster, different endpoint, and the targets need an ACL -
            # switches had none until now, because nobody wrote anything to
            # them.
            if sw.get("role") == "lock":
                targets = [x for x in switches(devices) if x["node"] in want]
                log(f"lock: binding switch {sw_node} to {len(targets)} switches", "step")
                errors = []
                for t in targets:
                    e = write_acl(t["node"], [sw_node])
                    if e:
                        errors.append(f"ACL {t['node']}: {e}")
                # A lock targets endpoint 2 and our own cluster, not endpoint 1
                # and OnOff: it does not turn anything on, it writes a state.
                e = write_binding(sw, [{"node": t["node"],
                                        "endpoint": SCHED_ENDPOINT,
                                        "cluster": int(SCHED_CLUSTER, 16)}
                                       for t in targets])
                if e:
                    errors.append(f"binding {sw_node}: {e}")
                if errors:
                    log(f"finished with {len(errors)} "
                        f"{'error' if len(errors) == 1 else 'errors'}", "err")
                else:
                    log(f"done: lock {sw_node} now locks {len(targets)} switches", "ok")
                state_wake()
                return self._send({"switch": sw_node, "bulbs": want,
                                   "errors": errors or None})

            bulbs = [b for b in devices.get("bulbs", []) if b["node"] in want]
            errors = []

            # The desired state, per switch. We read the other switches' tables
            # once; the one being edited is replaced with what the user asked
            # for.
            desired = {}
            # Locks are excluded: they do not control bulbs, so they have no
            # business in a bulb's ACL.
            others = [x for x in switches(devices)
                      if x["node"] != sw_node and x.get("role") != "lock"]
            # The numbering is computed, not written by hand: the first step is
            # missing when there are no other switches, and a "2/3" with no
            # "1/3" makes you think you lost a line from the console.
            total = 3 if others else 2
            step_no = 0

            if others:
                step_no += 1
                log(f"{step_no}/{total} reading the bindings of the other "
                    f"{len(others)} switches. A bulb's ACL lists ALL the "
                    f"switches that control it, so the ones I am not editing "
                    f"have to be known too - otherwise I would cut off their "
                    f"access.", "step")
            for s2 in switches(devices):
                if s2["node"] == sw_node:
                    desired[s2["node"]] = set(want)
                else:
                    table, _e = read_binding(
                        s2["node"],
                        s2.get("entry_endpoint", s2.get("endpoint", 1)))
                    desired[s2["node"]] = {e["node"] for e in (table or [])}

            # One ACL per bulb: every switch that controls it. A bulb can be
            # controlled by several switches at once.
            all_bulbs = devices.get("bulbs", [])
            step_no += 1
            log(f"{step_no}/{total} rewriting the ACL on all {len(all_bulbs)} "
                f"bulbs. All of them, not just the checked ones: an unchecked "
                f"bulb must also LOSE the right, not merely drop out of the "
                f"switch's table.", "step")
            for b in all_bulbs:
                allowed = sorted(n for n, s3 in desired.items()
                                 if b["node"] in s3)
                log(f"bulb {b['node']} ({b.get('name', '?')}): controlled by "
                    f"{', '.join(str(x) for x in allowed) or 'no switch'}", "info")
                e = write_acl(b["node"], allowed)
                if e:
                    errors.append(f"ACL {b['node']}: {e}")

            step_no += 1
            log(f"{step_no}/{total} writing the binding table on switch "
                f"{sw_node}. The write REPLACES the whole table, so the "
                f"complete list is sent, not just the difference.", "step")
            log(f"the switch is sleepy with a 15 s poll - it may take a few "
                f"seconds to answer", "step")
            e = write_binding(sw, binding_entries(sw, bulbs))
            if e:
                errors.append(f"binding {sw_node}: {e}")

            if errors:
                log(f"finished with {len(errors)} "
                    f"{'error' if len(errors) == 1 else 'errors'}", "err")
                # The write failed somewhere, so we do not know what is left on
                # the switch. We re-read it in the background instead of
                # assuming.
                state_wake()
            else:
                log(f"done: switch {sw_node} now controls {len(want)} bulb"
                    f"{'' if len(want) == 1 else 's'}", "ok")
                state_put(sw_node,
                          values={"binding": binding_entries(sw, bulbs)},
                          meta={"readAt": time.time(), "okAt": time.time(),
                                     "ok": True, "err": None})

            return self._send({"switch": sw_node, "bulbs": want,
                               "errors": errors or None})

        if self.path == "/api/commission":
            log("--- commissioning ---", "step")
            # Adds a brand-new device to our fabric.
            #
            # kind="bulb"   -> bulb: has a printed code, commissioned with the
            #                  payload
            # kind="switch" -> switch: it is OUR device, with no printed code.
            #                  We use the passcode + discriminator from the
            #                  firmware.
            #
            # BLE only: a factory-new device is on no network at all, so it has
            # to be within Bluetooth range OF THE PI, which is what runs
            # chip-tool. Simplest is to bring the bulb next to the Pi.
            kind = body.get("kind", "bulb")
            code = str(body.get("code", "")).strip().replace("-", "").replace(" ", "")
            name = str(body.get("name", "")).strip()
            where = str(body.get("where", "")).strip()
            if not name:
                return self._send({"error": "give it a name"}, status=400)
            if kind != "switch" and not code:
                return self._send({"error": "the pairing code is missing"},
                                  status=400)

            if not THREAD_DATASET.exists():
                return self._send({"error":
                    f"the Thread dataset is missing ({THREAD_DATASET}). "
                    "Run this first: ./scripts/commission.sh dataset <hex>"},
                    status=400)
            dataset = THREAD_DATASET.read_text().strip()

            devices = load_devices()
            bulbs = devices.setdefault("bulbs", [])
            sws = devices.setdefault("switches", switches(devices))
            others = devices.setdefault("devices", [])
            devices.pop("switch", None)
            used = {d["node"] for d in all_devices(devices)}

            node = int(body.get("node") or 0)
            if not node:
                # Two blocks, because there are two commissioning paths: ours,
                # which has no printed code, and everything else. What the
                # device turns out to BE is not known yet and cannot be, so it
                # does not get to pick the number.
                base = 2000 if kind == "switch" else 1000
                same = [n for n in used if base < n < base + 1000]
                node = max(same + [base]) + 1
            if node in used:
                return self._send({"error": f"node ID {node} is already in use"},
                                  status=400)

            # Commissioning takes a while: BLE, then joining Thread, then CASE.
            #
            # A switch's code is compiled into its firmware, so there is no
            # label to read; everything else brings its own printed on the box.
            pair_code = (manual_code(int(SWITCH_PASSCODE),
                                     int(SWITCH_DISCRIMINATOR))
                         if kind == "switch" else code)

            # Search BLE *and* the network, always.
            #
            # This used to ask for network-only unless the device was one of our
            # switches, on the theory that everything else arrives already on
            # Thread - shared out of the ecosystem that set it up. That holds
            # for a bulb bought for another hub and fails completely for a
            # FACTORY-NEW device, which is on no network at all and can only be
            # reached over BLE. The failure says "Discovery timed out" after
            # thirty seconds, which sounds like the device is out of range
            # rather than like it was never looked for.
            #
            # Searching both costs nothing when the device is already on the
            # network - it is found there - so there is no reason to guess.
            network_only = False

            # matter-server cannot hand over a network it has not been given,
            # and a device reached over BLE needs the credentials to join.
            try:
                MS.call("set_thread_dataset", {"dataset": dataset}, timeout=30.0)
            except MatterError as exc:
                log(f"could not give matter-server the Thread dataset: {exc}",
                    "err")
                return self._send({"error": str(exc)}, status=502)

            if BYPASS_ATTESTATION:
                # Logged on EVERY commissioning, on purpose: a security
                # weakening that is visible nowhere gets forgotten, and six
                # months later nobody knows why any device gets through.
                log("attestation certificate verification is DISABLED "
                    "(PANEL_BYPASS_ATTESTATION)", "warn")

            try:
                resp = MS.call("commission_with_code",
                               {"code": pair_code, "network_only": network_only},
                               timeout=300.0)
            except MatterError as exc:
                log(f"commissioning failed - {exc}", "err")
                return self._send({"error": str(exc)}, status=502)

            # matter-server picks the node id on its own fabric; ours stays ours,
            # and msNode is the only thing tying the two together.
            ms_node_id = (resp or {}).get("node_id")
            if ms_node_id is None:
                return self._send({"error": "commissioned, but no node id came "
                                            "back"}, status=502)
            log(f"commissioned as matter-server node {ms_node_id}", "ok")

            # If the room is new we remember it - otherwise it disappears when
            # you delete the last bulb in it, and comes back spelled differently
            # next time.
            rooms = devices.setdefault("rooms", [])
            if where and where.casefold() not in {r.casefold() for r in rooms}:
                rooms.append(where)

            # msNode is written at the same moment the device is created, not
            # patched in afterwards: without it nothing can address the device
            # at all, since every read and every command goes by matter-server's
            # numbering.
            entry = {"node": node, "endpoint": 1, "name": name, "where": where,
                     "msNode": int(ms_node_id)}

            if kind == "switch":
                sws.append(entry)
            else:
                # Ask the device what it is rather than being told. This is the
                # only description that cannot go stale, because it comes from
                # the device and not from whoever filled the form.
                try:
                    entry["desc"] = describe_device(node, ms=ms_node_id)
                except Exception as exc:  # noqa: BLE001 - a mute device is still added
                    log(f"node {node}: could not read its descriptor: {exc}", "warn")
                    entry["desc"] = {"types": [], "endpoints": {}}
                entry["kind"] = device_type_name(entry["desc"])
                is_light = bool(set(entry["desc"].get("types") or []) & LIGHT_TYPES)
                log(f"node {node} says it is a {entry['kind']}", "ok")

                if is_light:
                    entry["caps"] = detect_caps(node, ms=ms_node_id)
                    bulbs.append(entry)
                    kind = "bulb"     # from here on it is wired like one
                elif is_remote(entry):
                    # Pressed, not read. It belongs with the switches even
                    # though nothing can be written into it.
                    entry["remote"] = True
                    sws.append(entry)
                    kind = "remote"
                else:
                    others.append(entry)

            save_devices(devices)

            if kind == "remote":
                # No ACL and no binding table: there is nothing in the device to
                # write them to. What it controls is chosen in the panel and
                # kept here, so it is ready to be edited immediately.
                state_wake()
                return self._send({"node": node, "name": name,
                                   "kind": entry["kind"], "remote": True,
                                   "desc": entry["desc"]})

            if kind not in ("switch", "bulb"):
                # Nothing else to wire: an ACL and a binding are what a SWITCH
                # needs in order to command a BULB. A sensor is read, not
                # commanded, and giving it either would be cargo cult.
                state_wake()
                return self._send({"node": node, "name": name, "kind": entry["kind"],
                                   "desc": entry["desc"]})

            # A new switch does not control anything yet - the bindings are made
            # from the panel.
            if kind == "switch":
                # New node: we know nothing about it, so we read it in the
                # background.
                state_wake()
                return self._send({"node": node, "name": name, "kind": "switch"})

            # A new bulb is wired to nothing, and that is the whole change.
            #
            # It used to bind itself here, to `sws[0]` - the first switch in the
            # file. Not the switch in the same room, not a switch anybody chose:
            # whichever one happened to be listed first. Add a lamp in the
            # kitchen and it answered a button in the hall. And because a binding
            # write REPLACES the entire table, the same call also handed that
            # switch every other bulb in the house, so a switch that drove one
            # lamp silently started driving three.
            #
            # A new switch has always said "the bindings are made from the
            # panel"; there was never a reason for a bulb to be different. The
            # bindings page writes the ACLs as well as the tables - including
            # revoking the ones that should no longer be there - so nothing is
            # lost by waiting to be asked.
            state_wake()
            return self._send({"node": node, "name": name,
                               "kind": entry.get("kind", "bulb"),
                               "unbound": True,
                               "switches": len(sws)})

        if self.path == "/api/rename":
            """Change a device's name. Ours alone - Matter never sees it.

            The name is set once at commissioning and was never editable after,
            which is fine right up until the second bulb of the same model
            arrives and is also called "Light 1".
            """
            node = int(body.get("node") or 0)
            name = str(body.get("name") or "").strip()
            if not node:
                return self._send({"error": "missing 'node'"}, status=400)
            if not name:
                return self._send({"error": "the name cannot be empty"}, status=400)
            if len(name) > 40:
                return self._send({"error": "at most 40 characters"}, status=400)

            devices = load_devices()
            found = False
            # All three lists plus the single-switch layout - the same lesson the
            # room operations had to learn: a sensor lives in neither of the two
            # you first think of.
            for group in ("switches", "bulbs", "devices"):
                for d in devices.get(group, []):
                    if d.get("node") == node:
                        d["name"] = name
                        found = True
            one = devices.get("switch")
            if one and one.get("node") == node:
                one["name"] = name
                found = True
            if not found:
                return self._send({"error": f"unknown device: {node}"}, status=404)

            save_devices(devices)
            log(f"node {node} is now called {name!r}", "ok")
            return self._send({"node": node, "name": name})

        if self.path == "/api/forget":
            """Take a device off the network and out of the house.

            Four things, in an order that matters:

              1. out of every binding table that names it, WHILE THE SWITCHES
                 CAN STILL BE REACHED. After the unpair the removed node is
                 gone, but a switch left pointing at it keeps sending to an
                 address nothing answers - and a stale entry in a table you
                 cannot see is worse than a device you can.
              2. out of the ACLs, if what is leaving is a switch.
              3. unpaired, which is what actually removes our fabric from the
                 device and lets it be commissioned somewhere else.
              4. out of devices.json, out of the schedule and out of the state.

            A device that does not answer is still removable. Unpairing it is
            the only step that needs it alive, so a failure there is reported
            and the rest goes ahead: refusing to forget a lamp you have already
            thrown away would leave the house permanently wrong.
            """
            node = int(body.get("node") or 0)
            if not node:
                return self._send({"error": "missing 'node'"}, status=400)
            devices = load_devices()
            entry = next((d for d in all_devices(devices) if d["node"] == node), None)
            if entry is None:
                return self._send({"error": f"unknown device: {node}"}, status=404)

            name = entry.get("name") or str(node)
            was_switch = any(x["node"] == node for x in devices.get("switches", []))
            log(f"--- forgetting {name} (node {node}) ---", "step")
            warnings = []

            # 1. unbind, from whoever points at it
            for sw in switches(devices):
                if sw["node"] == node:
                    continue
                ep = sw.get("entry_endpoint", sw.get("endpoint", 1))
                table, _e = read_binding(sw["node"], ep)
                have = {e["node"] for e in (table or [])}
                if node not in have:
                    continue
                keep = [b for b in devices.get("bulbs", [])
                        if b["node"] in have and b["node"] != node]
                log(f"unbinding it from {sw.get('name', sw['node'])}", "step")
                err = write_binding(sw, binding_entries(sw, keep))
                if err:
                    warnings.append(f"unbinding from {sw['node']}: {err}")

            # 2. and out of the bulbs' ACLs, if a switch is what is leaving
            if was_switch:
                for b in devices.get("bulbs", []):
                    if b["node"] == node:
                        continue
                    others = [x["node"] for x in switches(devices) if x["node"] != node]
                    allowed = []
                    for s2 in others:
                        ep2 = next((y.get("entry_endpoint", y.get("endpoint", 1))
                                    for y in switches(devices) if y["node"] == s2), 1)
                        table, _e = read_binding(s2, ep2)
                        if b["node"] in {e["node"] for e in (table or [])}:
                            allowed.append(s2)
                    err = write_acl(b["node"], sorted(allowed))
                    if err:
                        warnings.append(f"ACL {b['node']}: {err}")

            # 3. off the fabric
            err = None
            ms = ms_of(node)
            if ms is None:
                err = "not on matter-server"
            else:
                try:
                    MS.call("remove_node", {"node_id": ms}, timeout=90.0)
                except MatterError as exc:
                    err = str(exc)
            if err:
                warnings.append(f"unpair: {err}")
                log(f"could not unpair {node}: {err} - removing it here anyway. "
                    f"The device still holds our fabric and will have to be "
                    f"factory reset before anything else can commission it.", "warn")
            else:
                log(f"node {node} unpaired", "ok")

            # 4. out of the house
            for key in ("switches", "bulbs", "devices"):
                devices[key] = [d for d in devices.get(key, [])
                                if d.get("node") != node]
            save_devices(devices)

            sched = load_schedule_file()
            if sched["bulbs"].pop(str(node), None) is not None:
                save_schedule_file(sched)
            with _state_lock:
                _state.get("nodes", {}).pop(str(node), None)
                _state.get("bulbs", {}).pop(str(node), None)
            state_save()

            log(f"{name} is gone" + (f" ({len(warnings)} warning"
                f"{'' if len(warnings) == 1 else 's'})" if warnings else ""),
                "warn" if warnings else "ok")
            state_wake()
            return self._send({"node": node, "name": name,
                               "unpaired": not err,
                               "warnings": warnings or None})

        if self.path == "/api/share":
            log("--- opening the commissioning window ---", "step")
            # Opens a commissioning window so you can add the bulb to another
            # ecosystem (Apple Home) WITHOUT removing it from ours. Matter
            # allows several fabrics at once.
            node = body.get("node")
            minutes = int(body.get("minutes", 10))
            if node is None:
                return self._send({"error": "'node' is missing"}, status=400)

            # 180 s is the floor for an Enhanced Commissioning Window, not 60.
            # Below it the device answers INVALID_COMMAND - which reads like a
            # malformed request and sends you looking at the wrong things
            # entirely. The spec caps it at 900 s.
            seconds = max(180, min(900, minutes * 60))

            ms = ms_of(node)
            if ms is None:
                return self._send({"error": f"node {node} is not on "
                                            f"matter-server"}, status=400)
            try:
                res = MS.call("open_commissioning_window",
                              {"node_id": ms, "timeout": seconds}, timeout=90.0)
            except MatterError as exc:
                return self._send({"error": str(exc)}, status=502)

            # The codes come back as the RESULT of the command. They used to be
            # scraped out of chip-tool's stdout log, because its WebSocket reply
            # did not carry them - which meant the whole feature depended on log
            # rotation not having happened at the wrong moment.
            codes = {"manual": (res or {}).get("setup_manual_code"),
                     "qr": (res or {}).get("setup_qr_code")}
            if not codes.get("qr"):
                return self._send({
                    "error": "the window opened, but no codes came back",
                    "raw": res,
                }, status=500)

            return self._send({
                "manual": codes["manual"],
                "qr": codes["qr"],
                "svg": qr_svg(codes["qr"]),
                "expires_in": seconds,
            })

        if self.path == "/api/light":
            # Turn a bulb on or off from the panel.
            #
            # This does not go through the switch: it is a direct command from
            # the Pi to the bulb, so it works even when the switch is asleep or
            # not bound yet. Useful when debugging, too - it separates "the bulb
            # does not answer" from "the switch does not send".
            node = int(body.get("node") or 0)
            endpoint = int(body.get("endpoint", 1))
            action = str(body.get("action", "")).strip().lower()
            level = body.get("level")
            mireds = body.get("mireds")
            if action and action not in ("on", "off", "toggle"):
                return self._send({"error": "unknown action"}, status=400)
            if not action and level is None and mireds is None:
                return self._send({"error": "nothing to do"}, status=400)
            if not node:
                return self._send({"error": "missing 'node'"}, status=400)

            what = action or ("level " + str(level) if level is not None else "") \
                          or ("colour " + str(mireds))
            log(f"--- bulb {node}: {what} ---", "step")

            if action:
                err = m_cmd(node, endpoint, 0x0006, ONOFF_CMD[action])
                if err:
                    log(f"bulb {node}: {action} failed: {err}", "err")
                    return self._send({"error": err}, status=502)

            # Brightness on its own, with-on-off so asking for light gives light
            # rather than silently arming a bulb that is switched off.
            if level is not None:
                lvl = max(LEVEL_MIN, min(254, int(level)))
                err = m_cmd(node, endpoint, 0x0008, "MoveToLevelWithOnOff",
                            {"level": lvl, "transitionTime": 0,
                             "optionsMask": 0, "optionsOverride": 0})
                if err:
                    log(f"bulb {node}: level {lvl} failed: {err}", "err")
                    return self._send({"error": err}, status=502)

                # And what it comes up at, or setting the brightness by hand
                # would not survive the switch being pressed.
                #
                # MoveToLevel changes CurrentLevel and nothing else, so a bulb
                # set to 100% here still came back on at whatever the schedule
                # last wrote - you turned it off and on and it was dimmer, with
                # no cause visible anywhere. The schedule still takes OnLevel
                # back when its curve moves, which is the documented bargain;
                # what it should not do is take it back the instant you touch
                # the light.
                #
                # One write per gesture: the slider sends on release, so this is
                # not the flash-wear case the schedule's threshold exists for.
                e = m_write(node, endpoint, 0x0008, 0x0011, lvl)
                if e:
                    log(f"bulb {node}: OnLevel {lvl} failed: {e}", "warn")
                else:
                    # The schedule's memo has to agree, or its next tick sees a
                    # value it did not write and steps on this one.
                    with _state_lock:
                        memo = _state.setdefault("bulbs", {}).setdefault(str(node), {})
                        # Both, because the schedule compares against both: one
                        # guards OnLevel, the other guards the live MoveToLevel.
                        # Recording only OnLevel left the schedule believing the
                        # bulb was still at the curve's brightness.
                        memo["onlevel"] = lvl
                        memo["level"] = lvl
                set_override(node, True)

            # optionsMask=1, optionsOverride=1 -> ExecuteIfOff, so the colour
            # lands even with the bulb off, which is when it matters most.
            if mireds is not None:
                mir = max(100, min(700, int(mireds)))
                err = m_cmd(node, endpoint, 0x0300, "MoveToColorTemperature",
                            {"colorTemperatureMireds": mir, "transitionTime": 0,
                             "optionsMask": 1, "optionsOverride": 1})
                if err:
                    log(f"bulb {node}: colour {mir} failed: {err}", "err")
                    return self._send({"error": err}, status=502)
                with _state_lock:
                    _state.setdefault("bulbs", {}).setdefault(str(node), {})["mireds"] = mir
                set_override(node, True)

            # Read the state back - all of it, through the same combined read
            # the poll uses. A confirmed command does not mean the bulb did what
            # was asked, and here you are looking straight at the bulb, so you
            # would see the difference. Reading level and colour as well is what
            # turns "it says 254" into something you can check.
            devices = load_devices()
            bulb = next((b for b in devices.get("bulbs", []) if b["node"] == node),
                        {"node": node, "endpoint": endpoint})

            # Let it settle first - after ANY write, not just on and off.
            #
            # A command is acknowledged before the bulb has finished applying
            # it, so a read in that gap returns the value the bulb is LEAVING.
            # This guard only covered on/off, which left the commonest case of
            # all uncovered: a brightness change carries no action, so it got no
            # settle and the read-back reported the PREVIOUS brightness. The
            # panel believed it - it is a read-back, the one number here that is
            # meant to be beyond doubt - and the sheet showed the value you set
            # a gesture ago. Measured: asked 144, reply said 1; asked 8, reply
            # said 144.
            # We now WAIT FOR THE DEVICE TO SAY SO, rather than sleeping a
            # guessed interval and reading whatever is there.
            #
            # The read-back no longer reaches the bulb: it comes out of
            # matter-server's cache, which is filled by the bulb's own reports.
            # That is faster and cheaper, but it changes the failure: a fixed
            # 0.35 s sleep used to be enough for a round trip to complete, and
            # is now simply a bet on the report having arrived. Lost bets showed
            # as "asked for 120, reply says 7" - the value the bulb was leaving,
            # which is exactly the bug this settle was written to prevent.
            #
            # So poll our own cache until it agrees, and give up after a short
            # deadline rather than block the request for ever. A bulb that never
            # reports the value it was given is a real thing to know about, and
            # falling through with the honest current reading says it.
            asked = None if level is None else max(LEVEL_MIN, min(254, int(level)))
            asked_ct = None if mireds is None else max(100, min(700, int(mireds)))
            want_on = {"on": True, "off": False}.get(action)
            deadline = time.time() + 1.5
            while True:
                try:
                    refresh_bulb(bulb)
                except Exception as exc:  # noqa: BLE001
                    log(f"bulb {node}: read-back failed: {exc}", "warn")
                    break
                st = state_of(node)
                settled = (
                    (want_on is None or st.get("on") is want_on)
                    and (asked is None or st.get("on") is False
                         or st.get("level") == asked)
                    and (asked_ct is None or st.get("mireds") == asked_ct)
                    # An On sitting at the floor while OnLevel says otherwise is
                    # a bulb still on its way up - one genuinely dimmed to 1 by
                    # hand has an OnLevel of 1 to match.
                    and not (action in ("on", "toggle") and st.get("on") is True
                             and (st.get("level") or 0) <= 1
                             and (st.get("onlevel") or 0) > 1))
                if settled or time.time() >= deadline:
                    if not settled:
                        log(f"bulb {node}: did not report the new value within "
                            f"1.5 s - showing what it last said", "warn")
                    break
                time.sleep(0.05)
            # The hold is not released here any more. refresh_bulb above
            # does it, along with every other place an off is observed - the
            # wall switch included, which this handler never hears about.
            st = state_of(node)
            log(f"bulb {node}: " +
                ("on" if st.get("on") else "off" if st.get("on") is False else "state unknown") +
                (f", level {st['level']}" if st.get("level") is not None else "") +
                (f", {st['mireds']} mireds" if st.get("mireds") else ""),
                "ok" if st.get("on") is not None else "warn")
            return self._send({"node": node, "action": action, "on": st.get("on"),
                               "level": st.get("level"), "mireds": st.get("mireds"),
                               "onlevel": st.get("onlevel"), "held": overridden(node),
                               "ctMin": st.get("ctMin"), "ctMax": st.get("ctMax")})

        if self.path == "/api/firmware":
            node = int(body.get("node") or 0)
            version = body.get("version")
            if not node or version is None:
                return self._send({"error": "need 'node' and 'version'"}, status=400)
            if node in _ota_running:
                return self._send({"error": "an update is already running for "
                                            "this device"}, status=409)
            fw = firmware_for(node)
            if fw.get("availableCode") != version:
                # The offer moved, or somebody is asking for a version that was
                # never offered. Either way, do not push an image at a device on
                # the strength of a stale screen.
                return self._send({"error": "that version is no longer the one "
                                            "on offer - reopen and try again"},
                                  status=409)
            # In the background: this fetches an image and then waits for a
            # sleepy device to take it, which is minutes. The device's own
            # UpdateState is what the panel follows meanwhile.
            threading.Thread(target=firmware_update, args=(node, version),
                             name=f"ota-{node}", daemon=True).start()
            return self._send({"node": node, "started": True,
                               "version": fw.get("available")})

        if self.path == "/api/identify":
            node = body.get("node")
            endpoint = body.get("endpoint", 1)
            seconds = int(body.get("seconds", 15))
            log(f"--- identify on node {node}, {seconds} s ---", "step")
            if node is None:
                return self._send({"error": "'node' is missing"}, status=400)
            # Two commands on the same cluster, because devices disagree about
            # which one lights the lamp.
            #
            # `Identify` sets IdentifyTime and lets the device decide what to do
            # with it. A bulb blinks. An IKEA MYGGBETT accepts it - IdentifyTime
            # counts down, so the command certainly lands - and shows nothing,
            # even though it reports IdentifyType 2, VisibleIndicator, and does
            # blink from IKEA's own app. The difference is TriggerEffect: a
            # discrete "blink now" rather than a duration to interpret.
            #
            # So: the duration first, then the effect. A device that does not
            # implement TriggerEffect answers with an error and is otherwise
            # unaffected, which is why that error is logged and not returned -
            # the identify itself already succeeded.
            err = m_cmd(node, endpoint, 0x0003, "Identify",
                        {"identifyTime": seconds})
            if err:
                return self._send({"error": err}, status=502)

            # EffectIdentifier 0 = Blink, EffectVariant 0 = default.
            e2 = m_cmd(node, endpoint, 0x0003, "TriggerEffect",
                       {"effectIdentifier": 0, "effectVariant": 0}, timeout=20.0)
            if e2:
                log(f"node {node}: no TriggerEffect ({e2}) - Identify alone", "info")
            return self._send({"ok": True, "effect": not e2})

        self._send({"error": "not found"}, status=404)


if __name__ == "__main__":
    print(f"Panel:         http://0.0.0.0:{PANEL_PORT}")
    print(f"matter-server: {MATTER_WS}")
    print(f"state:         {STATE_FILE} (refresh every {REFRESH_SEC} s)")
    state_load()

    threading.Thread(target=refresher, name="refresher", daemon=True).start()
    threading.Thread(target=event_watch, name="events", daemon=True).start()
    threading.Thread(target=matter_watch, name="matter", daemon=True).start()
    threading.Thread(target=power_watch, name="power", daemon=True).start()
    threading.Thread(target=firmware_watch, name="firmware", daemon=True).start()
    threading.Thread(target=press_watch, name="presses", daemon=True).start()
    try:
        devs = load_devices()
        if migrate_remotes(devs):
            save_devices(devs)
            log("moved a remote out of the sensor list", "ok")
    except (OSError, ValueError) as exc:
        log(f"could not check for remotes: {exc}", "warn")
    ThreadingHTTPServer(("0.0.0.0", PANEL_PORT), Handler).serve_forever()
