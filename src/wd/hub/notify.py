"""Push notifications via ntfy (https://ntfy.sh or a self-hosted server).

Pushes go through a third party, so they carry as little as possible:
``detail = "full"`` sends session name + host; ``"count"`` sends only how many
sessions need you. Never titles, summaries or prompts.

Delivery runs on its own thread (``Notifier``) so a slow ntfy server can never
delay the board, and a session is marked notified only after ntfy accepted it.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass

from wd.config import Notify
from wd.hub.store import episode_of


@dataclass
class Job:
    keys: list[tuple[str, str, int, int]]  # (session key, session_id, episode, level) to mark on success
    title: str
    message: str


def build_jobs(realm: str, candidates: list[dict], all_sessions: list[dict], n: Notify, threshold: int) -> list[Job]:
    if not candidates:
        return []
    marks = [(s["key"], s["session_id"], episode_of(s), s["attention"]["level"]) for s in candidates]
    if n.detail == "count":
        total = sum(1 for s in all_sessions if s["attention"]["level"] >= threshold and s["status"] != "gone")
        return [Job(marks, f"wd {realm}", f"{total} session(s) need you")]
    return [
        Job([m], f"wd {realm} {s['host']}", f"{s.get('name') or s['session_id'][:8]}: {s['attention']['reason']}")
        for m, s in zip(marks, candidates)
    ]


def post(n: Notify, title: str, message: str) -> bool:
    req = urllib.request.Request(
        f"{n.ntfy_server.rstrip('/')}/{n.ntfy_topic}",
        data=message.encode(),
        # HTTP headers must be ASCII; the UTF-8 body carries any CJK text
        headers={"Title": title.encode("ascii", "ignore").decode() or "wd", "Tags": "bell"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"[wd notify] {e}", file=sys.stderr)
        return False


class Notifier:
    """Bounded background delivery. ``mark`` is called (on this thread) after success."""

    RETRY_S = 60

    def __init__(self, n: Notify, mark):
        self.n = n
        self.mark = mark
        self.q: queue.Queue[Job] = queue.Queue(maxsize=50)
        self.inflight: set[str] = set()
        self.retry_at: dict[str, float] = {}
        # (key, session_id, episode) already delivered -- guards against a watcher
        # that computed its candidates just before the marker was committed
        self.delivered: dict[tuple, float] = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, jobs: list[Job]) -> None:
        now = time.time()
        for job in jobs:
            keys = [k for k, _, _, _ in job.keys]
            with self.lock:
                if all((k, sid, ep) in self.delivered for k, sid, ep, _ in job.keys):
                    continue
                if any(k in self.inflight or self.retry_at.get(k, 0) > now for k in keys):
                    continue
                self.inflight.update(keys)
            try:
                self.q.put_nowait(job)
            except queue.Full:
                with self.lock:
                    self.inflight.difference_update(keys)

    def _run(self) -> None:
        while True:
            job = self.q.get()
            ok = post(self.n, job.title, job.message)
            if ok:  # record delivery BEFORE releasing inflight, so nothing re-queues it
                for k, sid, ep, level in job.keys:
                    try:
                        self.mark(k, sid, ep, level)
                    except Exception as e:
                        print(f"[wd notify] mark: {e}", file=sys.stderr)
            now = time.time()
            with self.lock:
                for k, sid, ep, _ in job.keys:
                    if ok:
                        self.delivered[(k, sid, ep)] = now
                        self.retry_at.pop(k, None)
                    else:
                        self.retry_at[k] = now + self.RETRY_S
                    self.inflight.discard(k)
                if len(self.delivered) > 1000:  # forget old episodes
                    for key in sorted(self.delivered, key=self.delivered.get)[:500]:
                        del self.delivered[key]
