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

Start with: ./run.sh  (starts both chip-tool and this server)
"""

import atexit
import collections
import json
import math
import os
import pathlib
import random
import re
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from websockets.sync.client import connect as ws_connect
except ImportError:
    raise SystemExit(
        "The 'websockets' package is missing.\n"
        "  pip install websockets\n"
    )

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
CHIP_TOOL_WS = os.environ.get("CHIP_TOOL_WS", "ws://127.0.0.1:9002")
PANEL_PORT = int(os.environ.get("PANEL_PORT", "8080"))

# chip-tool's log, redirected there by run.sh.
#
# Why we need it: the pairing codes (manual + QR) do NOT come back in the JSON
# response on the WebSocket - CommissioningWindowOpener.cpp prints them with
# ChipLogProgress, so they land on the process's stdout. We read them from there.
#
# The alternative would have been to run a second chip-tool as a subprocess, but
# then two processes would write to the same storage - exactly what you do not
# want with the fabric credentials.
CHIP_TOOL_LOG = pathlib.Path(
    os.environ.get("CHIP_TOOL_LOG", str(HERE.parent / "ota" / "state" / "chip-tool.log")))

# Skips attestation certificate verification during commissioning.
#
# WHY THIS EXISTS. chip-tool checks the device's signature against the
# attestation roots (PAA) in --paa-trust-store-path. The set shipped with the
# SDK holds 40 authorities and does NOT include IKEA (vendor 0x117C), so every
# IKEA bulb fails with
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

# ------------------------------------------------ keeping chip-tool asleep
#
# chip-tool burns a whole CPU core while completely idle. It is not doing work:
# examples/common/websocket-server/WebSocketServer.cpp runs lws_service(ctx, -1)
# in a while loop, and libwebsockets inverts the POSIX convention - a negative
# timeout means "do not block" rather than "block forever", so the loop spins.
# Reported upstream as connectedhomeip#29971 (October 2023), still open.
#
# Measured on this installation: 100% of one core for eight days straight, which
# also held the Pi 3B+ at its 60 C soft-throttle point, running at 1200 MHz
# instead of 1400. So the busy loop costs roughly 1.5 W and 14% of the clock.
#
# Until the upstream loop is fixed we SIGSTOP the process when nothing has used
# it for a while, and SIGCONT it just before we do. Frozen, it uses no CPU at
# all while keeping its memory, its sockets and its CASE sessions - which is why
# this is better than stopping the service: no restart latency, and the fabric
# storage is never touched, so it cannot be corrupted by a badly timed shutdown.
#
# The safety property that matters: a frozen chip-tool looks exactly like a dead
# one from outside. So we thaw unconditionally when the panel starts and when it
# stops, and we never freeze while a command is in flight.
CHIP_IDLE_SEC = int(os.environ.get("PANEL_CHIP_IDLE_SEC", "120"))
CHIP_PROC_MATCH = os.environ.get("PANEL_CHIP_PROC", "chip-tool interactive")

_freeze_lock = threading.Lock()
_frozen = False
_last_use = time.monotonic()


def _chip_pid():
    """PID of the running chip-tool, or None. Re-read every time: the service
    can restart under us, and signalling a recycled PID would be worse than
    doing nothing."""
    try:
        out = subprocess.run(["pgrep", "-f", CHIP_PROC_MATCH],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    pids = [int(x) for x in out.stdout.split() if x.isdigit()]
    return pids[0] if pids else None


def _signal_chip(sig: int) -> bool:
    pid = _chip_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, sig)
        return True
    except OSError as exc:
        log(f"cannot signal chip-tool (pid {pid}): {exc}", "warn")
        return False


def chip_thaw(force: bool = False):
    """Make sure chip-tool is runnable, and reset the idle timer.

    force=True signals unconditionally. That matters at startup and at
    shutdown: a chip-tool left frozen by a PREVIOUS run of the panel is not
    tracked by our _frozen flag, which starts False in a fresh process - so the
    guarded path would do nothing and leave a Matter admin that accepts
    connections and answers none of them. SIGCONT to an already-running process
    is a no-op, so forcing costs nothing.
    """
    global _frozen, _last_use
    with _freeze_lock:
        _last_use = time.monotonic()
        if _frozen or force:
            _signal_chip(signal.SIGCONT)
            _frozen = False


def chip_freeze_if_idle():
    global _frozen
    with _freeze_lock:
        if _frozen or time.monotonic() - _last_use < CHIP_IDLE_SEC:
            return
        # Never freeze mid-command. _ws_lock is held for the whole exchange.
        if _ws_lock.locked():
            return
        if _signal_chip(signal.SIGSTOP):
            _frozen = True


def freezer():
    """Background thread. Cheap: a clock comparison every few seconds."""
    while True:
        time.sleep(5)
        try:
            chip_freeze_if_idle()
        except Exception as exc:  # noqa: BLE001 - must never kill the thread
            log(f"freezer: {exc}", "warn")


# chip-tool does not support concurrent commands on the same socket.
_ws_lock = threading.Lock()



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


def chip_error(resp: dict):
    """The error from a chip-tool response, or None.

    chip-tool reports a failure in TWO different places, and it cost us dearly:

      at the top level  {"error": "..."}       - we never reached the device
      nested inside     {"results": [{"error": "FAILURE"}]}  - it answered "no"

    A failed commissioning has the second shape. Checking only the first, the
    panel reported "bulb added" for a bulb that had been rejected at attestation
    verification, wrote it into the registry, and went on to write its ACL and
    binding. We then went looking for that bulb on the Thread network, where it
    could not possibly be.
    """
    if resp.get("error"):
        return resp["error"]
    for res in resp.get("results", []):
        if isinstance(res, dict) and res.get("error"):
            return res["error"]
    return None


def _short(resp: dict, limit: int = 220) -> str:
    """chip-tool's full response is unreadable in a console a few lines tall.
    We keep only the part that says whether it worked."""
    err = chip_error(resp)
    if err:
        return str(err)[:limit]
    txt = json.dumps(resp, separators=(",", ":"))
    return txt[:limit] + ("..." if len(txt) > limit else "")


def chip(command: str, timeout: float = 30.0) -> dict:
    """Send a command to chip-tool and return the JSON response.

    chip-tool gets a SHORTER deadline than we do, and that is not a refinement -
    it is what keeps the whole panel from wedging.
    Without --timeout, chip-tool uses its own deadline, longer than ours. When we
    give up and close the socket, it KEEPS working on the command. Its WebSocket
    server is single-threaded, so while it is busy it cannot complete new
    connections - hence "timed out while waiting for handshake response" on
    anything else you try meanwhile, and the impression that it died. And when it
    does finish, the late response goes out on the CURRENT connection, so it can
    land on a different request than the one that produced it.

    With the deadline handed to it, it always answers first, with a clean error,
    and nothing is left in flight.
    """
    if "--timeout" not in command:
        command = f"{command} --timeout {max(5, int(timeout) - 8)}"

    chip_thaw()
    log(f"$ {command}", "cmd")
    t0 = time.monotonic()

    # Connecting and waiting for the response are handled SEPARATELY, on
    # purpose.
    #
    # "cannot connect to chip-tool" and "chip-tool took the command but nobody
    # answered" look the same as exceptions, yet they mean completely different
    # things: the first is a problem on the Pi, the second is on the radio. Mix
    # them up and the panel says "chip-tool is down" when the RCP radio is the
    # thing that is missing - and you spend hours looking in the wrong place.
    with _ws_lock:
        try:
            # ping_interval=None: chip-tool does not answer pings while it is
            # executing a command, and the library's keepalive was closing the
            # connection from our side after ~50 s - that is exactly the orphan
            # the --timeout above avoids. The recv deadline has to be the only
            # one in charge.
            conn = ws_connect(CHIP_TOOL_WS, open_timeout=5, ping_interval=None)
            ws = conn.__enter__()
        except Exception as exc:  # noqa: BLE001 - we want the raw message in the UI
            out = {"error": f"{type(exc).__name__}: {exc}", "command": command,
                   "transport": True}
            log(f"chip-tool unreachable after {time.monotonic() - t0:.1f}s: "
                f"{out['error']}", "err")
            return out

        try:
            ws.send(command)
            raw = ws.recv(timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            out = {"error": f"{type(exc).__name__}: {exc}", "command": command}
            log(f"no response after {time.monotonic() - t0:.1f}s: "
                f"{out['error']}", "err")
            return out
        finally:
            conn.__exit__(None, None, None)

    try:
        resp = json.loads(raw)
    except json.JSONDecodeError:
        resp = {"error": "response was not JSON", "raw": str(raw)[:2000]}

    dt = time.monotonic() - t0
    log(f"{dt:.1f}s  {_short(resp)}", "err" if chip_error(resp) else "ok")
    chip_thaw()   # refresh the idle timer; the command just finished
    return resp


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
    0x0044: "PM2.5 sensor", 0x0043: "PM1 sensor", 0x0045: "PM10 sensor",
    0x0042: "carbon monoxide sensor", 0x0041: "carbon dioxide sensor",
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
    (0x0400, 0x0000, "illuminance",  "light",        "lx",       1),
]

AIR_QUALITY_WORDS = ["unknown", "good", "fair", "moderate",
                     "poor", "very poor", "extremely poor"]

# The device types that get the light treatment - an ACL so a switch may command
# them, a place in a switch's binding table, and a place in the schedule.
#
# Nobody declares this at the form. A device is commissioned, asked what it is,
# and put in the right list; "is it a bulb?" is a question the bulb can answer
# and the person holding the box often cannot.
LIGHT_TYPES = {0x0100, 0x0101, 0x010C, 0x010D}


def describe_device(node: int) -> dict:
    """Ask a device what it is: its endpoints, their types and their clusters.

    Everything else in the panel is told what a device is when it is added.
    This asks, which is the only way a generic "add anything" can work - and it
    is also the only description that cannot go stale, because it comes from the
    device rather than from whoever typed the form.
    """
    out = {"types": [], "endpoints": {}}
    parts = chip(f"any read-by-id 0x{DESCRIPTOR_CLUSTER:X} "
                 f"0x{DESC_PARTS_LIST_ATTR:X} {node} 0", timeout=30.0)
    eps = _first_attr(parts)
    eps = [int(e) for e in eps] if isinstance(eps, list) else []
    # Endpoint 0 is the node itself and holds no application device type worth
    # showing; if PartsList came back empty we still try endpoint 1, which is
    # where a single-function device puts everything.
    if not eps:
        eps = [1]

    for ep in eps[:8]:          # a sensor with more than eight parts is not a
        r = chip(f"any read-by-id 0x{DESCRIPTOR_CLUSTER:X},0x{DESCRIPTOR_CLUSTER:X} "
                 f"0x{DESC_DEVICE_TYPE_ATTR:X},0x{DESC_SERVER_LIST_ATTR:X} "
                 f"{node} {ep},{ep}", timeout=30.0)   # thing this panel handles
        idx = attr_index(r)
        types = idx.get((DESCRIPTOR_CLUSTER, ep, DESC_DEVICE_TYPE_ATTR), {}).get("value")
        servers = idx.get((DESCRIPTOR_CLUSTER, ep, DESC_SERVER_LIST_ATTR), {}).get("value")
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


def device_type_name(desc: dict) -> str:
    """One phrase for what this is, for a tile that has no icon of its own."""
    named = [DEVICE_TYPE_NAMES[t] for t in desc.get("types", [])
             if t in DEVICE_TYPE_NAMES]
    if named:
        return named[0]
    types = desc.get("types") or []
    return f"device type 0x{types[0]:04X}" if types else "device"


def read_measurements(dev: dict) -> dict:
    """Every measurement this device is known to expose, in one request.

    One request and not one per reading: these are mains-powered Thread routers
    and each read is cheap, but chip-tool is single threaded and the panel holds
    a lock across a command, so ten reads is ten times the window in which
    everything else waits.
    """
    desc = dev.get("desc") or {}
    paths = []
    for ep, info in (desc.get("endpoints") or {}).items():
        have = set(info.get("clusters") or [])
        for cluster, attr, key, label, unit, scale in MEASURED:
            if cluster in have:
                paths.append((cluster, attr, int(ep), key, scale))
    if not paths:
        return {}

    clusters = ",".join(f"0x{c:X}" for c, _, _, _, _ in paths)
    attrs = ",".join(f"0x{a:X}" for _, a, _, _, _ in paths)
    eps = ",".join(str(e) for _, _, e, _, _ in paths)
    resp = chip(f"any read-by-id {clusters} {attrs} {dev['node']} {eps}",
                timeout=BULB_READ_TIMEOUT)
    idx = attr_index(resp)

    out = {}
    for cluster, attr, ep, key, scale in paths:
        entry = idx.get((cluster, ep, attr), {})
        val = entry.get("value")
        if val is None:
            continue
        out[key] = round(val * scale, 2) if scale != 1 else val
    return out


def acl_for(node: int, switch_nodes: list) -> str:
    """A bulb's ACL: the admin plus the switches allowed to control it.

    Manage, not Operate: on/off would work with Operate too, but writing
    StartUpOnOff requires Manage."""
    subjects = ",".join(str(n) for n in switch_nodes)
    return (f'accesscontrol write acl \'['
            f'{{"fabricIndex":1,"privilege":5,"authMode":2,'
            f'"subjects":[{ADMIN_NODE}],"targets":null}}'
            + (f',{{"fabricIndex":1,"privilege":4,"authMode":2,'
               f'"subjects":[{subjects}],"targets":null}}' if subjects else "")
            + f']\' {node} 0')


# ColorControl (0x0300) feature bits, checked against cluster-enums.h.
CC_HUE_SAT = 0x01
CC_XY = 0x08
CC_COLOR_TEMP = 0x10


def detect_caps(node: int, endpoint: int = 1) -> dict:
    """What the bulb can do: color temperature, RGB color, or white only.

    We read the FeatureMap on ColorControl. If the bulb does not have the
    cluster the request fails - and that failure is the answer.
    """
    r = chip(f"colorcontrol read feature-map {node} {endpoint}", timeout=45.0)
    if chip_error(r):
        # Unknown, not "the bulb is plain white". The difference matters: a bulb
        # with color temperature reported as white loses half the schedule.
        return {"ct": None, "color": None}

    fm = None
    def walk(o):
        nonlocal fm
        if fm is not None:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if k.lower() in ("value", "featuremap") and isinstance(v, int):
                    fm = v
                    return
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(r)
    if fm is None:
        return {"ct": False, "color": False}
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
            entries.append({"fabricIndex": 1, "node": b["node"],
                            "endpoint": b.get("endpoint", 1),
                            "cluster": cluster})
    return entries


def binding_for(switch: dict, bulbs: list) -> str:
    return (f"binding write binding '{json.dumps(binding_entries(switch, bulbs))}' "
            f"{switch['node']} {switch.get('endpoint', 1)}")


ADMIN_NODE = int(os.environ.get("ADMIN_NODE", "112233"))
# The test credentials compiled into the switch firmware. See ota/config.sh.
SWITCH_PASSCODE = os.environ.get("SWITCH_PASSCODE", "20202021")
SWITCH_DISCRIMINATOR = os.environ.get("SWITCH_DISCRIMINATOR", "3840")


def known_rooms(devices: dict) -> list:
    """The known rooms: the ones declared explicitly plus the ones devices use.
    The comparison is case-insensitive, so 'Bedroom' does not show up next to
    'bedroom'."""
    seen, out = {}, []
    src = list(devices.get("rooms", []))
    # Every device, switches included. Reading only the bulbs meant a room
    # holding nothing but switches vanished from the list the moment you stopped
    # declaring it explicitly.
    src += [d.get("where", "") for d in all_devices(devices)]
    for r in src:
        r = (r or "").strip()
        if r and r.casefold() not in seen:
            seen[r.casefold()] = r
            out.append(r)
    return sorted(out, key=str.casefold)


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


def commissioning_reason(from_pos: int, timeout: float = 4.0) -> str:
    """The failure reason, from chip-tool's log, starting at from_pos."""
    deadline = time.monotonic() + timeout
    chunk = ""
    while time.monotonic() < deadline:
        try:
            with CHIP_TOOL_LOG.open("r", errors="replace") as fh:
                fh.seek(from_pos)
                chunk = fh.read()
        except OSError:
            chunk = ""
        if "Cleanup" in chunk or "commissioning Failure" in chunk:
            break
        time.sleep(0.3)

    steps = RE_FAILED_STEP.findall(chunk)
    if steps:
        # The first step that failed, not the last: the rest are consequences.
        step, error = steps[0]
        explanation = STEP_EXPLANATIONS.get(step)
        return (f"failed at step '{step}'"
                + (f" - {explanation}" if explanation else f" ({error.strip()})"))

    general = RE_GENERAL_FAILURE.search(chunk)
    if general:
        return general.group(1).strip()

    # No commissioning step started at all = we never reached the device.
    if "Discovered device" not in chunk and "BLE" not in chunk:
        return ("could not find the device. Usually that means it is not in "
                "pairing mode (factory reset it again) or that it is too far "
                "from the Raspberry Pi")
    return ""


