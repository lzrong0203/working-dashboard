"""Configuration.

Real configuration never lives in the repo. It is read from
``$WD_CONFIG`` or ``~/.config/wd/config.toml``; data goes to ``$WD_HOME`` or
``~/.local/share/wd``. See ``examples/`` for annotated templates.

Vocabulary:
  realm    -- a privacy boundary ("personal", "work"). Each realm has exactly one
              hub, and a realm's data is only ever stored on its hub.
  profile  -- one Claude Code config dir on this machine (``~/.claude``,
              ``~/.claude-work``), mapped to one realm.
  hub      -- the machine that stores a realm's data and serves its board.
"""

from __future__ import annotations

import re

import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = Path("~/.config/wd/config.toml")
DEFAULT_HOME = Path("~/.local/share/wd")


class ConfigError(ValueError):
    pass


def expand(p: str | Path) -> Path:
    return Path(os.path.expandvars(str(p))).expanduser()


@dataclass
class Profile:
    label: str
    config_dir: Path
    realm: str


@dataclass
class Realm:
    name: str
    # ssh targets tried in order; the literal "local" means this machine is the hub
    hub: list[str] = field(default_factory=lambda: ["local"])
    # command that runs `wd` on the hub (non-interactive ssh often lacks ~/.local/bin)
    hub_cmd: str = "~/.local/bin/wd"
    # "full" sends titles/summaries/prompts; "minimal" sends status + timestamps only
    send: str = "full"
    # local port `wd view` forwards to this realm's hub (0 = don't)
    view_port: int = 0


@dataclass
class Notify:
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    # "full" = session name + host in the push; "count" = only "N sessions waiting"
    detail: str = "full"


@dataclass
class Hub:
    realm: str = ""  # empty: this machine is not a hub
    port: int = 8740
    data_dir: Path = field(default_factory=lambda: expand("~/wd-data"))
    # other realms' hubs, merged by the *browser* (data is never copied hub-to-hub)
    peers: list[str] = field(default_factory=list)
    notify: Notify = field(default_factory=Notify)
    git_autocommit: bool = True
    # read-only research records served by the 研究 view (see wd/research.py)
    research: list = field(default_factory=list)


@dataclass
class View:
    """Viewer-side settings (the machine where you open the board)."""

    # local helper that opens claude.ai links in the right browser profile per realm
    opener_port: int = 8739
    browser: str = "Google Chrome"
    # realm -> browser profile directory, e.g. {"work": "Profile 2", "personal": "Default"}
    browser_profiles: dict[str, str] = field(default_factory=dict)
    # optional realm -> label shown on the board's open buttons (default: the
    # browser profile's own display name)
    browser_labels: dict[str, str] = field(default_factory=dict)
    # the agent keeps realms with a view_port forwarded (re-forwarding after a
    # network change), so the board never needs a manual `wd view`
    keep_tunnels: bool = True
    # optional: replay every realm's sessions into a local pixel-agents office (wd/pixel.py)
    pixel: bool = False
    pixel_port: int = 3100


@dataclass
class Config:
    host: str
    profiles: list[Profile]
    realms: dict[str, Realm]
    hub: Hub
    home: Path
    view: View = field(default_factory=View)
    interval_s: float = 10.0
    heartbeat_s: float = 60.0
    path: Path | None = None

    def realm(self, name: str) -> Realm:
        if name not in self.realms:
            self.realms[name] = Realm(name=name)
        return self.realms[name]

    def profile_for_config_dir(self, config_dir: str | None) -> Profile | None:
        target = expand(config_dir or "~/.claude").resolve()
        for p in self.profiles:
            if p.config_dir.resolve() == target:
                return p
        return None

    def current_realm(self) -> str:
        """Realm for commands run from a shell or an agent's Bash tool.

        Never guesses for an explicitly set but unknown CLAUDE_CONFIG_DIR: a
        session from an unmapped (possibly work) profile must not silently write
        into another realm.
        """
        if os.environ.get("WD_REALM"):
            return os.environ["WD_REALM"]
        explicit = os.environ.get("CLAUDE_CONFIG_DIR")
        p = self.profile_for_config_dir(explicit)
        if p:
            return p.realm
        if explicit:
            raise ConfigError(
                f"CLAUDE_CONFIG_DIR={explicit} is not a configured profile; add it to "
                f"{self.path or DEFAULT_CONFIG} or pass --realm / set WD_REALM"
            )
        if self.hub.realm:
            return self.hub.realm
        return self.profiles[0].realm if self.profiles else "personal"


