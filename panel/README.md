# panel/ — the local admin panel

Runs on the Raspberry Pi, next to the OTA provider. It exists for a few
practical jobs:

- **Which bulb is which.** You press Identify and the bulb blinks for a few
  seconds. No more walking up to a lamp to find out its node ID.
- **What each switch drives.** It reads the switch's binding table and shows it
  as switch → bulbs links. That is exactly what the switch commands directly,
  with no hub — not a separate configuration that could disagree with reality.
- **Turning the lights on and off**, and seeing which are on, from a grid of
  tiles grouped by room.
- **The light schedule** — what the bulbs are set to, hour by hour.
- **Sharing to Apple Home.** It generates a QR code for any device already
  commissioned here, so you can add it to another ecosystem without removing it
  from ours.
- **Commissioning.** Adding a new bulb or switch to the fabric, with the real
  reason printed when it fails.

```bash
./panel/run.sh          # http://<pi>.local:8080
```

## How it is wired

```
browser <──long poll──> panel/server.py <──WebSocket──> matter-server <──Thread──> devices
                                          (port 5580)
```

Every arrow is a **push**, not a poll, and that is the point of the shape:

- a device reports an attribute the moment it changes; matter-server holds a
  subscription to all of them and keeps a live cache
- the panel folds each report into its state and bumps a revision
- the browser is parked on a long poll and is answered the instant that happens

So a wall-switch press — which never touches the Pi, the switch commands the
bulb directly — reaches the screen in about 0.2 s. Nothing anywhere chose an
interval and hoped.

Reads run the other way for free: they come out of matter-server's cache rather
than waking a sleeping device, which for a battery sensor is the difference
between an answer and a coin flip.

Commands, writes, bindings, ACLs and commissioning go through the same socket.
See `panel/matter_link.py` — the listener and the command socket are separate
connections on purpose.

**This used to be `chip-tool interactive server`.** Two sockets, a SIGSTOP
freezer to stop it burning a core while idle, pairing codes scraped out of its
stdout log, and no subscriptions at all. The migration took no factory resets —
Matter allows several fabrics — but binding and ACL are fabric-scoped and had to
be rewritten. See [`docs/devices.md`](../docs/devices.md).

## Files

| | |
|---|---|
| `run.sh` | starts the panel |
| `server.py` | HTTP, state, the schedule |
| `matter_link.py` | the two sockets to matter-server: reports in, commands out |
| `index.html` | the interface |
| `devices.json` | node IDs, names and rooms |

`devices.json` is for you alone — Matter does not know the names you give. The
panel appends to it when you commission a device from the interface; edit it by
hand to rename things or to describe devices that were commissioned with
`scripts/commission.sh`. The node IDs have to match the ones actually used at
commissioning.

## Theme

A single button in the top right, cycling **auto → light → dark**. Auto follows
the system setting and changes with it on the fly; the choice is kept in
`localStorage`, so it is per browser, not per installation.

The icon shows the preference, not the effective theme — otherwise on auto you
would have no way to see that you are on auto. The three shapes: a full sun
(light), a crescent (dark), a half disc with rays (auto — the same symbol as
automatic brightness on a phone). They are not three icons swapped in and out but
one SVG whose pieces translate: the rays pull in and rotate, and a circle in the
mask slides over the disc and carves the crescent. The disc's radius never
changes, so the sun really does become the moon. There is no visible text, so the
state and the next step live in `title` and `aria-label`, and
`prefers-reduced-motion` stops the animation.

The theme is resolved in an inline script in `<head>`, before first paint —
otherwise you would see a flash of the wrong theme on every load. `localStorage`
holds the preference (`auto|light|dark`), `<html>` carries the **effective**
theme (`light|dark`), so the CSS has exactly two palettes and no media query.

Every color goes through the variables in `:root[data-theme=...]`. Add a
hardcoded color and the light theme breaks silently; for accent shades use
`color-mix` over `--amber` / `--link` so they adjust themselves. Two deliberate
exceptions: the grid mask (`#000`, where only the alpha channel matters) and the
QR code's background, which stays white so it scans reliably.

## The layout: rooms, tiles, drag and drop

**Adding is one question, not three buttons.** `+ device`, `+ switch` and
`+ room` used to sit permanently across the top of a page whose subject is the
rooms underneath them — and the difference between the first two is invisible to
anybody who has not read this file. The header carries a single `+` now, beside
`schedule` and the theme control, and asks once: a Matter device from anyone, one
of our own switches, or just a name to group things under. Each choice says what
it is *and what it costs*, which is the part that was never on screen.

**Removing a device is two presses and one sentence.** Not a `confirm()` and not
a second dialog: the button says what it is about to do, and the line underneath
says what that costs — because unpairing is not "take it off the list", it hands
the device back, and getting it into any other system means a factory reset
first. It disarms itself after a few seconds so an armed button is never left
lying around.

The order on the server matters. It comes out of every binding table that names
it *while the switches can still be reached* — after the unpair the node is gone,
and a switch left pointing at an address nothing answers is a stale entry in a
table you cannot see. Then the ACLs, if what is leaving is a switch. Then the
unpair. Then `devices.json`, the schedule and the state. **A device that does not
answer is still removable**: unpairing is the only step that needs it alive, so a
failure there is reported and the rest goes ahead — refusing to forget a lamp you
have already thrown away would leave the house permanently wrong. The panel says
which happened, because the difference decides whether you can use the thing
again without a factory reset.

The page is a grid of tiles grouped **by room**, not by kind of device. Grouping
by kind — every switch, then every bulb — answers a question nobody has; you know
whether you are looking for a lamp. What is genuinely easy to get wrong is which
room a device ended up in, and sections make that impossible to miss.

Each tile does two jobs, split the way a phone splits them:

- the **round icon** is the quick action — on/off for a bulb, lock/unlock for a
  switch;
- **anywhere else** on the tile opens its settings.

Three tile sizes, from the picker on the right of the toolbar. `s` is a row
(icon beside the text), `m` and `l` are squares. They are not one shape scaled:
below about 150px a square has no room for a name. The choice is kept in
`localStorage`, so the wall tablet and the laptop can differ while looking at the
same server.

