"""The only shape of session data that ever leaves a machine.

Everything a collector learns is funnelled through ``SessionSnap`` and then
through ``redact()``. Fields not listed here cannot be sent, so adding a new
field to the wire format is always a deliberate, reviewable change.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

# Status values come straight from Claude Code ("busy" | "idle" | "waiting").
# "gone" is assigned by the hub when a session disappears from its source.
STATUSES = ("waiting", "busy", "idle", "gone", "unknown")
# Newer Claude Code states folded into the ones above; the raw value is kept in
# ``status_detail`` so nothing is lost.
STATUS_ALIASES = {"shell": "busy"}

TITLE_MAX = 120
SUMMARY_MAX = 600
PROMPT_MAX = 240
REPLY_MAX = 400
DETAIL_MAX = 60

# Fields dropped when a realm is configured with send = "minimal".
CONTENT_FIELDS = ("cwd", "git_branch", "ai_title", "away_summary", "last_prompt", "last_reply", "tmux", "bridge_session_id", "account", "account_org")


@dataclass
class SessionSnap:
    realm: str
    host: str
    profile: str
    pid: int
    session_id: str
    name: str = ""
    kind: str = "interactive"
    status: str = "unknown"
    status_since: int = 0  # epoch ms
    status_detail: str = ""  # e.g. what a waiting session waits for ("permission prompt")
    updated_at: int = 0  # epoch ms
    started_at: int = 0  # epoch ms
    version: str = ""
    cwd: str = ""
    git_branch: str = ""
    tmux: str = ""
    bridge_session_id: str = ""
    ai_title: str = ""
    ai_title_at: int = 0  # epoch ms; Claude Code writes the title once, early, so it ages
    away_summary: str = ""
    away_summary_at: int = 0  # epoch ms
    last_prompt: str = ""
    last_reply: str = ""  # Claude's latest text to you (clipped), the turn's own recap
    last_reply_at: int = 0  # epoch ms
    account: str = ""  # Claude login of this profile (email)
    account_org: str = ""
    linked_items: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.host}/{self.profile}/{self.pid}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SessionSnap":
        names = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in names})


def _clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def redact(snap: SessionSnap, send: str = "full") -> SessionSnap:
    """Apply length limits always, and drop content fields for minimal realms."""
    snap.ai_title = _clip(snap.ai_title, TITLE_MAX)
    snap.away_summary = _clip(snap.away_summary, SUMMARY_MAX)
    snap.last_prompt = _clip(snap.last_prompt, PROMPT_MAX)
    snap.last_reply = _clip(snap.last_reply, REPLY_MAX)
    snap.status_detail = _clip(snap.status_detail, DETAIL_MAX)
    if snap.status in STATUS_ALIASES:
        snap.status_detail = snap.status_detail or snap.status
        snap.status = STATUS_ALIASES[snap.status]
    if snap.status not in STATUSES:
        snap.status = "unknown"
    if send == "minimal":
        for f in CONTENT_FIELDS:
            setattr(snap, f, "")
        snap.away_summary_at = snap.last_reply_at = snap.ai_title_at = 0
    return snap
