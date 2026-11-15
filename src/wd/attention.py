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

# Policy knobs (minutes unless noted). Edit these rather than the logic.
WAIT_PUSH_MIN = 2  # waiting this long -> URGENT (push). Shorter = you're probably on it.
FRESH_DONE_MIN = 15  # a turn that finished this recently -> SOON ("go read the result")
PARKED_IDLE_H = 12  # idle longer than this = deliberately parked -> no badge at all
LONG_BUSY_H = 3  # one uninterrupted busy streak this long -> FYI (stuck? runaway loop?)
STALE_SOURCE_S = 180  # owning machine silent this long -> we can't see it; never push


def _span(mins: int) -> str:
    return f"{mins} 分鐘" if mins < 120 else f"{mins / 60:.1f} 小時"


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

    if status == "gone":
        return OK, ""

    # The owning machine went quiet: what we show is a guess, so it may sit in
    # "needs you" but must never buzz the phone. (The card adds its own
    # "this machine hasn't reported" line, so the reason stays short.)
    if source_age_s > STALE_SOURCE_S:
        if status == "waiting":
            return SOON, f"可能還在等你（{_span(mins)}）"
        return FYI, ""

    if status == "waiting":
        # A permission prompt you are about to answer anyway shouldn't buzz:
        # it goes to the top of the board at once, and pushes after a grace.
        if mins >= WAIT_PUSH_MIN:
            return URGENT, f"等你 {_span(mins)}"
        return SOON, "剛開始等你"

    if status == "idle":
        # Finished work is worth reading while it's fresh; after that it is
        # either seen or parked, and a board full of old idles is just noise.
        # Idle since launch means nothing has run yet: nothing to read.
        started = int(s.get("started_at") or 0)
        if started and int(s.get("status_since") or 0) - started < 60_000:
            return OK, ""
        if mins < FRESH_DONE_MIN:
            return SOON, f"剛做完（{_span(mins)}前）"
        if mins < PARKED_IDLE_H * 60:
            return FYI, f"閒置 {_span(mins)}"
        return OK, ""

    if status == "busy" and mins >= LONG_BUSY_H * 60:
        return FYI, f"連續執行 {_span(mins)}"
    return OK, ""