### Brightness is the fill

A lit bulb's tile fills from the bottom to its brightness, and the top edge of
the fill is a hairline. So a row of tiles reads as a row of levels with no
numbers being compared, and it reads from across the room.

The height and the number are **perceptual**, like every other percentage in the
panel. This was the one place they were the raw Matter level, so the same bulb
read 40% in its sheet and 11% on its tile — the same light, two scales, and
nothing on screen to say which was lying.

The fill is flat on purpose. Height is the only thing that varies, which is what
makes it a measurement rather than a texture — shading it would add a second
gradient that means nothing and blur the very edge that carries the value.

**Unknown is not off.** A bulb that did not answer gets a dashed border and
"no answer", never an empty tile. Showing it as off is a lie you would act on:
you would walk over and press a switch that is already in the right position.

### Moving and arranging devices

**Press and hold** a tile for a third of a second, then drag it — into another
room, or to another place in the same one. It works the same with a mouse and
with a finger, being written on pointer events rather than the HTML
drag-and-drop API, which does not exist on touch at all, and a wall tablet is
exactly the case where this matters.

It starts on the hold, not on movement: starting on movement means every attempt
to scroll the page with a finger picks up whatever tile it began on. Move more
than 10px before the hold completes and it is treated as a scroll.

The tile itself moves — there is no clone to keep in sync with the fill, the
badge and the icon state. It is taken out of the grid's flow (`position: fixed`,
pinned where it already was) and a **gap** is left in its place, which then
follows the pointer to wherever the drop would land. The gap is a real grid
item, so the preview is the layout itself rather than a line drawn over a layout
that might disagree with it. `fixed` also makes the auto-scroll simple: the tile
is positioned in viewport coordinates and so is the pointer, so it tracks the
finger whatever the page does underneath. Hold near the top or bottom of the
screen and the page scrolls — otherwise you could only ever drop into a room
that happened to be visible already.

Finding the slot in a grid is two questions, not one: is the pointer above this
row, or inside it and left of this tile's middle. Comparing distance to tile
centres, the usual shortcut, puts the gap in the wrong row whenever rows are
taller than they are wide.

The order is then read back **off the screen** rather than computed from where
we think the tile went — the DOM is what the user just watched settle, and
anything else would be a second opinion about the same event. Dropping a tile
back where it started writes nothing.

### Where a tile sits in its room

Order comes from a `pos` on each device, written by a drop, and it is the only
thing that decides order once it exists.

Devices without one — an install that has never been rearranged, or a device
commissioned into a room that has — fall back to the original rule: switches
before bulbs in `devices.json` order, sorted after everything placed by hand. So
a house nobody has touched looks exactly as it did, and a newly added device
turns up at the end rather than in the middle of an arrangement somebody made.

A drop sends `op: "order"` with the room and everything in it, in order. One
operation covers both moving a device into a room and rearranging the room,
because a drop does both at once and splitting them would mean two writes with a
wrong state in between. Only the listed devices are touched; the room a device
came **from** keeps its own positions, gaps and all — nothing reads the numbers,
only their order.

### Rooms

Rooms exist only in `devices.json`. Matter has no concept of one — it is our
grouping, and it is most of the reason that file exists next to the fabric.

`+ room` creates one; the wrench on a room's heading renames or deletes it. That
is all the sheet does: which devices are in a room, and in what order, is
answered by dragging them there, where the answer is also the question. Deleting
a room does not delete
what is in it — the devices end up with no room, and land in the `no room`
section — but you cannot know that before you press, so it asks twice.

Every room operation rewrites `devices.json` whole and atomically. There is no
partial state worth leaving behind: renaming a room means touching the room list
**and** every device in it, and half of that is worse than none of it.

## The light schedule

The `schedule` button in the toolbar opens it. It lives on the Pi in
`ota/state/schedule.json`.

### One house schedule, and any bulb may have its own

That is the whole model, and it is deliberately the simplest one that answers the
question people actually have: *all my lights do this, except the bedside lamp*.
A bulb either follows the house or it does not.

Named schedules that several bulbs subscribe to would be more powerful and would
need a screen of their own to manage. This needs one row on a bulb's sheet and
one column in a list.

The file keeps its old shape, so nothing had to be migrated — a bare
`{"points": [...]}` is a house schedule with no overrides:

```json
{ "points": [ … ], "bulbs": { "1001": { "points": [ … ] } } }
```

**Nothing changes without saying what.** The sentence above the chart spells the
scope out — `editing the house schedule — 3 bulbs follow it`, or
`editing Bedside left on its own` — and the save button repeats it:
`save for the house`, `save for 2 bulbs`.

**Two save buttons, and the second one carries its own target.** `save globally`
writes the house schedule. Beside it is a split button: press the label to save
for the bulbs it names, press the caret to change which bulbs those are. Arriving
from a bulb's sheet pre-fills it, so the common case — open a lamp's schedule,
change it, save it — is one press with the target already written on the button.

The picker behind the caret groups by **room**, because that is how anybody holds
a house in their head: *the bedside lamps*, not *nodes 1004 and 1005*. A bulb
that already has a schedule of its own is marked, so you can see what you are
about to overwrite before you tick it.

This replaced a list of every bulb sitting permanently under the chart — a table
of the whole house taking room on a page whose job is a curve. The button now
carries the answer, so a save's reach is visible without opening anything.

**Changing the target is not the same as changing the curve**, and conflating the
two cost an edit. `save globally` has to retarget the save at the house before
sending it, and it did that by calling the same routine the picker uses — which
retargets *and* loads that target's saved curve over whatever is on screen. So it
reloaded the stored house curve one frame before posting it: the save worked
perfectly and stored what was already there, the edit vanished, and it looked for
all the world like a save that silently refused. Retargeting is its own operation
now, and it touches nothing but the label.

