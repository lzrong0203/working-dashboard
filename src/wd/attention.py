"""How much does a session need *you* right now?

The hub calls ``attention()`` for every session on every update. The result
drives three things:

* sort order on the board (highest level first),
* the badge/colour of each card,
* push notifications: a session that reaches ``URGENT`` triggers one ntfy push
  per episode (it must drop below URGENT before it can notify again).
"""

from __future__ import annotations

URGENT = 3  # needs you now -> push notification
SOON = 2  # look when you get a chance (e.g. finished and forgotten)
FYI = 1  # worth a glance (e.g. unusually long run)
OK = 0  # nothing to do

LEVEL_NAMES = {URGENT: "urgent", SOON: "soon", FYI: "fyi", OK: "ok"}


def attention(s: dict, now_ms: int, source_age_s: float) -> tuple[int, str]:
    """Return ``(level, reason)`` for one session.

    s             -- a session row: status ("waiting" | "busy" | "idle" | "gone"),
                     status_since (epoch ms, when the status last changed),
                     started_at, name, ai_title, away_summary, kind ...
    now_ms        -- current time, epoch ms
    source_age_s  -- seconds since the machine/profile that owns this session
                     last reported (large = its agent or network is down)

    ``reason`` is a short human phrase shown on the card, e.g. "等你 4 分鐘".
    """
    status = s.get("status", "unknown")
    mins = max(0, (now_ms - int(s.get("status_since") or now_ms)) // 60000)

    # TODO(you): this is the policy that decides what interrupts you.
    # Questions only you can answer:
    #   - waiting: notify immediately, or only after N minutes? (a permission
    #     prompt you're about to answer anyway shouldn't buzz your phone)
    #   - idle: after how long does a finished session count as "forgotten"
    #     (SOON)? Does an idle session with an away_summary deserve more?
    #   - busy: is a very long busy streak (hours) worth an FYI?
    #   - source_age_s: when the reporting machine goes silent, should its
    #     sessions be surfaced (we can't see them anymore) or muted?
    # Placeholder so the board works until you replace it:
    if status == "waiting":
        return URGENT, f"等你 {mins} 分鐘"
    return OK, ""
