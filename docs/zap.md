# Changing the data model (ZAP)

The device's endpoints, clusters and attributes are described in
`firmware/src/default_zap/light_switch.zap`. The files under `zap-generated/`,
which go into the build, are generated from it. To add or remove a cluster you
edit the `.zap` and regenerate.

## Why we do not use NCS's `zap_bootstrap.sh`

That script pulls the ZAP binary from CIPD, pinned at `v2024.08.14-nightly.1`. On
macOS ARM it fetches the **x86** build, which needs Rosetta.

Worse: even the archive named `zap-mac-arm64.zip` in the official releases
contains an **x86_64** `zap-cli` — only the GUI application (`zap.app`) is native.
Checked on `v2024.10.11-nightly`:

```
zap/zap-cli                    -> Mach-O 64-bit executable x86_64
zap/zap.app/Contents/MacOS/zap -> Mach-O 64-bit executable arm64
```

Since Rosetta goes away with the next macOS, the binary route is a dead end.

## The route that survives: ZAP from source, on native node

Matter's `zap_execution.py` supports a `ZAP_DEVELOPMENT_PATH` variable, which runs
`node src-script/zap-start.js` from a source checkout. Node is native arm64, so
the architecture of the ZAP binaries stops mattering.

```bash
git clone --depth 1 --branch v2024.08.14-nightly \
    https://github.com/project-chip/zap ~/tools/zap-src
cd ~/tools/zap-src && npm ci
```

The tag has to be **the same** as the pin in
`$NCS/modules/lib/matter/scripts/setup/zap.json`. With another version, the
generated output may no longer match the SDK's templates.

If `npm ci` complains about permissions in `~/.npm/_cacache` (leftovers from an
old `sudo npm`), use a separate cache instead of reaching for sudo:

```bash
npm ci --cache /tmp/npm-cache
```

## Regenerating

```bash
MATTER=$HOME/ncs/modules/lib/matter
ZAP_DEVELOPMENT_PATH=~/tools/zap-src ZAP_SKIP_REAL_VERSION=1 \
python3 $MATTER/scripts/tools/zap/generate.py \
    firmware/src/default_zap/light_switch.zap \
    -z "$MATTER/src/app/zap-templates/zcl/zcl.json" \
    -t "$MATTER/src/app/zap-templates/app-templates.json" \
    -o firmware/src/default_zap/zap-generated \
    --no-version-check
```

`-z` and `-t` have to be given explicitly: the paths inside the `.zap` are
relative to its original location in the NCS tree, and our copy lives in the
project, so they resolve to the wrong place.

You also need `clang-format` on PATH (`brew install clang-format`), otherwise
generation succeeds and then fails at the formatting step.

## The compatibility test

Before you rely on a ZAP version, **regenerate from the unmodified `.zap` and
compare against what is already in the repo**. That tells you whether the version
is good without mixing the answer up with your own change.

The comparison has to be made on **normalized content**, not on bytes: the files
in NCS were formatted with Nordic's `.clang-format`, while `generate.py` uses the
Matter style. The code comes out identical; the formatting does not.

```bash
python3 - <<'PY'
import re, pathlib
def norm(p):
    s = pathlib.Path(p).read_text()
    s = s.replace('\\\n', ' ')
    s = re.sub(r'//.*', '', s)
    s = re.sub(r'/\*.*?\*/', '', s, flags=re.S)
    return re.sub(r'\s+', '', s)
# compare norm(new_file) with norm(file_from_repo)
PY
```

Verified this way when the Time Synchronization cluster was added: all six
generated files came out identical in content before the change.

## What has changed so far

| When | What | Why |
|---|---|---|
| added | `TimeSynchronization` (0x0038) server on endpoint 0 | the switch has no battery-backed RTC; at the time, the switch itself ran the schedule and needed the time |

The cluster block was not written by hand but copied from
`examples/light-switch-app/light-switch-common/light-switch-app.zap` in the same
SDK — the same device type, so the definition matches the zcl metadata exactly.

Cost: **+11 KB** of flash.

Note that nothing in the firmware consumes this cluster any more. The schedule
moved to the Raspberry Pi, which keeps local time itself and writes `OnLevel` and
the color temperature into the bulbs; the switch has no clock and no schedule
left, so `TimeSynchronization` is now dead weight in the data model. Removing it
means a regeneration, and regenerations are the risky part — hence it is recorded
here rather than silently dropped.

## Why our own cluster does NOT go through ZAP

Two small attributes of ours live on a vendor cluster, `0xFFF1FC30`: `Locked` and
`Role` (see "Locking the switches" in the main README). It was originally added
for the editable schedule, which has since moved to the Pi; the cluster stayed
because the lock state genuinely has to live in the switch.

The first attempt took the obvious route: a cluster XML, an extended `zcl.json`,
regeneration, cluster on endpoint 1. It does not hold together, and the reason is
worth recording so nobody walks the path again:

1. `zap_cluster_list.py` wants a source directory for every cluster. That is
   solved with `EXTERNAL_CLUSTERS` in `chip_configure_data_model`.
2. But the generated `callback-stub.cpp` calls
   `emberAf<Name>ClusterInitCallback` and `chip::app::Clusters::<Name>::Id`. Both
   come from **app-common**, which is NOT generated from the application's
   `.zap` but from the SDK's `controller-clusters.zap`, with a different set of
   templates.
3. To have them, you would have to regenerate the whole of app-common with your
   XML included and vendor it into the project — several megabytes of generated
   code, to be resynced on every NCS update.

Not worth it for a couple of attributes. The cluster now sits on a **dynamic
endpoint**, declared in C++ in `firmware/src/lock_cluster.cpp` with raw
identifiers and `emberAfSetDynamicEndpoint`. It does not touch the generated data
model at all. The price is one extra endpoint in the Descriptor.

### Two traps, should you take the ZAP route anyway

**MEI codes are not all free.** The SDK already uses `0xFFF1FC05`, `0xFFF1FC06`
and `0xFFF1FC20` — the last one is the "Sample MEI" cluster. If you land on one,
ZAP does not complain: it overwrites your definition with its own. You find out
at link time, from an
`undefined reference to emberAfSampleMeiClusterServerInitCallback`.

**ZAP does not accept absolute paths in `xmlRoot`.** If you copy `zcl.json`
somewhere else and rewrite the paths as absolute, loading the package fails
silently and you get an unrelated message much later:

```
Unknown cluster "Access Control" in attributeAccessInterfaceAttributes
```

Verified: the same file works with relative paths and fails with absolute ones,
and it fails the same way without our own cluster — so the cluster is not to
blame. What does work is a temporary directory of symlinks to everything in
`zcl/`, plus a symlink to your XML, with `zcl.json` written there using the
original relative paths.

## What is NOT in the data model, on purpose

`Switch` (0x003B, Generic Switch). Without it the switch exposes no press events,
so it shows up with no actions in Apple Home. It drives the bulb directly, through
the binding, and that is all. See the main README.
