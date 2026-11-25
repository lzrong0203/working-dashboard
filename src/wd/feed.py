"""One long-lived ``/api/events`` subscription per local hub port.

The agent used to GET ``/api/state`` every few seconds to see whether a realm's
board was reachable (and, for the pixel bridge, what was on it). Through an ssh
forward every GET is a new channel, so the hub machine opened a fresh TCP
connection each time and was left with a TIME_WAIT per request. A feed keeps
one stream open instead: the hub sends the full state on connect and on every
change, and a ping at least every 15 s, so "reachable" is simply "the stream is
up and was heard from recently". It reconnects with backoff when it drops.
"""

from __future__ import annotations

import http.client
import json
import threading
import time

PING_GAP_S = 15.0       # the hub writes at least this often (state or ": ping")
STALE_S = 3 * PING_GAP_S
READ_TIMEOUT_S = STALE_S
MAX_BACKOFF_S = 30.0


class Feed:
    def __init__(self, port: int, host: str = "127.0.0.1"):
        self.port, self.host = port, host
        self.state: dict | None = None  # replaced whole, never mutated: readers may keep a reference
        self.connected = False
        self.heard = 0.0                # time.monotonic() of the last line from the hub
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()
        self.wake = threading.Event()   # kick(): retry now instead of after the backoff

    def start(self) -> "Feed":
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name=f"wd-feed-{self.port}", daemon=True)
            self.thread.start()
        return self

    def alive(self) -> bool:
        return self.connected and time.monotonic() - self.heard < STALE_S

    def latest(self) -> dict | None:
        """The board's current state, or None while the stream is down."""
        return self.state if self.alive() else None

    def kick(self) -> None:
        """The port works again (e.g. a forward was just re-added): reconnect now."""
        self.wake.set()

    def wait_alive(self, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.alive():
                return True
            time.sleep(0.05)
        return self.alive()

    def _run(self) -> None:
        delay = 1.0
        while not self.stop.is_set():
            began = time.monotonic()
            try:
                self._stream()
            except (OSError, http.client.HTTPException, ValueError):
                pass
            finally:
                self.connected = False
            if time.monotonic() - began > 60:  # it was a working stream: come back quickly
                delay = 1.0
            if self.stop.is_set():
                return
            if self.wake.wait(delay):
                self.wake.clear()
                delay = 1.0
            else:
                delay = min(MAX_BACKOFF_S, delay * 2)

    def _stream(self) -> None:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=READ_TIMEOUT_S)
        try:
            conn.request("GET", "/api/events", headers={"Accept": "text/event-stream"})
            resp = conn.getresponse()
            if resp.status != 200:
                raise OSError(f"HTTP {resp.status}")
            self.connected, self.heard = True, time.monotonic()
            event, data = "", []
            while not self.stop.is_set():
                raw = resp.readline()
                if not raw:
                    return  # the hub (or the tunnel) closed the stream
                self.heard = time.monotonic()
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:  # blank line: dispatch the event
                    if event == "state" and data:
                        self.state = json.loads("\n".join(data))
                    event, data = "", []
                elif line.startswith(":"):
                    continue  # comment / keep-alive ping
                elif line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].removeprefix(" "))
        finally:
            conn.close()


_feeds: dict[int, Feed] = {}
_lock = threading.Lock()


def feed(port: int) -> Feed:
    """The (started) feed for a hub served on 127.0.0.1:<port>, shared per process."""
    with _lock:
        f = _feeds.get(port)
        if f is None:
            f = _feeds[port] = Feed(port).start()
        return f
