"""Work items ("cards"): experiments, evals, code projects, ops tasks.

One card = one Markdown file with YAML frontmatter, kept in a realm's data dir
(``~/wd-data/items/<id>.md`` on the realm's hub). Humans and agents can both
edit cards; git diffs stay readable. The hub indexes cards into SQLite.

Relationship vocabulary borrows W3C PROV terms. An experiment has at most ONE
*lineage* edge (what it was built from) and any number of *context* edges.

Concurrency: every mutation runs under an exclusive lock on the data dir
(``ItemStore.locked()``) and files are replaced atomically, because several
``wd rpc`` processes (one per ssh call) may write at the same time.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import secrets
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

TYPES = ("experiment", "eval", "code", "ops", "project")
TYPE_PREFIX = {"experiment": "exp", "eval": "eval", "code": "code", "ops": "ops", "project": "proj"}
STATUSES = ("planned", "running", "done", "failed", "abandoned", "superseded")
VERDICTS = ("supported", "refuted", "inconclusive", "n/a")

# lineage: "what was this built from" (PROV wasDerivedFrom and its subtypes)
LINEAGE_EDGES = ("derived_from", "ablation_of", "variant_of", "scaled_from", "rerun_of")
# context: any number allowed
CONTEXT_EDGES = ("evaluates", "supersedes", "informed_by", "part_of")
EDGE_TYPES = LINEAGE_EDGES + CONTEXT_EDGES

# ids and slugs become file names: keep them boring
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")

FIELD_ORDER = (
    "id", "type", "title", "slug", "project", "status", "verdict", "edges",
    "hypothesis", "change", "expected", "conclusion", "launcher", "config",
    "metric", "results", "sessions", "created", "updated",
)


class ItemError(ValueError):
    pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id(type_: str) -> str:
    return f"{TYPE_PREFIX[type_]}-{time.strftime('%Y%m%d')}-{secrets.token_hex(2)}"


def check_id(value, what: str = "id") -> str:
    if not isinstance(value, str) or not ID_RE.match(value) or ".." in value:
        raise ItemError(f"invalid {what} {value!r}: use letters, digits, '.', '_' or '-'")
    return value


def check_shape(meta: dict) -> None:
    """Types every consumer relies on. Hand-edited cards that break them are
    skipped (and listed on the board) instead of crashing the hub."""
    check_id(meta.get("id"))
    for k in ("type", "status", "title"):
        if not isinstance(meta.get(k), str):
            raise ItemError(f"field {k!r} must be a string")
    for k in ("slug", "verdict", "project"):
        if meta.get(k) is not None and not isinstance(meta[k], str):
            raise ItemError(f"field {k!r} must be a string")
    edges = meta.get("edges")
    if edges is not None and not (isinstance(edges, list) and all(isinstance(e, dict) and isinstance(e.get("type"), str) and isinstance(e.get("to"), str) for e in edges)):
        raise ItemError("edges must be a list of {type, to}")
    sessions = meta.get("sessions")
    if sessions is not None and not (isinstance(sessions, list) and all(isinstance(s, str) for s in sessions)):
        raise ItemError("sessions must be a list of strings")


@dataclass
class Item:
    meta: dict
    body: str = ""

    @property
    def id(self) -> str:
        return self.meta["id"]

    def edges(self) -> list[dict]:
        e = self.meta.get("edges") or []
        return [x for x in e if isinstance(x, dict)] if isinstance(e, list) else []

    def to_markdown(self) -> str:
        ordered = {k: self.meta[k] for k in FIELD_ORDER if self.meta.get(k) not in (None, "", [])}
        ordered.update({k: v for k, v in self.meta.items() if k not in ordered and v not in (None, "", [])})
        fm = yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False, width=100)
        return f"---\n{fm}---\n{self.body.rstrip()}\n" if self.body.strip() else f"---\n{fm}---\n"

    @classmethod
    def from_markdown(cls, text: str) -> "Item":
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
        if not m:
            raise ItemError("card has no YAML frontmatter")
        meta = yaml.safe_load(m.group(1))
        if not isinstance(meta, dict):
            raise ItemError("card frontmatter is not a mapping")
        check_shape(meta)
        return cls(meta=meta, body=m.group(2))


class ItemStore:
    """Cards on disk in one realm's data dir. Single writer: the realm's hub."""

    def __init__(self, data_dir: Path, autocommit: bool = True):
        self.root = data_dir
        self.dir = data_dir / "items"
        self.autocommit = autocommit
        self.errors: list[str] = []  # cards skipped on the last scan, for the board

    # -- reading -----------------------------------------------------------
    def all(self) -> list[Item]:
        self.errors = []
        if not self.dir.is_dir():
            return []
        out, seen = [], set()
        for p in sorted(self.dir.glob("*.md")):
            try:
                it = Item.from_markdown(p.read_text())
            except Exception as e:  # one bad card must never take the board down
                self.errors.append(f"{p.name}: {e}")
                continue
            if it.id in seen:
                self.errors.append(f"{p.name}: duplicate id {it.id!r} (skipped)")
                continue
            seen.add(it.id)
            out.append(it)
        return out

    def get(self, id_: str) -> Item | None:
        p = self.dir / f"{check_id(id_)}.md"
        return Item.from_markdown(p.read_text()) if p.is_file() else None

    def resolve(self, ref: str) -> Item:
        """Find a card by id, slug, or unique id prefix."""
        items = self.all()
        for it in items:
            if it.id == ref or it.meta.get("slug") == ref:
                return it
        hits = [it for it in items if it.id.startswith(str(ref))]
        if len(hits) == 1:
            return hits[0]
        raise ItemError(f"no unique item matches {ref!r}")

    def fingerprint(self) -> str:
        """Changes whenever any card is added, removed or edited."""
        if not self.dir.is_dir():
            return ""
        h = hashlib.sha1()
        for p in sorted(self.dir.glob("*.md")):
            try:
                st = p.stat()
            except FileNotFoundError:
                continue
            h.update(f"{p.name}:{st.st_mtime_ns}:{st.st_size};".encode())
        return h.hexdigest()

    # -- writing -----------------------------------------------------------
    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / ".wd.lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _stage(self, item: Item) -> tuple[str, Path]:
        """Write the new version to a temp file next to its destination."""
        item.meta["updated"] = now_iso()
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-", suffix=".md")
        with os.fdopen(fd, "w") as f:
            f.write(item.to_markdown())
            f.flush()
            os.fsync(f.fileno())
        return tmp, self.dir / f"{check_id(item.id)}.md"

    def validate(self, item: Item, pending: dict[str, Item] | None = None) -> None:
        """Validate ``item`` against the store plus not-yet-written ``pending`` cards."""
        m = item.meta
        check_shape(m)
        if m.get("slug") is not None:
            check_id(m["slug"], "slug")
        if m.get("type") not in TYPES:
            raise ItemError(f"type must be one of {TYPES}")
        if m.get("status") not in STATUSES:
            raise ItemError(f"status must be one of {STATUSES}")
        if m.get("verdict") not in (None, *VERDICTS):
            raise ItemError(f"verdict must be one of {VERDICTS}")
        if not isinstance(m.get("title"), str) or not m["title"].strip():
            raise ItemError("title is required")
        if m.get("edges") is not None and not isinstance(m["edges"], list):
            raise ItemError("edges must be a list")
        edges = item.edges()
        for e in edges:
            if e.get("type") not in EDGE_TYPES:
                raise ItemError(f"edge type must be one of {EDGE_TYPES}")
            if not isinstance(e.get("to"), str):
                raise ItemError("edge needs a 'to'")
        lineage = [e for e in edges if e["type"] in LINEAGE_EDGES]
        if len(lineage) > 1:
            raise ItemError("an item has at most one lineage edge (derived_from/ablation_of/variant_of/scaled_from/rerun_of)")
        known = {it.id: it for it in self.all()}
        known.update(pending or {})
        known[item.id] = item
        slug = m.get("slug")
        if slug and any(o.meta.get("slug") == slug and o.id != item.id for o in known.values()):
            raise ItemError(f"slug {slug!r} is already used")
        for e in edges:
            if e["to"] not in known:
                raise ItemError(f"edge target {e['to']!r} does not exist; create it first")
            if e["to"] == item.id:
                raise ItemError("an item cannot point at itself")
        if lineage:  # no cycles along lineage
            seen, cur = {item.id}, lineage[0]["to"]
            while cur:
                if cur in seen:
                    raise ItemError("lineage edges would form a cycle")
                seen.add(cur)
                nxt = [e for e in known[cur].edges() if e.get("type") in LINEAGE_EDGES] if cur in known else []
                cur = nxt[0]["to"] if nxt else None

    def commit_changes(self, items: list[Item], message: str) -> None:
        """Validate everything first, then write everything, then git-commit."""
        pending = {it.id: it for it in items}
        for it in items:
            self.validate(it, pending)
        staged = []
        try:  # stage every file first, so a disk error can't leave half an operation
            for it in items:
                staged.append(self._stage(it))
        except OSError:
            for tmp, _ in staged:
                Path(tmp).unlink(missing_ok=True)
            raise
        for tmp, dest in staged:  # renames are atomic and practically can't fail here
            os.replace(tmp, dest)
        self._git(message)

    def _git(self, message: str) -> None:
        if not self.autocommit or not (self.root / ".git").exists():
            return
        git = ["git", "-C", str(self.root)]
        subprocess.run([*git, "add", "-A", "items"], capture_output=True)
        subprocess.run([*git, "commit", "-q", "-m", message], capture_output=True)


# -- operations (run by the hub under ItemStore.locked()) ----------------------

def _edge(store: ItemStore, type_: str, ref: str, note: str | None = None) -> dict:
    return {"type": type_, "to": store.resolve(ref).id, **({"note": note} if note else {})}


def _effects(store: ItemStore, item: Item, new_edges: list[dict]) -> list[Item]:
    """Side effects of newly added edges on other cards (currently: supersedes)."""
    touched = []
    for e in new_edges:
        if e["type"] == "supersedes":
            old = store.resolve(e["to"])
            old.meta["status"] = "superseded"
            touched.append(old)
    return touched


def op_new(store: ItemStore, args: dict) -> dict:
    type_ = args.get("type", "experiment")
    if type_ not in TYPES:
        raise ItemError(f"type must be one of {TYPES}")
    id_ = check_id(args.get("id") or new_id(type_))
    if store.get(id_) is not None:
        raise ItemError(f"item {id_} already exists")
    meta = {
        "id": id_,
        "type": type_,
        "title": args.get("title"),
        "slug": args.get("slug"),
        "project": args.get("project"),
        "status": args.get("status") or ("running" if type_ in ("experiment", "eval") else "planned"),
        "edges": [],
        "hypothesis": args.get("hypothesis"),
        "change": args.get("change"),
        "expected": args.get("expected"),
        "launcher": args.get("launcher"),
        "config": args.get("config"),
        "metric": args.get("metric"),
        "sessions": [args["session"]] if args.get("session") else [],
        "created": now_iso(),
    }
    if args.get("from"):
        meta["edges"].append(_edge(store, args.get("rel") or "derived_from", args["from"], args.get("rel_note")))
    for e in args.get("edges") or []:
        meta["edges"].append(_edge(store, e["type"], e["to"], e.get("note")))
    item = Item(meta=meta, body=args.get("body", ""))
    store.commit_changes([item, *_effects(store, item, meta["edges"])], f"wd: new {type_} {id_} {meta['title']}")
    return {"id": item.id, "item": item.meta}


def op_note(store: ItemStore, args: dict) -> dict:
    item = store.resolve(args["ref"])
    kind = args.get("kind", "observation")
    who = args.get("author", "agent" if args.get("session") else "human")
    stamp = time.strftime("%Y-%m-%d %H:%M")
    text = " ".join(str(args["text"]).split())
    body = item.body.rstrip()
    if "## Log" not in body:
        body += "\n\n## Log\n"
    item.body = body.rstrip() + "\n" + f"- {stamp} [{kind}/{who}] {text}" + "\n"
    _attach_session(item, args.get("session"))
    store.commit_changes([item], f"wd: note {item.id}")
    return {"id": item.id}


def op_set(store: ItemStore, args: dict) -> dict:
    item = store.resolve(args["ref"])
    for k, v in (args.get("fields") or {}).items():
        if k in ("id", "type", "created", "edges", "updated"):
            raise ItemError(f"field {k!r} cannot be set directly")
        item.meta[k] = v
    added = [_edge(store, e["type"], e["to"], e.get("note")) for e in args.get("add_edges") or []]
    item.meta["edges"] = [*item.edges(), *added]
    _attach_session(item, args.get("session"))
    # validation covers the item AND the cards its new edges modify, before any write
    store.commit_changes([item, *_effects(store, item, added)], f"wd: update {item.id}")
    return {"id": item.id, "item": item.meta}


def op_close(store: ItemStore, args: dict) -> dict:
    fields = {"status": args.get("status") or "done"}
    for k in ("verdict", "conclusion", "results"):
        if args.get(k):
            fields[k] = args[k]
    return op_set(store, {"ref": args["ref"], "fields": fields, "session": args.get("session")})


def op_link(store: ItemStore, args: dict) -> dict:
    item = store.resolve(args["ref"])
    if not args.get("session"):
        raise ItemError("no session to link (run inside a Claude Code session or pass --session)")
    _attach_session(item, args["session"])
    store.commit_changes([item], f"wd: link {item.id}")
    return {"id": item.id}


def _attach_session(item: Item, session: str | None) -> None:
    if not session:
        return
    sessions = [s for s in (item.meta.get("sessions") or []) if isinstance(s, str)]
    if session not in sessions:
        sessions.append(session)
    item.meta["sessions"] = sessions


OPS = {"item.new": op_new, "item.note": op_note, "item.set": op_set, "item.close": op_close, "item.link": op_link}