def _default_profiles() -> list[Profile]:
    out = []
    for label, d, realm in (("personal", "~/.claude", "personal"), ("work", "~/.claude-work", "work")):
        if expand(d).is_dir():
            out.append(Profile(label, expand(d), realm))
    return out


def _research(raw: list) -> list:
    from wd.research import DEFAULT_FIELDS, ResearchSource

    out, names = [], set()
    for r in raw or []:
        name = str(r.get("name") or "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", name) or name in names:
            raise ConfigError(f"[[hub.research]] needs a unique lowercase name, got {name!r}")
        names.add(name)
        fields = {k: [v] if isinstance(v, str) else list(v) for k, v in (r.get("fields") or {}).items()}
        unknown = set(fields) - set(DEFAULT_FIELDS)
        if unknown:
            raise ConfigError(f"[[hub.research]] {name}: unknown fields {sorted(unknown)}")
        out.append(ResearchSource(
            name=name, path=expand(r["path"]), label=str(r.get("label") or ""),
            format=str(r.get("format") or "prereg-json"),
            overlay=expand(r["overlay"]) if r.get("overlay") else None, fields=fields,
            forbidden=[str(x) for x in r.get("forbidden", [])],
        ))
    return out


def load(path: str | Path | None = None) -> Config:
    path = expand(path or os.environ.get("WD_CONFIG") or DEFAULT_CONFIG)
    raw: dict = {}
    if path.exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)

    profiles = [
        Profile(p["label"], expand(p["config_dir"]), p.get("realm", p["label"]))
        for p in raw.get("profiles", [])
    ] or _default_profiles()
    labels = [p.label for p in profiles]
    if len(labels) != len(set(labels)):
        raise ConfigError(f"duplicate profile labels in {path}: {labels}")
    dirs = [str(p.config_dir) for p in profiles]
    if len(dirs) != len(set(dirs)):
        raise ConfigError(f"the same config_dir is listed twice in {path}")

    realms = {}
    for name, r in raw.get("realms", {}).items():
        hub = r.get("hub", ["local"])
        realms[name] = Realm(
            name=name,
            hub=[hub] if isinstance(hub, str) else list(hub),
            hub_cmd=r.get("hub_cmd", "~/.local/bin/wd"),
            send=r.get("send", "full"),
            view_port=int(r.get("view_port", 0)),
        )
    for p in profiles:
        realms.setdefault(p.realm, Realm(name=p.realm))

    h = raw.get("hub", {})
    n = h.get("notify", {})
    hub = Hub(
        realm=h.get("realm", ""),
        port=int(h.get("port", 8740)),
        data_dir=expand(h.get("data_dir", "~/wd-data")),
        peers=list(h.get("peers", [])),
        notify=Notify(
            ntfy_topic=n.get("ntfy_topic", ""),
            ntfy_server=n.get("ntfy_server", "https://ntfy.sh"),
            detail=n.get("detail", "full"),
        ),
        git_autocommit=bool(h.get("git_autocommit", True)),
        research=_research(h.get("research", [])),
    )

    v = raw.get("view", {})
    view = View(
        opener_port=int(v.get("opener_port", 8739)),
        browser=str(v.get("browser", "Google Chrome")),
        browser_profiles={str(k): str(val) for k, val in (v.get("browser_profiles") or {}).items()},
        browser_labels={str(k): str(val) for k, val in (v.get("browser_labels") or {}).items()},
        keep_tunnels=bool(v.get("keep_tunnels", True)),
        pixel=bool(v.get("pixel", False)),
        pixel_port=int(v.get("pixel_port", 3100)),
    )

    return Config(
        view=view,
        host=raw.get("host") or socket.gethostname().split(".")[0],
        profiles=profiles,
        realms=realms,
        hub=hub,
        home=expand(os.environ.get("WD_HOME") or raw.get("home") or DEFAULT_HOME),
        interval_s=float(raw.get("interval_s", 10)),
        heartbeat_s=float(raw.get("heartbeat_s", 60)),
        path=path if path.exists() else None,
    )