def read_pairing_codes(from_pos: int, timeout: float = 5.0) -> dict:
    """Read the codes from the tail of chip-tool's log, starting at from_pos.

    The log lines can arrive slightly after the WebSocket response, so we retry
    briefly instead of reading once.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with CHIP_TOOL_LOG.open("r", errors="replace") as fh:
                fh.seek(from_pos)
                chunk = fh.read()
        except OSError:
            chunk = ""

        manual = RE_MANUAL.search(chunk)
        qr = RE_QR.search(chunk)
        if manual and qr:
            return {"manual": manual.group(1), "qr": qr.group(1)}
        time.sleep(0.3)

    return {"manual": manual.group(1) if manual else None,
            "qr": qr.group(1) if qr else None}


def extract_bindings(response: dict) -> list:
    """
    Pull the binding table out of a chip-tool response.

    The exact JSON shape varies between chip-tool versions, so we search
    recursively for the first list of dicts that looks like a binding table. If
    we find nothing, the UI shows the raw response so you can adjust.

    Two shapes, and we have seen both from the same version:

      named:   {"fabricIndex": 1, "node": 1001, "endpoint": 1, "cluster": 6}
      by ID:   {"254": 1, "1": 1001, "3": 1, "4": 6}

    The second is what chip-tool returns when it reads the attribute by ID and
    does not have the cluster description at hand. Looking only for the 'node'
    key means reporting "0 bindings" for a switch that really does control the
    bulb - which is the expensive mistake: you believe it is not bound and bind
    it again, or go digging through the firmware for nothing.
    """
    # Field IDs from the Binding cluster's (0x001E) TargetStruct.
    FIELDS = {"1": "node", "2": "group", "3": "endpoint", "4": "cluster",
              "254": "fabricIndex"}

    def normalize(entry: dict) -> dict:
        if "node" in entry or "group" in entry:
            return entry
        return {name: entry[field_id] for field_id, name in FIELDS.items()
                if field_id in entry}

    def is_table(obj) -> bool:
        """A binding entry has either a node or a group - in both shapes."""
        return bool(obj) and all(
            isinstance(i, dict) and ({"node", "group", "1", "2"} & i.keys())
            for i in obj)

    found = []

    def walk(obj):
        if isinstance(obj, list):
            if is_table(obj):
                found.extend(normalize(i) for i in obj)
                return
            for item in obj:
                walk(item)
        elif isinstance(obj, dict):
            for value in obj.values():
                walk(value)

    walk(response)
    return found


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
    # the percentages beside them are how bright each one LOOKS, which is
    # what the panel shows and what the curve was drawn in.
    {"min":    0, "level":   1, "fade": 20, "mireds": 454},   # 01:00     4%   2200 K
    {"min":  120, "level":   1, "fade": 20, "mireds": 454},   # 03:00     4%   2200 K
    {"min":  240, "level":   1, "fade": 20, "mireds": 454},   # 05:00     4%   2200 K
    {"min":  360, "level":  22, "fade": 20, "mireds": 370},   # 07:00    35%   2700 K
    {"min":  480, "level": 144, "fade": 20, "mireds": 250},   # 09:00    80%   4000 K
    {"min":  600, "level": 254, "fade": 20, "mireds": 217},   # 11:00   100%   4600 K
    {"min":  720, "level": 254, "fade": 20, "mireds": 208},   # 13:00   100%   4800 K
    {"min":  840, "level": 223, "fade": 20, "mireds": 222},   # 15:00    95%   4500 K
    {"min":  960, "level": 144, "fade": 20, "mireds": 263},   # 17:00    80%   3800 K
    {"min": 1080, "level":  71, "fade": 20, "mireds": 333},   # 19:00    60%   3000 K
    {"min": 1200, "level":  29, "fade": 20, "mireds": 370},   # 21:00    40%   2700 K
    {"min": 1320, "level":   5, "fade": 20, "mireds": 417},   # 23:00    15%   2400 K
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
    # true/false, not 1/0: the attribute is BOOLEAN in the firmware
    # (DECLARE_DYNAMIC_ATTRIBUTE(kLockedAttr, BOOLEAN, ...)), and chip-tool
    # encodes 1/0 as an integer, which the device refuses with
    # CONSTRAINT_ERROR. Verified on hardware: with "1" it fails every time,
    # with "true" it goes through and reads back as True.
    r = chip(f"any write-by-id {SCHED_CLUSTER} {LOCK_ATTR} "
             f"{'true' if locked else 'false'} "
             f"{node} {SCHED_ENDPOINT}", timeout=60.0)
    e = chip_error(r)
    if e:
        return {"node": node, "ok": False, "error": e}

    # Read it back. A confirmed write does not guarantee the value landed, and
    # the mistake is expensive here: you would believe you locked and you did
    # not.
    back = _first_attr(chip(f"any read-by-id {SCHED_CLUSTER} {LOCK_ATTR} "
                             f"{node} {SCHED_ENDPOINT}"))
    if back is not None and bool(back) != locked:
        log(f"node {node}: the write went through but the value reads back as "
            f"{back} - it is NOT locked", "err")
        return {"node": node, "ok": False, "error": "the value did not apply"}

    log(f"node {node}: {'locked' if locked else 'unlocked'}", "ok")
    state_put(node, values={"locked": locked},
              meta={"readAt": time.time(), "ok": True, "err": None})
    return {"node": node, "ok": True, "locked": locked}


def set_role(node: int, role: int) -> dict:
    log(f"node {node}: role -> {ROLE_NAMES.get(role, role)}", "step")
    r = chip(f"any write-by-id {SCHED_CLUSTER} {ROLE_ATTR} {role} "
             f"{node} {SCHED_ENDPOINT}", timeout=60.0)
    e = chip_error(r)
    if e:
        return {"node": node, "ok": False, "error": e}
    state_put(node, values={"role": role},
              meta={"readAt": time.time(), "ok": True, "err": None})
    return {"node": node, "ok": True, "role": role}


def acl_for_switch(node: int, lock_nodes: list) -> str:
    """The ACL of a switch controlled by a lock.

    Switches had no ACL at all until now - nobody wrote anything to them. A
    switch in the lock role does write an attribute on them, so it needs the
    right. Manage, as for bulbs: it is an attribute write, not a command."""
    subjects = ",".join(str(n) for n in lock_nodes)
    return (f'accesscontrol write acl \'['
            f'{{"fabricIndex":1,"privilege":5,"authMode":2,'
            f'"subjects":[{ADMIN_NODE}],"targets":null}}'
            + (f',{{"fabricIndex":1,"privilege":4,"authMode":2,'
               f'"subjects":[{subjects}],"targets":null}}' if subjects else "")
            + f']\' {node} 0')


def lock_binding_for(lock_sw: dict, targets: list) -> str:
    """A lock's binding table: the switches it locks.

    It targets endpoint 2 and the custom cluster, not endpoint 1 and OnOff - a
    lock does not turn anything on, it writes a state."""
    entries = [{"fabricIndex": 1, "node": t["node"], "endpoint": SCHED_ENDPOINT,
                "cluster": int(SCHED_CLUSTER, 16)} for t in targets]
    return (f"binding write binding '{json.dumps(entries)}' "
            f"{lock_sw['node']} {lock_sw.get('endpoint', 1)}")


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
_chip_ok = True   # did the last attempt to talk to chip-tool succeed?


def state_load():
    try:
        d = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
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
    # Atomic write: the panel can be restarted at any moment, and a truncated
    # state file would be worse than a missing one - it would read as "I know
    # nothing" only after parsing had already failed.
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(_state, ensure_ascii=False))
        tmp.replace(STATE_FILE)
    except OSError as exc:  # noqa: BLE001
        log(f"cannot save the state to disk: {exc}", "warn")


def state_of(node) -> dict:
    with _state_lock:
        return dict(_state["nodes"].get(str(node), {}))


def state_rev() -> int:
    with _state_lock:
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
    if changed:
        log(f"node {node}: {', '.join(changed)} changed", "ok")
    state_save()
    return changed


def attr_index(resp: dict) -> dict:
    """(cluster, endpoint, attribute) -> the entry from 'results'.

    With a single path read, "take the first value in results" worked too. With
    several paths in the same response, that could return the binding table as
    "locked" - the expensive error, produced by an optimization. An element with
    no clusterId is the error aggregate and must not be allowed to throw away
    the paths that succeeded.
    """
    out = {}
    for res in resp.get("results", []):
        if "clusterId" not in res:
            continue
        try:
            key = (int(res["clusterId"]), int(res["endpointId"]),
                   int(res["attributeId"]))
        except (KeyError, TypeError, ValueError):
            continue
        out[key] = res
    return out


def _first_attr(resp: dict):
    """The first attribute value in a response that read a single path."""
    for entry in attr_index(resp).values():
        if "value" in entry:
            return entry["value"]
    return None


def read_switch_state(node: int, endpoint: int = 1) -> dict:
    """A switch's entire state in a SINGLE request.

    Three paths: the binding table on endpoint 1, plus locked and role from our
    cluster on endpoint 2. chip-tool pairs the cluster, attribute and endpoint
    lists position by position, not as a cross product, so all three have to be
    the same length.

    Being a single request is the point: sequential reads do not land on the
    same wake-up, because the switch's active window is far shorter than its
    poll interval. Three reads = three wake-ups = ~45 s; one read = ~15 s.
    """
    paths = [(BINDING_CLUSTER_ID, 0x0000, endpoint)]
    paths += [(SCHED_CLUSTER_ID, _ATTRS[k], SCHED_ENDPOINT)
              for k in ("locked", "role")]
    clusters = ",".join(f"0x{c:X}" for c, _, _ in paths)
    attrs = ",".join(f"0x{a:X}" for _, a, _ in paths)
    eps = ",".join(str(e) for _, _, e in paths)
    return chip(f"any read-by-id {clusters} {attrs} {node} {eps}", timeout=45.0)


def refresh_switch(sw: dict) -> list:
    """Read a switch and update the state. Returns which facets changed."""
    global _chip_ok
    node = sw["node"]
    endpoint = sw.get("endpoint", 1)
    resp = read_switch_state(node, endpoint)

    if resp.get("transport"):
        _chip_ok = False
        state_put(node, meta={"readAt": time.time(), "ok": False,
                              "err": resp.get("error")})
        return []
    _chip_ok = True

    idx = attr_index(resp)

    def path(cluster, ep, attr):
        """None = we did not find out. Different from [] or false, which are
        answers."""
        entry = idx.get((cluster, ep, attr))
        if not entry or "value" not in entry:
            return None
        return entry["value"]

    binding_raw = path(BINDING_CLUSTER_ID, endpoint, 0x0000)
    locked_raw = path(SCHED_CLUSTER_ID, SCHED_ENDPOINT, _ATTRS["locked"])
    role_raw = path(SCHED_CLUSTER_ID, SCHED_ENDPOINT, _ATTRS["role"])
    values = {}
    if binding_raw is not None:
        values["binding"] = extract_bindings({"results": [{"value": binding_raw}]})
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
    cl = [ONOFF_CLUSTER_ID, LEVEL_CLUSTER_ID, LEVEL_CLUSTER_ID,
          COLOR_CLUSTER_ID, COLOR_CLUSTER_ID, COLOR_CLUSTER_ID]
    at = [0x0, 0x0, LEVEL_ONLEVEL_ATTR,
          COLOR_TEMP_ATTR, COLOR_CT_MIN_ATTR, COLOR_CT_MAX_ATTR]
    resp = chip(
        "any read-by-id " + ",".join(f"0x{c:X}" for c in cl) + " "
        + ",".join(f"0x{a:X}" for a in at) + f" {node} "
        + ",".join(str(endpoint) for _ in cl),
        timeout=BULB_READ_TIMEOUT)

    idx = attr_index(resp)
    on = idx.get((ONOFF_CLUSTER_ID, endpoint, 0), {}).get("value")
    level = idx.get((LEVEL_CLUSTER_ID, endpoint, 0), {}).get("value")
    onlevel = idx.get((LEVEL_CLUSTER_ID, endpoint, LEVEL_ONLEVEL_ATTR), {}).get("value")
    mireds = idx.get((COLOR_CLUSTER_ID, endpoint, COLOR_TEMP_ATTR), {}).get("value")
    ct_min = idx.get((COLOR_CLUSTER_ID, endpoint, COLOR_CT_MIN_ATTR), {}).get("value")
    ct_max = idx.get((COLOR_CLUSTER_ID, endpoint, COLOR_CT_MAX_ATTR), {}).get("value")

    if on is None:
        # No answer. We keep the last value rather than showing "off" for a bulb
        # that is merely out of range - "stale" is honest, "off" is a lie you
        # would act on.
        return state_put(node, meta={"readAt": time.time(), "ok": False,
                                     "err": chip_error(resp) or "no response"})

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
                     meta={"readAt": time.time(), "ok": True, "err": None})


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
                     meta={"readAt": time.time(), "ok": True, "err": None})


def refresh_bulbs(force: bool = False) -> dict:
    """What every bulb and every generic device is doing, coalesced and rate
    limited. One map, because the page draws them side by side."""
    try:
        devices = load_devices()
    except (OSError, ValueError):
        return {}

    watched = ([("bulb", b) for b in devices.get("bulbs", [])]
               + [("device", d) for d in devices.get("devices", [])])

    now = time.time()
    with _bulb_lock:
        for kind, dev in watched:
            st = state_of(dev["node"])
            age = now - float(st.get("readAt") or 0)
            if not force:
                if age < BULB_TTL_SEC:
                    continue
                # Cold: it did not answer last time. Do not spend a full timeout
                # on it every time somebody opens the page.
                if st.get("ok") is False and age < BULB_COLD_SEC:
                    continue
            try:
                (refresh_bulb if kind == "bulb" else refresh_device)(dev)
            except Exception as exc:  # noqa: BLE001 - one device does not stop the rest
                log(f"node {dev['node']}: read failed: {exc}", "warn")

    out = {}
    for kind, dev in watched:
        st = state_of(dev["node"])
        row = {"ok": st.get("ok"), "readAt": st.get("readAt")}
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
# level 1..254 is proportional to emitted light and the eye is not, so
# interpolating raw levels would follow a different curve from the drawn one.
# Colour temperature is interpolated in mireds, which is what its axis already
# is; a spline is affine-invariant, so that matches the drawing exactly.


def perceived(level: int) -> float:
    """L* from CIE Lab: how bright a Matter level looks, 0..1."""
    y = max(0.0, min(1.0, level / 254))
    return (116 * (y ** (1 / 3)) - 16 if y > 0.008856 else 903.3 * y) / 100


def level_from_perceived(p: float) -> int:
    lstar = max(0.0, min(1.0, p)) * 100
    y = ((lstar + 16) / 116) ** 3 if lstar > 8 else lstar / 903.3
    return max(1, min(254, round(y * 254)))


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
    """
    release = False
    with _state_lock:
        entry = _state.get("bulbs", {}).get(str(node))
        if not entry or "override" not in entry:
            return
        if on:
            if entry.get("heldLit"):
                return
            entry["heldLit"] = True      # now it can be ended by an off
        else:
            if not entry.get("heldLit"):
                return                   # set while dark; waiting for its turn
            release = True
    if release:
        release_hold(node, "switched off")
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
    r = chip(f"levelcontrol write on-level {level} {node} {ep}", timeout=20.0)
    if chip_error(r):
        log(f"bulb {node}: re-arming OnLevel failed: {chip_error(r)}", "warn")
    else:
        sent["onlevel"] = level
    if mireds:
        # ExecuteIfOff, so the colour lands on a dark bulb too.
        r = chip(f"colorcontrol move-to-color-temperature {mireds} 4 1 1 "
                 f"{node} {ep}", timeout=20.0)
        if not chip_error(r):
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
            r = chip(f"levelcontrol write on-level {level} {b['node']} {ep}",
                     timeout=45.0)
            e = chip_error(r)
            if e:
                log(f"bulb {b['node']}: OnLevel failed: {e}", "err")
            else:
                sent["onlevel"] = level
                did = True

        # What it should be doing right now, if it is lit at all.
        if force or sent.get("level") != level:
            r = chip(f"levelcontrol move-to-level {level} {tenths} 0 0 "
                     f"{b['node']} {ep}", timeout=45.0)
            e = chip_error(r)
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
                r = chip(f"colorcontrol move-to-color-temperature {mireds} "
                         f"{tenths} 1 1 {b['node']} {ep}", timeout=45.0)
                e = chip_error(r)
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
            d["measures"] = [{"key": k, "label": lab, "unit": u}
                             for _, _, k, lab, u, _ in MEASURED]
            d["airQualityWords"] = AIR_QUALITY_WORDS
            self._send(d)

        elif self.path == "/api/bindings":
            # From the state held on the Pi, so it is instant. Reading from the
            # device happens in the background - see the "state kept on the Pi"
            # section.
            sw_list = switches(load_devices())
            out = {}
            # "reachable" stays honest: it counts the switches whose LAST read
            # succeeded, not the switches we happen to hold an old opinion
            # about. A switch unplugged an hour ago is "not answering", even
            # though we still know its table.
            health = {"total": len(sw_list), "reachable": 0,
                      "chipTool": _chip_ok}
            for sw in sw_list:
                st = state_of(sw["node"])
                out[str(sw["node"])] = st.get("binding") or []
                if st.get("ok"):
                    health["reachable"] += 1
            self._send({"bySwitch": out, "raw": None, "health": health,
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
                    rooms.sort(key=str.casefold)

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
                rooms.sort(key=str.casefold)
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
                rooms.sort(key=str.casefold)
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
                    rooms.sort(key=str.casefold)
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
            # A lock does not control bulbs, it controls switches. Different
            # cluster, different endpoint, and the targets need an ACL -
            # switches had none until now, because nobody wrote anything to
            # them.
            if sw.get("role") == "lock":
                targets = [x for x in switches(devices) if x["node"] in want]
                log(f"lock: binding switch {sw_node} to {len(targets)} switches", "step")
                errors = []
                for t in targets:
                    r = chip(acl_for_switch(t["node"], [sw_node]), timeout=60.0)
                    e = chip_error(r)
                    if e:
                        errors.append(f"ACL {t['node']}: {e}")
                r = chip(lock_binding_for(sw, targets), timeout=60.0)
                e = chip_error(r)
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
                    r = chip(f"binding read binding {s2['node']} "
                             f"{s2.get('entry_endpoint', s2.get('endpoint', 1))}")
                    desired[s2["node"]] = {e["node"]
                                           for e in extract_bindings(r)}

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
                r = chip(acl_for(b["node"], allowed), timeout=60.0)
                e = chip_error(r)
                if e:
                    errors.append(f"ACL {b['node']}: {e}")

            step_no += 1
            log(f"{step_no}/{total} writing the binding table on switch "
                f"{sw_node}. The write REPLACES the whole table, so the "
                f"complete list is sent, not just the difference.", "step")
            log(f"the switch is sleepy with a 15 s poll - it may take a few "
                f"seconds to answer", "step")
            r = chip(binding_for(sw, bulbs), timeout=60.0)
            e = chip_error(r)
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
                          meta={"readAt": time.time(), "ok": True, "err": None})

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
            if kind == "switch":
                cmd = (f"pairing ble-thread {node} hex:{dataset} "
                       f"{SWITCH_PASSCODE} {SWITCH_DISCRIMINATOR}")
            else:
                cmd = f"pairing code-thread {node} hex:{dataset} {code}"
            if BYPASS_ATTESTATION:
                # Logged on EVERY commissioning, on purpose: a security
                # weakening that is visible nowhere gets forgotten, and six
                # months later nobody knows why any device gets through.
                log("attestation certificate verification is DISABLED "
                    "(PANEL_BYPASS_ATTESTATION). Anything that answers gets "
                    "commissioned.", "warn")
                cmd += " --bypass-attestation-verifier true"
            # The log position BEFORE the command: the real reason for a failure
            # is written to chip-tool's stdout, not into the WebSocket response,
            # which only says "FAILURE".
            try:
                log_pos = CHIP_TOOL_LOG.stat().st_size
            except OSError:
                log_pos = 0

            resp = chip(cmd, timeout=180.0)
            err = chip_error(resp)
            if err:
                reason = commissioning_reason(log_pos)
                message = f"{err}: {reason}" if reason else str(err)
                log(f"commissioning failed - {message}", "err")
                return self._send({"error": message, "step": reason or None,
                                   "raw": resp}, status=502)

            # If the room is new we remember it - otherwise it disappears when
            # you delete the last bulb in it, and comes back spelled differently
            # next time.
            rooms = devices.setdefault("rooms", [])
            if where and where.casefold() not in {r.casefold() for r in rooms}:
                rooms.append(where)
                rooms.sort(key=str.casefold)

            entry = {"node": node, "endpoint": 1, "name": name, "where": where}

            if kind == "switch":
                sws.append(entry)
            else:
                # Ask the device what it is rather than being told. This is the
                # only description that cannot go stale, because it comes from
                # the device and not from whoever filled the form.
                try:
                    entry["desc"] = describe_device(node)
                except Exception as exc:  # noqa: BLE001 - a mute device is still added
                    log(f"node {node}: could not read its descriptor: {exc}", "warn")
                    entry["desc"] = {"types": [], "endpoints": {}}
                entry["kind"] = device_type_name(entry["desc"])
                is_light = bool(set(entry["desc"].get("types") or []) & LIGHT_TYPES)
                log(f"node {node} says it is a {entry['kind']}", "ok")

                if is_light:
                    entry["caps"] = detect_caps(node)
                    bulbs.append(entry)
                    kind = "bulb"     # from here on it is wired like one
                else:
                    others.append(entry)

            save_devices(devices)

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
                have = {e["node"] for e in
                        extract_bindings(chip(f"binding read binding {sw['node']} {ep}"))}
                if node not in have:
                    continue
                keep = [b for b in devices.get("bulbs", [])
                        if b["node"] in have and b["node"] != node]
                log(f"unbinding it from {sw.get('name', sw['node'])}", "step")
                err = chip_error(chip(binding_for(sw, keep), timeout=60.0))
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
                        if b["node"] in {e["node"] for e in extract_bindings(
                                chip(f"binding read binding {s2} {ep2}"))}:
                            allowed.append(s2)
                    err = chip_error(chip(acl_for(b["node"], sorted(allowed)), timeout=60.0))
                    if err:
                        warnings.append(f"ACL {b['node']}: {err}")

            # 3. off the fabric
            err = chip_error(chip(f"pairing unpair {node}", timeout=90.0))
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

            # The spec caps the window at 900 s.
            seconds = max(60, min(900, minutes * 60))
            # Option 1 = Enhanced Commissioning Method: a NEW, temporary code is
            # generated. Option 0 would reuse the factory code, which is no
            # longer valid on an already commissioned device.
            discriminator = random.randint(0, 4095)
            iteration = 1000  # the spec's minimum for PBKDF2

            try:
                pos = CHIP_TOOL_LOG.stat().st_size
            except OSError:
                pos = 0

            open_window = (f"pairing open-commissioning-window {node} 1 "
                           f"{seconds} {iteration} {discriminator}")
            resp = chip(open_window)
            err = chip_error(resp)

            if err:
                # "Busy" means the device ALREADY has a window open - typically
                # a freshly factory-reset bulb, which is waiting to be added
                # anyway. The cluster-specific status (0x02 = kBusy) does not
                # make it into the JSON, only a bare "FAILURE", so we infer it
                # by reading the window status.
                st = _first_attr(chip(f"administratorcommissioning read "
                                      f"window-status {node} 0", timeout=45.0))
                if isinstance(st, int) and st != 0:
                    log(f"node {node}: already has a window open "
                        f"(status {st}) - revoking it and reopening with a new "
                        f"code", "warn")
                    chip(f"administratorcommissioning revoke-commissioning {node} 0 "
                         f"--timedInteractionTimeoutMs 3000", timeout=45.0)
                    try:
                        pos = CHIP_TOOL_LOG.stat().st_size
                    except OSError:
                        pos = 0
                    resp = chip(open_window)
                    err = chip_error(resp)

            if err:
                return self._send({"error": err, "raw": resp}, status=502)

            codes = read_pairing_codes(pos)
            if not codes.get("qr"):
                return self._send({
                    "error": "the window opened, but the codes were not in "
                             "chip-tool's log. Check CHIP_TOOL_LOG.",
                    "raw": resp,
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
                r = chip(f"onoff {action} {node} {endpoint}", timeout=60.0)
                err = chip_error(r)
                if err:
                    log(f"bulb {node}: {action} failed: {err}", "err")
                    return self._send({"error": err}, status=502)

            # Brightness on its own, with-on-off so asking for light gives light
            # rather than silently arming a bulb that is switched off.
            if level is not None:
                lvl = max(1, min(254, int(level)))
                r = chip(f"levelcontrol move-to-level-with-on-off {lvl} 0 0 0 "
                         f"{node} {endpoint}", timeout=60.0)
                err = chip_error(r)
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
                r = chip(f"levelcontrol write on-level {lvl} {node} {endpoint}",
                         timeout=45.0)
                e = chip_error(r)
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
                r = chip(f"colorcontrol move-to-color-temperature {mir} 0 1 1 "
                         f"{node} {endpoint}", timeout=60.0)
                err = chip_error(r)
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

            # Let it settle first. On and Off do not land the instant the
            # command is acknowledged: the bulb has to apply OnLevel to
            # CurrentLevel, and reading in that gap returns the level it is
            # LEAVING. For an On that is the off value - 1 - so the panel put
            # "on" and "4%" on screen together and stood by it, because a
            # read-back is the one number here that is supposed to be beyond
            # doubt.
            if action:
                time.sleep(0.35)
            try:
                refresh_bulb(bulb)
            except Exception as exc:  # noqa: BLE001
                log(f"bulb {node}: read-back failed: {exc}", "warn")

            # And if it still looks like the gap, read once more rather than
            # publish it. A bulb that is ON at its floor while OnLevel says
            # otherwise has not finished; a bulb genuinely dimmed to 1 by hand
            # has an OnLevel of 1 to match, so this cannot fire on a real value.
            st = state_of(node)
            if (action in ("on", "toggle") and st.get("on") is True
                    and (st.get("level") or 0) <= 1
                    and (st.get("onlevel") or 0) > 1):
                log(f"bulb {node}: read back mid-transition, looking again", "warn")
                time.sleep(0.6)
                try:
                    refresh_bulb(bulb)
                except Exception as exc:  # noqa: BLE001
                    log(f"bulb {node}: second read failed: {exc}", "warn")
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

        if self.path == "/api/identify":
            node = body.get("node")
            endpoint = body.get("endpoint", 1)
            seconds = int(body.get("seconds", 15))
            log(f"--- identify on node {node}, {seconds} s ---", "step")
            if node is None:
                return self._send({"error": "'node' is missing"}, status=400)
            # identify identify <IdentifyTime> <destination-node> <endpoint>
            return self._send(chip(f"identify identify {seconds} {node} {endpoint}"))

        self._send({"error": "not found"}, status=404)


if __name__ == "__main__":
    print(f"Panel:     http://0.0.0.0:{PANEL_PORT}")
    print(f"chip-tool: {CHIP_TOOL_WS}")
    print(f"state:     {STATE_FILE} (refresh every {REFRESH_SEC} s)")
    state_load()

    # A frozen chip-tool left over from a previous run would look dead, and our
    # _frozen flag knows nothing about it - hence force.
    chip_thaw(force=True)
    atexit.register(chip_thaw, True)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: (chip_thaw(True), os._exit(0)))

    threading.Thread(target=refresher, name="refresher", daemon=True).start()
    threading.Thread(target=freezer, name="freezer", daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PANEL_PORT), Handler).serve_forever()
