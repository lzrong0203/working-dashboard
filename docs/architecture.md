# Architecture

## Concepts

| Term | Meaning |
|---|---|
| **realm** | A privacy boundary, such as `personal` or `work`. Each realm has exactly one **hub**, and its data is stored only there. |
| **profile** | One Claude Code config dir on one machine (`~/.claude`, `~/.claude-work`, …), mapped to a realm. |
| **agent** | One process per machine. It collects every profile on that machine and pushes each snapshot to the profile's realm hub. |
| **hub** | Stores a realm's sessions (SQLite) and work-item cards (Markdown), and serves its board on `127.0.0.1`. |
| **item** | A card: experiment, eval, code project, ops task or project. Cards are linked to sessions and to each other by typed edges. |

## Example topology

```
laptop      profiles: personal → hub on home-server     work → hub on work-mini
work-mini   profiles: personal → hub on home-server     work → local hub (:8741)
home-server profiles: personal → local hub (:8740)

browser on laptop:
  ssh -L 8740:127.0.0.1:8740 home-server
  ssh -L 8741:127.0.0.1:8741 work-mini
  open http://127.0.0.1:8740  → the page also connects to its configured peers (8741)
```

Work data never touches the personal hub, and the reverse is also true. The
browser merges the two realms at view time.

## Data flow

1. **Collect** (`wd/collect/claude.py`), every `interval_s` (default 10 s):
   - read `<profile>/sessions/*.json` and drop entries whose pid is dead;
   - for each live session, read new bytes appended to its transcript and keep
     only the newest `ai-title`, `last-prompt`, `away_summary` and `gitBranch`.
     The first read looks at the last 4 MB; later reads parse only what was
     appended.
2. **Redact** (`wd/snapshot.py`): clip lengths. Realms with `send = "minimal"`
   lose every content field.
3. **Push** (`wd/transport.py`): `ssh <hub> ~/.local/bin/wd rpc` with
   `{"op":"ingest","envelope":…}` on stdin.
   - A snapshot is sent only if it changed, plus a heartbeat every `heartbeat_s`.
   - A failed snapshot is dropped, because the next one supersedes it.
   - A failed item operation is spooled and replayed.
4. **Ingest** (`wd/hub/store.py`): a full-state upsert per (host, profile).
   Sessions missing from the snapshot become `gone`, and every status change is
   written to `events`.
5. **Serve** (`wd/hub/server.py`): a watcher thread checks `PRAGMA data_version`
   and the cards' mtime every second, and recomputes at least every 15 s
   (attention depends on elapsed time). Browsers get the state over SSE.
6. **Attention** (`wd/attention.py`): `(level, reason)` per session. It sets the
   sort order and the badges, and level 3 triggers one ntfy push per episode.

## Work items

Cards are stored at `<hub data_dir>/items/<id>.md`. If the data dir is a git
repo, every change is committed automatically, so history is free.

```yaml
---
id: exp-20261115-a1b2
type: experiment
title: v7 without long-term memory
slug: v7-nomem
status: done            # planned | running | done | failed | abandoned | superseded
verdict: supported      # supported | refuted | inconclusive | n/a  (separate from status)
edges:
  - {type: ablation_of, to: exp-20261101-9f3c, note: memory module disabled}
hypothesis: most of v7's gain comes from long-term memory
expected: accuracy drops > 5 points
conclusion: −8.3 points; memory is the main driver
launcher: ./run-v7-nomem.sh
sessions: [laptop/personal/1f2e…]
---
free-form notes

## Log
- 2026-11-15 14:02 [observation/agent] eval at 300 samples finished, acc 0.512
```

Edge types:

- *Lineage* (at most one per item, no cycles): `derived_from`, `ablation_of`,
  `variant_of`, `scaled_from`, `rerun_of`.
- *Context* (any number): `evaluates`, `supersedes` (marks the target
  `superseded`), `informed_by`, `part_of`.

The CLI talks to the hub only, so each realm's cards have a single writer and
never produce merge conflicts.

## Security properties

- No HTTP write endpoint exists. The only writes are `ssh <hub> wd rpc`, so
  access control is your SSH keys.
- The server binds `127.0.0.1` and rejects any non-loopback `Host` or `Origin`,
  which blocks DNS rebinding. CORS is allowed only between loopback origins, for
  the realm merge.
- A hub refuses requests addressed to a different realm.
- Hub processes are meant to run at low priority with hard caps; see
  `deploy/systemd/wd-hub.service` (`Nice=19`, `CPUQuota`, `MemoryMax`,
  `IOSchedulingClass=idle`).

## Front end

- **Views and looks are separate.**
  - A *view* is a tab (Sessions, 實驗與工作).
  - A *look* is a visual world applied to the whole page: desk, studio, flap or
    kanban, set by `<html data-look>` before first paint.
  - Every look redefines all colour tokens and commits to a single colour
    scheme.
  - The data and the markup contract don't change between looks.
- **Loading.** `web/core.js` holds the shared state and imports nothing; views
  import it, and never `app.js`. `tests/web_smoke.mjs` loads the real modules
  under a DOM stub and renders every look.
- **Rendering.** Renders are coalesced with `setTimeout`, not
  `requestAnimationFrame`, which never fires without a display link.
- **Status strip.** The hub ships each session's last 12 h of transitions
  (`history`). The strip draws segments with a 2 px surface gap, gives each
  segment a hover title, and carries an `aria-label` with the same facts.

## Robustness

- **Card writes are serialised.** Every item operation runs under an exclusive
  `flock` on the data dir. Each card is validated *together with* the other
  cards it modifies (for example, the target of a `supersedes` edge) before
  anything is written. Files are replaced atomically.
- **Retries are safe.** Item requests carry a request id (`rid`). The hub stores
  each response and returns it again for a replay, so a lost reply never applies
  a request twice. Spooled requests replay in submission order, and new requests
  queue behind any backlog.
- **One bad card can't stop the board.** Malformed or duplicate cards are
  skipped and listed on the board. Every SQLite write runs in a transaction that
  rolls back on error.
- **The hub host is protected.** `wd serve` and every ssh-spawned `wd rpc`
  lower their own priority (SCHED_IDLE, nice 19, idle I/O on Linux), so the
  protection doesn't depend on the service manager. Sockets have timeouts, event
  streams are capped, and ntfy delivery runs on its own thread. A session is
  marked notified only after ntfy accepted the push.
- **Realms can't leak.** A hub checks that every session in a snapshot carries
  the snapshot's realm, host and profile. An unmapped `CLAUDE_CONFIG_DIR` is an
  error, never a guess.

## Roadmap

- **M1** (done): session board, item cards, `wd exp` / `wd item` CLI, the
  `experiment-log` skill, ntfy alerts.
- **M2**:
  - a lineage graph per experiment family, with a compare table;
  - backfill from existing launcher scripts: naming rules propose edges, and a
    human confirms them.
- **M3**:
  - views per work type (a code-project timeline, an ops checklist);
  - links to machine health;
  - a mobile digest page;
  - Codex sessions;
  - a Claude Code mod showing "N sessions waiting".