**A switch's sheet has no schedule button.** It used to, scoped to the bulbs
that switch drove — the only reading of "schedule" on a switch that meant
anything while the schedule was reached *through* the bindings. Once it stopped
being reached that way, the button became an arbitrary subset of the house, and
it quietly fell back to the whole house whenever the switch drove nothing. A
switch has bindings; bulbs have a schedule. The editor lives in the header and on
the sheet of each bulb it actually affects.

**The schedule reaches every bulb, bound or not.** It used to walk the switches
and then their binding tables, which quietly made *is on the schedule* mean *is
wired to a wall switch*. Those are two different relationships and only one of
them involves a switch: a binding is how the **wall switch** commands the bulb
with the Pi asleep, and the schedule is the **Pi** commanding the bulb directly
over Thread. The Pi never needed a switch in order to do that.

It went unnoticed while every new bulb bound itself to whatever switch happened
to be listed first. The moment that stopped, a newly added lamp sat outside the
schedule entirely — you pressed *save globally*, it said it had saved, and that
one bulb never moved.

**Idle polls slowly; it does not stop.** Three minutes without touching the page
used to mean the panel stopped asking for anything at all. That is right for a
lamp you are not using and wrong for a sensor, whose whole job is to change while
nobody is touching anything — you stand in front of a door sensor and the panel
has quietly stopped listening. Idle is 60 s now instead of 15. Hidden still stops
entirely: nobody can see it, and coming back refreshes at once.

**One missed read is not a missing device.** A battery sensor is asleep most of
the time, so a read landing in the wrong moment simply fails — routinely, with
nothing wrong. That used to flip the tile to "not answering" seconds after a
perfectly good reading. A device has to have been quiet for `PANEL_SLEEPY_GRACE`
before the panel says so.

**Adding a kind of device is a line, not a branch.** Nothing is hardcoded per
product: the panel reads a device's Descriptor on commissioning and decides from
the device type. A new measurement is one row in `MEASURED`; a reading that is a
state rather than a quantity — a contact, an occupancy — adds a row in
`MEASURE_WORDS` so it renders as a word instead of `true`. What has actually
been driven from here, and what each one got wrong, is in
[`../docs/devices.md`](../docs/devices.md).

**A save touches only the bulbs the saved schedule governs.** Writing the house
curve into a bulb that has its own would undo the override for a minute, which is
the sort of thing you see once and never trust again.

A bulb's own sheet carries an **affected by schedule** row saying which one
governs it, and opening it goes straight to that scope. Until it existed you
could watch a lamp change by itself with nothing on its own sheet to say what was
doing it. A bulb bound to no switch says so instead: nothing writes to it on a
timer.

The button on a switch's sheet scopes to the bulbs that switch drives. A switch
has no schedule of its own — what it has is bulbs, and that is the only reading
of the word there that means anything.

What reaches the hardware is not the schedule but its result: at every slot
change the Pi writes `OnLevel` and the colour temperature into each bound bulb,
and the switch only ever sends `Toggle`.

Three writes, because they answer three different questions:

| | |
|---|---|
| `OnLevel` | what the bulb comes up at when the switch is pressed |
| `MoveToLevel` | what a bulb that is **already lit** should be doing |
| colour | both at once — it carries `ExecuteIfOff` |

`OnLevel` alone is not enough, and that was a bug for a while: it is only
consulted when a bulb *turns on*, so writing it does nothing to a lamp you are
standing under while you edit the schedule — which is exactly when you are
watching.

`MoveToLevel` deliberately, and not `MoveToLevelWithOnOff`: with `optionsMask`
and `optionsOverride` at 0 the bulb falls back to its own `Options` attribute,
where `ExecuteIfOff` is clear — so a bulb that is off ignores it and a bulb that
is on follows it. No state to consult, and no way to light a dark room by
accident at four in the morning.

They are written at different rates, and the reason is flash. `OnLevel` is a
**persistent** attribute — that is the whole reason a bulb comes up correctly
with the Pi switched off — so it moves in steps of `PANEL_ONLEVEL_STEP` (8)
rather than every minute, which would be over half a million writes a year into a
lamp. The other two are commands: they cost a packet and nothing else, so they
follow the curve as closely as the tick allows, with a transition time of one
tick so the bulb glides between samples instead of stepping.

**A save says what it did, not what it wrote.** A bulb that is off takes every
one of these writes and shows nothing — `MoveToLevel` is ignored while off, on
purpose — so "written into 1 bulb" is a true sentence that reads as a lie to
somebody standing in front of a dark lamp. The reply carries how many bulbs were
lit, how many were off and how many did not answer, and the editor says
`1 bulb now at 43%` or `1 bulb off — it will come up at 43%`.

**A manual save lands fast; a tick glides.** The transition time is one tick
(60 s) for the background sampling, so the light moves with the day rather than
stepping. On a save it is 0.4 s: somebody has just pressed a button and is
looking at the lamp, and a minute-long fade to a value a few percent away is
indistinguishable from nothing happening — which is exactly the conclusion they
will draw.

Nothing is sent unless the target has actually moved — but on its own that was
not enough. It stops the schedule stamping on a manual change *while the curve is
flat*, and does nothing at a slot boundary: set a lamp to full at 14:29 and at
14:30 the curve moves, so the schedule ramps it back down over the next minute.
From under the lamp that is the light changing by itself a minute after you set
it, with the panel still showing what you asked for.

So **a hand on the light wins outright, and keeps winning until the light is
switched off.** Setting brightness or colour from a bulb's sheet marks it held;
a tick skips a held bulb entirely; switching it off releases it, which is the
natural end of "I am using this lamp now" and also the moment `OnLevel` starts
mattering again. Pressing save in the schedule editor releases it too — that is
asking for the schedule out loud. Its sheet says `on · held` while this is in
force, because otherwise the next question is why the schedule has stopped
touching that lamp.

**A hold has a ceiling as well.** Switching off is the right end for a lamp
somebody is using, and it has one blind spot: a lamp that is never switched off.
Leave the kitchen light on all day, nudge it once at nine in the morning, and it
sits out the whole day, because the event that ends a hold never comes — the
schedule is following the curve minute by minute for every other bulb and
skipping that one. Whichever arrives first now ends it: the light goes off, or
`PANEL_HOLD_MAX_SEC` runs out (3 h by default; 0 makes switching off the only
way out).

