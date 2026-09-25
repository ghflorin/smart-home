"""Light shows: an animation played across a group of lamps.

A lamp here is one pixel that can change a few times a second, not a strip of
LEDs: every change is a Matter command over Thread, about 35 ms each, and the
lamp does the fade itself when a command carries a transition time. So a show
is a sequence of STEPS - where each lamp is going and how long it takes to get
there - and the lamps draw the movement between them. Measured on a colour
bulb: a 3 s fade from 40% to 100% passes through 32 levels on its own.

Pure. A preset is a function of the step number, the lamps in order, the
group's brightness and speed, and a random source; it answers what each lamp
should do next and how long until the step after. The engine in server.py
sends it, and nothing in this file talks to a device.

Every preset works with whatever the group has. A colour lamp gets the
colours; a white-only lamp gets the same rhythm in warm and cool white; and
the effects that travel - the wave - fold down to a pulse on a single lamp.
"""

PRESETS = [
    {"key": "candycane", "label": "candy cane"},
    {"key": "twinkle", "label": "twinkle"},
    {"key": "candle", "label": "candle"},
    {"key": "wave", "label": "wave"},
    {"key": "breathe", "label": "breathe"},
    {"key": "rainbow", "label": "rainbow"},
]
KEYS = [p["key"] for p in PRESETS]
LABELS = {p["key"]: p["label"] for p in PRESETS}

RED = (0, 254)
GREEN = (85, 254)
GOLD = (30, 190)
EMBER = (20, 235)

# Speeds, as a multiplier on every step's length.
SPEEDS = {"slow": 1.6, "normal": 1.0, "fast": 0.6}


def _tenths(secs: float) -> int:
    """A transition time as Matter counts it, in tenths of a second."""
    return max(0, min(600, int(round(secs * 10))))


def _white(lamp: dict, warmth: float) -> int:
    """A white for this lamp: 1 is the warmest it can go, 0 the coolest."""
    lo, hi = lamp["ctLo"], lamp["ctHi"]
    return int(round(lo + (hi - lo) * max(0.0, min(1.0, warmth))))


def _tint(lamp: dict, hs, warmth: float, tt: int) -> dict:
    """The colour part of a step: hue and saturation on a colour lamp, the
    matching white on one that has none."""
    if lamp["colour"]:
        return {"hue": hs[0], "sat": hs[1], "tt": tt}
    return {"mireds": _white(lamp, warmth), "tt": tt}


def frame(key: str, step: int, lamps: list, level: int, speed: str, rng, memo: dict):
    """What to send this step, and how long until the next.

    Returns ({node: change}, seconds). A change holds any of level, hue+sat or
    mireds, and `tt` - the transition, in tenths. Anything not named is left as
    it is, which is what keeps a step down to the commands it actually needs.
    """
    k = SPEEDS.get(speed, 1.0)
    n = len(lamps)
    lo = max(3, int(level * 0.15))
    out = {}

    if key == "candycane":
        # Neighbours out of step, so the colours trade places down the line.
        period = 2.0 * k
        tt = _tenths(min(period * 0.5, 1.5))
        for j, lamp in enumerate(lamps):
            red = (step + j) % 2 == 0
            ch = _tint(lamp, RED if red else GREEN, 1.0 if red else 0.0, tt)
            if step == 0:
                ch["level"] = level
            out[lamp["node"]] = ch
        return out, period

    if key == "twinkle":
        # A warm field, and a few lamps at a time dipping out and back like
        # stars. The dip is quick and the return slower, which is what makes it
        # read as a twinkle rather than as a fault.
        period = 0.6 * k
        if step == 0:
            for lamp in lamps:
                out[lamp["node"]] = {"level": level, "tt": 4, **_tint(lamp, GOLD, 0.85, 4)}
            memo["dipped"] = []
            return out, period
        for node in memo.get("dipped", []):
            out[node] = {"level": level, "tt": _tenths(0.5 * k)}
        pool = [l["node"] for l in lamps if l["node"] not in memo.get("dipped", [])] \
            or [l["node"] for l in lamps]
        pick = rng.sample(pool, min(len(pool), max(1, n // 4)))
        for node in pick:
            out[node] = {"level": lo, "tt": _tenths(0.2 * k)}
        memo["dipped"] = pick
        return out, period

    if key == "candle":
        # Warm, and never still: a random level between just over half and
        # full, on half the lamps each step, with short uneven fades.
        period = 0.4 * k
        if step == 0:
            for lamp in lamps:
                out[lamp["node"]] = {"level": level, "tt": 3, **_tint(lamp, EMBER, 1.0, 3)}
            return out, period
        for lamp in rng.sample(lamps, max(1, (n + 1) // 2)):
            out[lamp["node"]] = {"level": max(lo, int(level * rng.uniform(0.55, 1.0))),
                                 "tt": rng.choice((2, 3, 4))}
        return out, period

    if key == "wave":
        # A crest of light moving down the line, in the order the lamps were
        # listed. Two commands a step: the crest arrives at one lamp and leaves
        # the one before. On a single lamp it is a pulse.
        period = 0.7 * k
        tt = _tenths(period * 0.8)
        if step == 0:
            for lamp in lamps:
                out[lamp["node"]] = {"level": lo, "tt": 4, **_tint(lamp, GOLD, 0.8, 4)}
            return out, period
        if n == 1:
            out[lamps[0]["node"]] = {"level": level if step % 2 else lo, "tt": tt}
            return out, period
        here = lamps[(step - 1) % n]["node"]
        before = lamps[(step - 2) % n]["node"]
        out[before] = {"level": lo, "tt": tt}
        out[here] = {"level": level, "tt": tt}
        return out, period

    if key == "breathe":
        # Slow in and out together. The colour lamps change between red and
        # green while they are dim, so each breath comes up in the other one.
        period = 2.6 * k
        tt = _tenths(period * 0.95)
        up = step % 2 == 0
        for j, lamp in enumerate(lamps):
            ch = {"level": level if up else lo, "tt": tt}
            if not up or step == 0:
                red = (step // 2 + j) % 2 == (0 if step == 0 else 1)
                ch = {**ch, **_tint(lamp, RED if red else GREEN, 0.9, tt)}
            out[lamp["node"]] = ch
        return out, period

    if key == "rainbow":
        # The colour lamps walk round the wheel, spread evenly around it; the
        # white ones drift between warm and cool at half the pace.
        period = 1.5 * k
        tt = _tenths(period)
        for j, lamp in enumerate(lamps):
            if lamp["colour"]:
                ch = {"hue": (step * 20 + j * 254 // max(1, n)) % 254, "sat": 254, "tt": tt}
            elif step % 2 == 0:
                ch = {"mireds": _white(lamp, 1.0 if (step // 2 + j) % 2 == 0 else 0.0),
                      "tt": _tenths(period * 2)}
            else:
                ch = {}
            if step == 0:
                ch["level"] = level
            if ch:
                out[lamp["node"]] = ch
        return out, period

    return out, 1.0
