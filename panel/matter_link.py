"""A link to python-matter-server, for the readings that must not be polled.

WHY THIS EXISTS
---------------
A door sensor is on battery, so it is an intermittently connected device. Its
poll interval governs how fast it RECEIVES, not how fast it can SEND - and that
asymmetry is the whole story:

    polling      we ask; the request waits at the device's Thread parent until
                 the device next checks its mailbox. For a SIT device that is up
                 to 15 s by spec, and the read simply fails if it lands wrong.
    subscribing  the device transmits the moment the value changes. Nothing is
                 queued for it and nothing waits.

So polling can never be prompt, however tight the loop, and chip-tool's
interactive server cannot subscribe: `subscribe-by-id` there returns the current
value as the command's result and drops the subscription with it. Measured
against the real sensor, six state changes were pushed here while the polling
panel noticed four, tens of seconds late.

WHAT IT DOES NOT DO
-------------------
Commission, bind, write ACLs, or drive lights. Those still go through chip-tool.
This is deliberately only the read path for devices that deserve a subscription,
so the migration can be judged one piece at a time instead of all at once.

FAILURE
-------
If matter-server is down or the socket drops, every linked node stops being
heard from, `sub_heard` goes stale, and the ordinary poller picks them up again
within `SUB_SILENCE`. A broken link costs latency, never correctness - which is
the lesson from the last attempt at this, where a silent subscription froze a
door sensor on "closed" because nothing was polling it any more.
"""

import json
import threading
import time

from websockets.sync.client import connect as ws_connect


class MatterLink:
    """Holds one socket to matter-server and turns pushed reports into state.

    Runs on its own thread. The panel's own chip-tool socket is untouched: this
    talks to a different process entirely, so nothing here can wedge the client
    that the lights depend on.
    """

    def __init__(self, url, on_value, log):
        self._url = url
        self._on_value = on_value      # (our_node, cluster, attribute, value)
        self._log = log
        # matter-server assigns its own node ids on its own fabric, so 1004 here
        # is 1 there. devices.json carries the mapping.
        self._theirs_to_ours = {}
        self._lock = threading.Lock()

    def set_map(self, mapping: dict):
        with self._lock:
            self._theirs_to_ours = {int(k): int(v) for k, v in mapping.items()}

    def _ours(self, theirs):
        with self._lock:
            return self._theirs_to_ours.get(int(theirs))

    def run(self):
        backoff = 5
        while True:
            try:
                self._session()
                backoff = 5
            except Exception as exc:  # noqa: BLE001 - a dropped link is normal
                self._log(f"matter-server link lost: {exc}", "warn")
            time.sleep(backoff)
            backoff = min(120, backoff * 2)

    def _session(self):
        conn = ws_connect(self._url, open_timeout=10, ping_interval=20)
        try:
            hello = json.loads(conn.recv(timeout=15))
            self._log(f"matter-server {hello.get('sdk_version')}, "
                      f"fabric {hello.get('fabric_id')}", "ok")

            # Everything it already knows, before anything changes.
            conn.send(json.dumps({"message_id": "nodes", "command": "get_nodes"}))
            # Then the stream. From here the device speaks when it wants to.
            conn.send(json.dumps({"message_id": "listen",
                                  "command": "start_listening"}))

            last_sweep = time.time()
            while True:
                # A quiet door is the normal case, so a recv timeout is not an
                # event - it certainly is not a reason to drop the socket. The
                # first version returned here, which reconnected every 30 s and
                # re-fetched on each reconnect: an accidental 40-second poller,
                # slower than the one it replaced, and it looked exactly like a
                # sensor stuck on its last value.
                try:
                    raw = conn.recv(timeout=5)
                except TimeoutError:
                    raw = None
                if raw is not None:
                    self._absorb(raw)

                # A cheap correctness backstop, every minute. This does not
                # touch the device: matter-server answers from the cache its own
                # subscription keeps current. It exists so that a subscription
                # dying quietly on ITS side cannot strand a value here - and,
                # because liveness is fed only by data actually arriving, a
                # matter-server that stops answering hands the node back to the
                # ordinary poller instead of freezing it.
                if time.time() - last_sweep > 60:
                    last_sweep = time.time()
                    conn.send(json.dumps({"message_id": "nodes",
                                          "command": "get_nodes"}))
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _absorb(self, raw):
        if not isinstance(raw, str) or not raw.lstrip().startswith("{"):
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            return

        # The initial dump, in reply to get_nodes.
        if msg.get("message_id") == "nodes":
            for node in msg.get("result") or []:
                ours = self._ours(node.get("node_id"))
                if ours is None:
                    continue
                for path, val in (node.get("attributes") or {}).items():
                    self._feed(ours, path, val)
            return

        # And every change after it. This is the part that is not polling.
        if msg.get("event") == "attribute_updated":
            data = msg.get("data")
            if not isinstance(data, list) or len(data) < 3:
                return
            theirs, path, val = data[0], data[1], data[2]
            ours = self._ours(theirs)
            if ours is not None:
                self._feed(ours, path, val)

    def _feed(self, node, path, val):
        """path is "<endpoint>/<cluster>/<attribute>"."""
        bits = str(path).split("/")
        if len(bits) != 3:
            return
        try:
            _ep, cluster, attr = (int(b) for b in bits)
        except ValueError:
            return
        self._on_value(node, cluster, attr, val)