**The release lives where an off is *observed*, not where one is sent.** This is
the part that was wrong for a while and is worth spelling out, because the
obvious place to put it is the wrong place. The panel used to release the hold
inside the request handler that turned the light off — which works perfectly for
the one case that barely matters. In this house a light is switched off at the
**wall**, and the wall switch talks to the bulb directly over Thread with the Pi
nowhere in the command path. No request, no release. A lamp set by hand and then
switched off at the wall stayed held for good: skipped by every tick, coming up
at the manual value for ever, and nothing on screen said why.

`note_power` now sits at the bottom of `refresh_bulb`, so every observation of a
bulb's on/off runs through it whoever caused it. And because nothing polls a
bulb unless a browser is open — `refresh_all` only touches switches —
`watch_held` reads the held bulbs on each tick. That is a read or two per minute,
since a held bulb is rare and is exactly the one whose off we are waiting for.

**A hold set on a dark lamp waits for its turn.** Releasing on the next observed
off would be wrong for a colour chosen while the lamp is off: that is not
somebody using the lamp, it is somebody choosing what it will come up at, and
the release would throw it away seconds later, before it had ever been switched
on. `heldLit` records whether the lamp has been seen lit *under this hold*, and
only then can an off end it. Its sheet reads `off · held` in the meantime.

**Releasing clears the memo, and re-arms immediately.** Two separate reasons.
The memo, because the manual value was written into it — so the next tick
compared the curve against the *manual* value, found the difference under
`ONLEVEL_STEP`, and sent nothing at all. And immediately, because waiting for
the next tick left the manual value standing in the bulb for up to
`PANEL_SCHED_TICK_SEC` — which is precisely the walk from the panel to the light
switch. `rearm` writes the curve's `OnLevel` and colour there and then: no
`MoveToLevel`, since the bulb is off, and no read-back, since it is called from
inside the read path.

**Nothing overrides a hold — not even a save.** `force` used to mean two things
at once: "write even if the memo says the bulb is already there" *and* "ignore
holds". Startup applies with `force`, so a restart stamped the curve onto a lamp
somebody was standing under. It means only the first thing now; the save paths
release the holds themselves and then apply.

**A new bulb is wired to nothing.** Adding one used to bind it on the spot to
`sws[0]` — the first switch in the file. Not the switch in the same room, not a
switch anybody picked: whichever happened to be listed first. Add a lamp in the
kitchen and it answered a button in the hall. And because a binding write
*replaces the whole table*, the same call handed that switch every other bulb in
the house at once, so a switch that drove one lamp silently began driving three.
A new switch has always said "the bindings are made from the panel"; there was
never a reason for a bulb to be different, and the bindings page writes the ACLs
too — including revoking the ones that should no longer be there — so nothing is
lost by waiting to be asked.

**The read-back waits for the bulb to settle.** On and Off do not land the
instant the command is acknowledged: the bulb still has to apply `OnLevel` to
`CurrentLevel`, and a read inside that gap returns the level it is *leaving*.
For an On that is the off value, 1 — so the sheet put `on` and `4%` on screen
together and stood by it, which is the worst possible place for a wrong number,
because a read-back is the one figure here that is meant to be beyond doubt.
There is a short settle before the read, and one retry if the answer still looks
like the gap: on, at the floor, with an `OnLevel` that says otherwise. That
cannot fire on a real value — a bulb genuinely dimmed to 1 by hand has an
`OnLevel` of 1 to match.

**Setting brightness also sets `OnLevel`.** `MoveToLevel` changes `CurrentLevel`
and nothing else, so a bulb set to 100% by hand still came back on at whatever
the schedule last wrote: you turned it off and on again and it was dimmer, with
no cause visible anywhere. One write per gesture — the control sends on release —
so this is not the flash-wear case the schedule's own threshold exists for.

**The tick's fade is two seconds, not one tick.** It used to be a whole minute,
on the theory that the light should glide from one sample to the next rather
than step. The theory was fine and the arithmetic was not: across twelve columns
the curve moves about *one level a minute*, so there was never anything to glide
across. What the long fade bought was a minute in which the bulb was in transit
while everything that reads it had already arrived — the panel included.

That is invisible while the bulb is a level away from the curve and ugly the
moment it is not. Any real gap to close — a hold released, a restart, a bulb
rejoining the schedule after being set by hand — became a full minute of the
panel saying `92%` over a lamp still at 5% and climbing. Measured on the bulb,
`move-to-level 254 600` from level 5 read **12, 28, 56, 101** over the first
twenty seconds; every one of those readings was honest, and none was what the
panel had already decided to show. At `20` the same command arrives inside two
seconds — longer than a step, shorter than a poll, so a fade always finishes
before anything looks.

A background thread checks every
`PANEL_SCHED_TICK_SEC` (60 s by default) but writes only when the slot has
changed — the check is an in-memory comparison and costs nothing. With the Pi
powered down the bulbs stay at the last value they received instead of following
the time of day.

### The editor

It is a **page**, not a dialog. A whole day, two curves and a list of bulbs do
not fit in a box in the middle of the screen, and every pixel of backdrop is a
pixel the chart does not get. `schedule` in the toolbar takes over the view;
`‹ rooms` goes back.

**Two controls, both live at once, and no switch between them.**

| | |
|---|---|
| the filled area | brightness — its height is the level |
| the line over it | colour temperature — its height is the mireds |

There used to be a brightness/colour toggle with only one of them live at a time,
which is the wrong shape for the job: setting an evening means setting both, and
remembering which mode you are in is a tax on every single edit. The pointer
picks whichever curve it is nearer, so there is nothing to choose in advance and
nothing to be in the wrong mode for.

One rule sits ahead of "nearest": below **both** curves you are standing inside
the filled area, and the filled area *is* brightness. Without it a day at 100%
pins the brightness edge to the top of the plot and every press in the fill lands
on the colour line — the largest target on the chart becomes the one you cannot
hit.

