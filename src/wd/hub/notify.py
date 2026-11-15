"""Push notifications via ntfy (https://ntfy.sh or a self-hosted server).

Pushes go through a third party, so they carry as little as possible:
``detail = "full"`` sends session name + host; ``"count"`` sends only how many
sessions need you. Never titles, summaries or prompts.
"""

from __future__ import annotations

import sys
import urllib.request

from wd.attention import URGENT
from wd.config import Notify
from wd.hub.store import Store


def _post(n: Notify, title: str, message: str, tags: str = "") -> None:
    req = urllib.request.Request(
        f"{n.ntfy_server.rstrip('/')}/{n.ntfy_topic}",
        data=message.encode(),
        # HTTP headers must be ASCII; the UTF-8 body carries any CJK text
        headers={"Title": title.encode("ascii", "ignore").decode() or "wd", "Tags": tags},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[wd notify] {e}", file=sys.stderr)


def check(store: Store, state: dict, n: Notify) -> int:
    if not n.ntfy_topic:
        return 0
    fresh = [s for s in state["sessions"] if store.should_notify(s["key"], s["attention"]["level"], URGENT)]
    if not fresh:
        return 0
    realm = state["realm"]
    if n.detail == "count":
        total = sum(1 for s in state["sessions"] if s["attention"]["level"] >= URGENT)
        _post(n, f"wd {realm}", f"{total} session(s) need you", "bell")
    else:
        for s in fresh:
            _post(n, f"wd {realm} {s['host']}", f"{s.get('name') or s['session_id'][:8]}: {s['attention']['reason']}", "bell")
    return len(fresh)
