"""Work items ("cards"): experiments, evals, code projects, ops tasks.

One card = one Markdown file with YAML frontmatter, kept in a realm's data dir
(``~/wd-data/items/<id>.md`` on the realm's hub). Humans and agents can both
edit cards; git diffs stay readable. The hub indexes cards into SQLite.

Relationship vocabulary borrows W3C PROV terms. An experiment has at most ONE
*lineage* edge (what it was built from) and any number of *context* edges.
"""

from __future__ import annotations

import re
import secrets
import subprocess
import time
from dataclasses import dataclass, field
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


@dataclass
class Item:
    meta: dict
    body: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.meta["id"]

    def edges(self) -> list[dict]:
        return list(self.meta.get("edges") or [])

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
        meta = yaml.safe_load(m.group(1)) or {}
        if "id" not in meta:
            raise ItemError("card frontmatter has no id")
        return cls(meta=meta, body=m.group(2))


class ItemStore:
    """Cards on disk in one realm's data dir. Single writer: the realm's hub."""

    def __init__(self, data_dir: Path, autocommit: bool = True):
        self.root = data_dir
        self.dir = data_dir / "items"
        self.autocommit = autocommit

    # -- reading -----------------------------------------------------------
    def all(self) -> list[Item]:
        if not self.dir.is_dir():
            return []
        out = []
        for p in sorted(self.dir.glob("*.md")):
            try:
                out.append(Item.from_markdown(p.read_text()))
            except (ItemError, yaml.YAMLError, OSError):
                continue
        return out

    def resolve(self, ref: str) -> Item:
        """Find a card by id, slug, or unique id prefix."""
        items = self.all()
        for it in items:
            if it.id == ref or it.meta.get("slug") == ref:
                return it
        hits = [it for it in items if it.id.startswith(ref)]
        if len(hits) == 1:
            return hits[0]
        raise ItemError(f"no unique item matches {ref!r}")

    def mtime(self) -> float:
        if not self.dir.is_dir():
            return 0.0
        return max((p.stat().st_mtime for p in self.dir.glob("*.md")), default=0.0)

    # -- writing -----------------------------------------------------------
    def save(self, item: Item, message: str) -> Item:
        self.validate(item)
        item.meta["updated"] = now_iso()
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{item.id}.md").write_text(item.to_markdown())
        self._commit(message)
        return item

    def validate(self, item: Item) -> None:
        m = item.meta
        if m.get("type") not in TYPES:
            raise ItemError(f"type must be one of {TYPES}")
        if m.get("status") not in STATUSES:
            raise ItemError(f"status must be one of {STATUSES}")
        if m.get("verdict") not in (None, *VERDICTS):
            raise ItemError(f"verdict must be one of {VERDICTS}")
        edges = item.edges()
        for e in edges:
            if e.get("type") not in EDGE_TYPES:
                raise ItemError(f"edge type must be one of {EDGE_TYPES}")
            if not e.get("to"):
                raise ItemError("edge needs a 'to'")
        lineage = [e for e in edges if e["type"] in LINEAGE_EDGES]
        if len(lineage) > 1:
            raise ItemError("an item has at most one lineage edge (derived_from/ablation_of/variant_of/scaled_from/rerun_of)")
        known = {it.id: it for it in self.all()}
        known[item.id] = item
        for e in edges:
            if e["to"] not in known:
                raise ItemError(f"edge target {e['to']!r} does not exist; create it first")
        if lineage:  # no cycles along lineage
            seen, cur = {item.id}, lineage[0]["to"]
            while cur:
                if cur in seen:
                    raise ItemError("lineage edges would form a cycle")
                seen.add(cur)
                nxt = [e for e in known[cur].edges() if e.get("type") in LINEAGE_EDGES] if cur in known else []
                cur = nxt[0]["to"] if nxt else None

    def _commit(self, message: str) -> None:
        if not self.autocommit or not (self.root / ".git").exists():
            return
        git = ["git", "-C", str(self.root)]
        subprocess.run([*git, "add", "items"], capture_output=True)
        subprocess.run([*git, "commit", "-q", "-m", message], capture_output=True)


# -- operations (shared by CLI-local and hub RPC) -----------------------------

def op_new(store: ItemStore, args: dict) -> dict:
    type_ = args.get("type", "experiment")
    if type_ not in TYPES:
        raise ItemError(f"type must be one of {TYPES}")
    meta = {
        "id": args.get("id") or new_id(type_),
        "type": type_,
        "title": args["title"],
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
        parent = store.resolve(args["from"])
        meta["edges"].append({"type": args.get("rel") or "derived_from", "to": parent.id, **({"note": args["rel_note"]} if args.get("rel_note") else {})})
    for e in args.get("edges") or []:
        meta["edges"].append({"type": e["type"], "to": store.resolve(e["to"]).id, **({"note": e["note"]} if e.get("note") else {})})
    item = Item(meta=meta, body=args.get("body", ""))
    store.save(item, f"wd: new {type_} {meta['id']} {meta['title']}")
    return {"id": item.id, "item": item.meta}


def op_note(store: ItemStore, args: dict) -> dict:
    item = store.resolve(args["ref"])
    kind = args.get("kind", "observation")
    who = args.get("author", "agent" if args.get("session") else "human")
    stamp = time.strftime("%Y-%m-%d %H:%M")
    line = f"- {stamp} [{kind}/{who}] {args['text'].strip()}"
    body = item.body.rstrip()
    if "## Log" not in body:
        body += "\n\n## Log\n"
    item.body = body.rstrip() + "\n" + line + "\n"
    _attach_session(item, args.get("session"))
    store.save(item, f"wd: note {item.id}")
    return {"id": item.id}


def op_set(store: ItemStore, args: dict) -> dict:
    item = store.resolve(args["ref"])
    for k, v in (args.get("fields") or {}).items():
        if k in ("id", "type", "created", "edges"):
            raise ItemError(f"field {k!r} cannot be set directly")
        item.meta[k] = v
    for e in args.get("add_edges") or []:
        item.meta.setdefault("edges", []).append({"type": e["type"], "to": store.resolve(e["to"]).id, **({"note": e["note"]} if e.get("note") else {})})
        if e["type"] == "supersedes":
            old = store.resolve(e["to"])
            old.meta["status"] = "superseded"
            store.save(old, f"wd: {old.id} superseded by {item.id}")
    _attach_session(item, args.get("session"))
    store.save(item, f"wd: update {item.id}")
    return {"id": item.id, "item": item.meta}


def op_close(store: ItemStore, args: dict) -> dict:
    fields = {"status": args.get("status") or "done"}
    if args.get("verdict"):
        fields["verdict"] = args["verdict"]
    if args.get("conclusion"):
        fields["conclusion"] = args["conclusion"]
    if args.get("results"):
        fields["results"] = args["results"]
    return op_set(store, {"ref": args["ref"], "fields": fields, "session": args.get("session")})


def op_link(store: ItemStore, args: dict) -> dict:
    item = store.resolve(args["ref"])
    if not args.get("session"):
        raise ItemError("no session to link (run inside a Claude Code session or pass --session)")
    _attach_session(item, args["session"])
    store.save(item, f"wd: link {item.id}")
    return {"id": item.id}


def _attach_session(item: Item, session: str | None) -> None:
    if not session:
        return
    sessions = list(item.meta.get("sessions") or [])
    if session not in sessions:
        sessions.append(session)
    item.meta["sessions"] = sessions


OPS = {"item.new": op_new, "item.note": op_note, "item.set": op_set, "item.close": op_close, "item.link": op_link}