The fill is exactly what a lit tile uses on the home page: `--lit`, flat, with a
hairline along its top edge — and the same again inside a bulb's own brightness
panel. Same colour and same treatment on purpose: a bulb at 60% on a tile, a
bulb's panel at 60% and the schedule asking for 60% at this hour are the same
fact, and they should not be three different pictures.

It was tinted along its length by the colour curve for a while — the light's own
colour running through the shape that says how much of it there is — and it read
as busy: two variables in one shape, where the shape only has one job. Colour
temperature is the line, and only the line.

**The handle appears only over a line.** Anywhere else in the plot is not a
control, and a handle that follows the pointer across empty chart claims
otherwise — it also means there is no way to just *look* at the schedule without
appearing to be about to change it. Within reach, the nearer curve wins, and
nothing else.

There used to be a rule ahead of that one — below *both* curves you were inside
the filled area and therefore meant brightness — written before the reach limit
existed, and a bug the moment it did: the colour line runs below the brightness
edge for most of a normal day, so hovering just under it satisfied "below both"
and the handle jumped to the line above. Standing deep in the fill now selects
nothing at all, which is what the limit is for.

**Every hour gets a label; every column gets a line.** The labels are the scale,
in the finest granularity worth naming — a scale you have to interpolate between
is one you end up counting on your fingers, and on a phone only every third is
drawn because twenty-five do not fit. The vertical rules are not the scale
though: they mark the twelve places you can actually take hold of. Drawing one
at every hour looked tidier and was worse to use, because eleven of the
twenty-three meant nothing and none of them looked any different from the twelve
that did. The grid answers "where are the handles", which is the only question
anyone asks of it while dragging. The three horizontal rules at the quarters are
unchanged.

**Reset** loads the recommended day into the editor — warm and nearly out
overnight, cold and full over the middle of the day, warming back to candle by
bedtime, modelled on how adaptive lighting behaves rather than copied from it.
It does not save. Resetting straight into the house would make it the one
control on this page that cannot be looked at before it takes effect, and the
curve is the whole point of looking. The same day is `SCHED_DEFAULT` on the
server, so a fresh install and the button agree; both are written in *perceived*
percentages, which is the scale the chart and the tiles use.

**The dormant handles** are an open ring at each column on each curve — the
twelve places the chart can be taken hold of, on both series. Before them the
grab points were invisible until the pointer happened to find one, which made a
control look like a picture.

They are told apart from the live handle by what is inside them: nothing. No
fill, so ground and fill show through. And the curve is masked *out* inside each
ring rather than drawn through it, so a ring reads as a gap in the line rather
than a bead sitting on it — the background comes through, the line does not. The
ring under the pointer is omitted, because the live handle already stands there.

**The handle** is a circle on the curve with an arrow above and below, and its
value beside it. That is the whole vocabulary, and it needs no legend. It exists
only under the pointer, so the chart at rest is the two curves and nothing else.

It is sized against the chart's own type — an 11px ring beside 11px hour labels
and a 12px reading. Drawn at nearly twice that it read as a control borrowed from
a bigger interface; it is a marker on a hairline, not a button. What you can hit
is unchanged: the reach is 30 units whatever the handle is drawn at.

Everything that was competing with them is gone: the column separators, the
dotted ladder, the stem, the focused-column rail. What is left is three dashed
horizontal lines at the quarters, the hour labels and a dashed marker for now.

The day is **twelve columns of two hours**, and that is not a setting. It was
adjustable for a while, stepping through the divisors of 24, and the control cost
a corner of the toolbar to answer a question nobody asks twice. Two hours is fine
enough to shape an evening and coarse enough that the curve through the column
centres stays smooth; below it you are drawing noise, above it you cannot
separate dusk from night.

A schedule written with any other number of points — by the older editor, or by
hand — is resampled on the way in: each column takes the value that was in effect
at its own midpoint, so the shape of the day survives.

**Save** sits in the top bar beside the sentence that says what it will do. It is
the only thing on the page you press to make something happen, so it is the only
solid button on it.

Brightness snaps to tenths with a floor of 10%; finer is a schedule full of 61%
and 63%, which are the same light, and below a tenth is a lamp that is
technically on. Colour is finer — 25 notches, about 14 mireds a step, roughly
where a change stops being visible.

### Why the brightness axis is not linear

Matter level 1..254 is proportional to the light emitted, and the eye is not: at
127 you do not see "half", you see about three quarters. Plotted linearly the
whole useful evening range is squashed into the bottom sixth of the chart.

The axis is **L\* from CIE Lab**, which is literally "how bright it looks". Half
the height really does look half as bright — and that lands at level 47, not 127.
The reasoning is in the main README, under "Why level 127 is not half".

### What is stored but no longer used

Each point still carries a `fade`, and the file still round-trips it, but nothing
applies it: the Pi writes `OnLevel`, which has no transition time, and sends the
colour temperature with a transition of 0. It is inert data, not a setting —
changing it does nothing, and that is not a bug. The editor does not show it.

Saving validates the schedule before writing it — points in increasing order, no
two at the same minute, level within 1..254, colour temperature within
100..700 mireds, at most 24 points — and then applies it to the bulbs
immediately. The validation used to live in the switch, which refused a malformed
blob; now that the file on the Pi is the only source, nothing else would catch a
broken schedule.

## State lives on the Pi

The panel does not read from the devices when you open the page. It reads from
the state `server.py` keeps, so it answers in tens of milliseconds.

The reason is that the switch is a **sleepy device**: it does not listen
continuously, it wakes every few seconds to collect its messages, so any read
waits for its next wakeup. Measured on the real installation when the poll was
still 15 s, a page that reread everything from the device cost **27 seconds** —
`/api/lock` 15 s, `/api/bindings` 12 s — for data that only changes when we
change it ourselves. At the current 3 s poll it is faster, and still pointless.

Reads from the device happen in the background:

- when the service starts;
- immediately after a write that failed or whose result we do not know;
- rarely otherwise (`PANEL_REFRESH_SEC`, 6 hours by default).

When we write something and the switch confirms it, the value goes straight into
the state without waking the device again — the lock is read back anyway as part
of the write.

The long interval is deliberate: every read is radio traffic to a
battery-powered device, and the binding, the role and the schedule never change
on their own. The only one that can change behind our back is the lock, when a
switch in the lock role toggles it — and that goes through the write that
produces it anyway.

### The one value that is read live

Everything above is something **we** wrote, which is why it can be cached for
hours. A bulb's on/off is the exception: the wall switch sends `Toggle` straight
to the bulb over Thread and the Pi never sees it, so our copy is wrong within a
second of anybody pressing a switch. The tiles show that state, so it has to be
polled — `/api/bulbs`.

Bulbs are subscribed now, so their state arrives on its own and this polling is
a backstop rather than the mechanism. It is still tied to somebody actually
**looking**, not to the page existing:

- the tab has to be visible;
- and touched within three minutes. A panel left open on a wall tablet goes quiet
  on its own, and wakes on the first touch with a forced read — so what you see
  when you walk up to it is current, not four hours old.

The same request reads the **colour temperature**, and that is not a detail.
Brightness alone does not tell you how much light there is: a tunable-white bulb
at 2200 K is running one channel of its LEDs and puts out a fraction of what the
same level gives at 4000 K. "Level 254 and still dim" is a sentence that only
makes sense once you can see both, so the tile carries a dot in the bulb's actual
light colour and its settings sheet says the Kelvin in words.

The server coalesces: a read newer than `PANEL_BULB_TTL_SEC` (12 s) is served
from memory, so several browsers do not multiply the radio traffic. A bulb that
did not answer is not asked again for `PANEL_BULB_COLD_SEC` (300 s) — otherwise
one unplugged bulb would hold up every other device on every poll, and the panel
would feel broken across the board.

This is cheap in a way the switch reads are not: bulbs are mains-powered Thread
routers, always listening, and answer in about 40 ms.

All of a switch's attributes are read in **a single request** with three paths
(`read_switch_state`): the binding table on endpoint 1, plus locked and role from
our cluster on endpoint 2. Sequential reads do not land on the same wakeup, so
three reads cost three wakeups, one costs one. That was ~45 s against ~15 s at
the old 15 s poll interval; the ratio is what matters, and it did not change when
the interval dropped to 3 s.

The state is saved to `ota/state/panel-state.json`, atomically. A service restart
does not leave you with an empty page.

### The screen is patched, never rebuilt

`render()` reconciles: it keeps the elements it already has, moves the ones that
belong elsewhere, creates only what is new, and writes a property only when the
value actually differs.

It used to empty `#content` and build everything again. That is fine on a click
and ruinous on a timer — the bulb poll runs every 15 s, so every 15 s the whole
grid was destroyed, recreated and faded back in. Worse, if it landed while you
were dragging a tile, the element under your finger stopped existing.

What makes keeping an element possible is that its listeners are bound **once**,
at build time, and read `el._o` — the current options — when they fire.
Rebinding on every pass would be most of the work of rebuilding anyway.

Two consequences worth knowing when editing this:

- **Anything you put on an element, you take off yourself.** A `busy` class on a
  quick-action button used to be swept away by the element being replaced.
  Nothing sweeps now.
- **The entry animation lives on `.fresh`**, added at build time and dropped as
  soon as it has run. Left on the element permanently it would replay every time
  the tile moved between rooms, because inserting a node restarts its CSS
  animations — which is the flashing this exists to prevent. A timer removes it
  too, because `animationend` never fires in a tab that was never painted.

A redraw requested during a drag is **deferred**, not skipped: `DRAG.dirty` is
set and `render()` runs at the drop. And a drop moves the tile in the model and
on screen immediately, before the server is asked — waiting for the round trip
means the tile snaps back to where it came from for a moment, which reads as
"the drop did not take". If the server refuses, it goes back and says why.

### How the interface learns something changed

A revision counter, which increments **only when a value actually differs** from
what we had — not on every read.

The browser holds a **long poll**: it sends the revision it already has and the
server does not answer until that number moves, up to 25 s. So the page is
neither asking repeatedly nor waiting on an interval — it is answered the moment
a device reports something new, and costs one parked request in between.

The chain end to end is a push at every hop: device → matter-server → panel →
browser. Measured at about 0.2 s from a bulb changing to the tile moving, which
is what makes a wall-switch press look live rather than remembered.

## What the header status means

| Text | When |
|---|---|
| `panel offline` | the browser cannot reach `server.py` |
| `matter-server is not answering` | the server is alive, but the last exchange with matter-server failed: either it is not running, or the socket dropped |
| `no devices configured` | `devices.json` has no switches |
| `no device is answering` | matter-server works, but no switch answers — no RCP radio, no Thread network, or unpowered devices |
| `connected 1/2` | some answer, not all |
| `connected` | every switch in `devices.json` answered |

The server computes this and sends it in `health`, because the interface has no
way to infer it: a successful HTTP response only means `server.py` is alive, not
that there is a radio or a device.

The distinction is made by **which phase** failed, not by exception type: failing
to open the connection = a problem on the Pi; failing while waiting for the reply
= the command went out and nothing came back, so the problem is on the radio side.
Both look identical as exceptions, and if you conflate them the panel says
`matter-server is not answering` when what is actually missing is the RCP radio —
and you spend hours looking in the wrong place.

With no RCP radio and no Thread network, every read waits out the full 30 s
timeout. That cannot be shortened much: the switch is a sleepy device, and a
legitimate reply that has to survive a retry or two really can take seconds.

## Lock

Locking is a switch tile's quick action: press its icon. Locked, the icon becomes
a red padlock, the tile dims and it carries a `locked` badge — this is a state
you want to see from across the room, so it is three signals, not a word in a
corner. `lock all` / `unlock all` sit in the toolbar when there is more than one
switch, and the settings sheet has the same toggle spelled out.

After every write the panel **reads the value back**. A confirmed write does not
guarantee the value landed, and the mistake is expensive here: you would believe
you had locked when you had not.

The attribute is a BOOLEAN in the firmware, so the write sends a real `bool`,
not `1`/`0`. An integer is rejected with `CONSTRAINT_ERROR`. It cost an
afternoon on the old path, where the value went over the wire as whatever
chip-tool made of the word on its command line.

A lock-role switch has no lock toggle — it cannot lock itself, or you would have
no way left to unlock the rest from the wall. Its quick action is `identify`
instead.

The role is changed from the binding editor (`bindings`), because the role also
decides what that editor lists: a normal switch picks bulbs, a lock picks
switches. A lock does not appear in its own list and has no lock toggle on its
card.

The behavior details are in the main README, under "Locking the switches".

## Adding anything, not just bulbs

`+ device` commissions any Matter-over-Thread device with a pairing code — a
bulb, a sensor, a plug — and **there is no "what kind is it?" field**. That is
the point: a form that makes you classify a box before you have plugged it in
gets it wrong sooner or later, and then the panel believes the form. The device
is asked once it joins: `describe_device()` reads its Descriptor cluster for the
endpoint list, each endpoint's device types and the clusters it serves. That is
the only description that cannot go stale, because it comes from the device.

**A bulb is a device that turned out to be a light.** If the types it reports
include one of the lighting device types, it is filed as a bulb, given an ACL so
the switch may command it, added to the switch's binding table and put on the
schedule — none of which anybody asked for at the form. Everything else lands in
an open list and gets none of that: a sensor is read, not commanded, and giving
it an ACL and a binding would be cargo cult.

There are two entries in the toolbar rather than three, and the split is not
between kinds of device — it is between the two commissioning paths that
genuinely differ. `+ device` needs a printed code; `+ switch` is our own
firmware, which has none and is paired with a passcode baked in at compile time.

Inside `devices.json` switches and bulbs still keep their own arrays, because the
binding table, the ACL and the schedule are genuinely switch-and-bulb logic.
That is now an implementation detail: nothing in the interface asks you which
one you are holding.

Readings come back through the same poll and the same combined read as the
bulbs: temperature, humidity, CO₂, PM2.5, PM10, VOC, air quality, pressure and
illuminance, each with its label, unit and scale declared once on the server and
sent to the interface, so adding a cluster is one line and nothing in the
JavaScript. A tile shows the headline reading; the sheet shows all of them.

Node IDs are blocked by kind — 1xxx bulbs, 2xxx switches, 3xxx everything else —
so a node ID still tells you what it is at a glance.

## Driving a bulb by hand

A bulb's sheet is **two tall panels you drag**, the way a phone does it, rather
than a row of labelled sliders. Not fashion: brightness is the thing you came
here for, and a control the size of your thumb that shows its value as a filled
area is faster to read and to hit than a 6px track with a number beside it. The
fill is painted in the bulb's own light colour, so the two controls explain each
other — drag the colour and you can see what the brightness is going to look
like.

Until these existed the panel could switch a bulb and nothing else; everything
about how the light looked came from the schedule, so "why is it dim?" had no
answer you could reach in one gesture.

**One gesture, two meanings, told apart by whether it moved.** Drag the panel and
it sets the value; press and let go without moving and it toggles. That is why
there is no separate on/off pair any more — two buttons doing a job the big
control already does, taking up the room it wanted.

They send on **release**. A control that fires while you drag puts a hundred
commands on the radio for one gesture, and the bulb only ever shows the last of
them.

**The drag is absolute** — the level follows the pointer, so letting go
four-fifths of the way up the panel means four-fifths. That is the whole point of
a control shaped like a column, and anything else fights what it looks like.

It was **relative** for a while, and that is worth recording as a mistake. The
problem it solved was real: a click low in the panel meant to toggle a lamp, with
a six-pixel tremor on the way up, stopped being a tap and became *set 2%* —
measured, `level: 5` — and being a brightness change it wrote `OnLevel` and took
a hold, so it stayed that way. Making the drag relative fixed the tremor and
broke the gesture. A short deliberate drag now moved the value only by the
distance travelled, so it landed roughly where it already was: the value picked
*last* time. Aim for 80, get the old value; aim for 20, get 80; aim for 50, get
20. **A control one step behind is worse than one that occasionally overshoots.**

So it is absolute again and the tremor is handled where it belonged all along —
at the threshold. Twelve pixels is far more than a click carries and far less
than a drag, so a press meaning "toggle" stays a toggle. Verified both ways: a
6px twitch sends `toggle`, and aiming at 50/35/95 lands on 50/35/95.

Dragging past either end still reaches both ends — the value clamps, so a long
sweep up lands on 254 and a long sweep down on the 1% floor.

### What the brightness panel shows when the bulb is off

`OnLevel` — what it will come up at — and not `CurrentLevel`.

A bulb that is off reports `CurrentLevel` 1. That is its off reading, not a
setting, so showing it made the control read 4% while off and 25% the moment you
switched it on, as though pressing on had also changed the brightness. While off,
the honest number is the one the wall switch is about to produce, which is also
the one dragging the control is about to change. The caption says
`off · comes up here` so it is clear which question is being answered.

Dragging brightness sets the *current* level and not the come-up level, so
switching the lamp off and on again returns it to the schedule's value. That is
the schedule owning the light, and the caption is what makes it visible instead
of surprising.

### The colour strip's travel is the bulb's range

Not the Matter range. The spec allows 153..500 mireds; the lamp here reports
153..**454** and silently clamps anything warmer, so the bottom eighth of the
control did nothing — you dragged into it and the value sprang back the moment it
was read again.

Springing back is the right behaviour when a bulb refuses something, and the
read-back that produces it is still there as the backstop. It is the wrong way to
discover that an eighth of a control is decorative. `ColorTempPhysicalMinMireds`
and `...Max` come back in the same combined read as everything else, and the
strip's travel, its gradient and its handle all use them, so every position on it
is one the bulb can reach.

Until a bulb has been read the Matter range is used, which is the widest anything
could be — narrowing it later only ever removes travel that was not doing
anything.

The handle's travel is inset by its own height at both ends. Positioned by its
centre it was drawn half outside at the top and clipped by the rounded corner,
which read as the control running past its own limit.

### The API behind them

They post to `/api/light`, which takes `action`, `level` and `mireds` in any
combination and applies them in that order. `level` goes out as
`MoveToLevelWithOnOff`, so asking for light gives light instead of silently
arming a bulb that is switched off; `mireds` goes out with `ExecuteIfOff`, so a
colour lands even on a bulb that is off, which is when it matters most.

Brightness is perceptual, the same axis as the schedule: level 127 does not look
like half, so a control reading 50% there would be lying about the light.

After every command the server re-reads the bulb and answers with what it found,
not with what was asked for. A confirmed command is not proof the bulb obeyed,
and on a device you are looking straight at, showing the value you requested
instead of the one you got is the expensive kind of wrong.

### The links go somewhere

The `controlled by` list on a bulb and the `controls directly` list on a switch
are the only place the panel states a relationship between two devices — and so
the only place you are looking at a device you did not come here for. Each row
**opens that device's sheet**, exactly as tapping its tile would.

Dead text there meant reading the name, closing the sheet, finding the tile and
opening it: four steps to follow a link the page had already drawn. A row that
names something in the binding table but not in `devices.json` stays plain text,
since a link that does nothing is worse than no link.

## Sheets

Every sheet closes from **one X in its top right corner**, and from `Escape`, and
by clicking the backdrop. There used to be a `close` or `cancel` at the bottom of
each one, which on a tall sheet meant scrolling to the end to get out of it.

The X is a single button that lives outside `#sheet` — sheets replace their own
`innerHTML`, so a button inside would have to be re-rendered and re-bound by
every one of them. It hangs off a wrapper instead, which also carries the width,
so the X stays in the corner while the sheet scrolls underneath.

A sheet that needs something done on the way out — the two that reload the device
list — sets `sheetOnClose`. `closeSheet()` clears it before calling it, so it
fires exactly once however the sheet was dismissed.

The schedule opens wide, `min(1280px, 95vw)`: a chart of a whole day in a 420px
box is a chart you cannot aim at. The plot is also capped at `58vh`, because wide
screens are usually short ones — the drawing scales down to fit and what is left
over shows the container's own ground, so the letterboxing is invisible.

## Console

The bar at the bottom, collapsed by default. Press it and it opens a log of what
the Pi sent to the devices and what they answered, how long it took, and the
result.

It is the only place any of this is visible. **The switch has no console** in the
production build — `CONFIG_SERIAL` is off so that two image slots fit for OTA. On
the device, the feedback is the RGB LED.

The log is a ring buffer in memory, 400 lines, not a file: what matters is the
last few minutes while you are looking at the panel, not a history. It is lost
when the service restarts.

There is no streaming channel to the browser, so the interface polls and asks
only for the lines it has not seen (`/api/log?since=`). The rate adapts: ~1 s
while a command is in flight, 2.5 s with the console open, 8 s collapsed — a
panel left open on a tablet has no business hammering the Pi. The dot in the bar
pulses amber while a reply is outstanding; that matters, because the switch is a
sleepy device and does not answer instantly.

Collapsed, the bar shows how many new lines have appeared since you last looked.

## Dependencies

```bash
pip install websockets segno
```

`segno` generates the QR codes; it is pure Python and has no dependencies of its
own.

The rest is the Python standard library. Nothing is fetched from the internet at
runtime — the page is self-contained, so it works even if the Pi has no way out.

## Sharing to Apple Home

The `share` button on each card opens a commissioning window on the device and
gives you a QR code plus a manual one. You scan them from Apple Home → Add
accessory.

The device **stays** in our fabric as well — Matter allows several ecosystems at
once. Neither the binding nor the ACL is lost.

A few things worth knowing:

- It uses **ECM** (Enhanced Commissioning Method, option 1): a new, temporary
  code is generated. The factory code is no longer valid on a device that has
  already been commissioned.
- The window is capped at **900 s** by the spec; the panel asks for 10 minutes
  and shows a countdown.
- **Each ecosystem occupies a fabric slot** in the device. The spec requires at
  least 5. Check with `./scripts/commission.sh check <node>` before sharing
  widely.
- **The window has a floor of 180 s**, not 60. Ask for less and the device
  answers `INVALID_COMMAND` — which reads like a malformed request and sends you
  looking at the wrong thing entirely. The ceiling is 900 s.
- The codes come back as the command's result. Under chip-tool they did not:
  `CommissioningWindowOpener` writes them with `ChipLogProgress`, so they landed
  on stdout, and the whole feature depended on scraping a log file that logrotate
  could truncate at the wrong moment.

## Identify on the switch

The RGB LED pulses **amber, one second per pulse**, at full brightness — the same
color (`#ffa726`) and the same rhythm as the Identify button in the interface. If
you change `@keyframes beacon` in `index.html`, change the duration in
`AppTask::IdentifyStartHandler` too, otherwise "which bulb is which" looks
different on screen than it does on the wall.

The switch polls every 3 s, and Identify is *inbound* traffic — so it can take a
few seconds before it blinks. That is not a fault.

## What has not been tested

The interface and the API **have run** locally: the device list, the error path
when matter-server is missing, and command construction.

The sharing path was tested against a mocked reply: parsing the codes,
generating the QR and rendering it. That turned up a
real bug — segno emits `<svg width="29" height="29">` with no `viewBox`, so the
CSS scaling did not scale the drawing; `qr_svg()` now replaces the fixed
dimensions with a viewBox.

The binding table no longer has to be dug out of a variably-shaped reply:
matter-server returns it as a typed list, and `translate_binding()` only has to
map node ids from its fabric's numbering back into ours. That translation is the
part to watch — an untranslated table compares against `devices.json`, matches
nothing, and reports a correctly bound switch as controlling no bulbs at all.
