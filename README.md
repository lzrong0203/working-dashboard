# working-dashboard

One board for every Claude Code session you have running — on every machine,
under every account — and for the experiments and work those sessions are
doing. Transcripts never leave the machine they're on.

> 一個看板看完所有機器、所有帳號的 Claude Code session（誰在等你、誰在跑、
> 在做什麼），以及這些 session 背後的實驗與工作：假設、譜系（B 是 A 的
> ablation）、結論。對話內容不離開原機器；公司與個人資料各自留在自己的 hub。

**Status:** early (M1). Session board + experiment capture work; the lineage
graph, backfill tools and per-work-type views are next. See
[docs/architecture.md](docs/architecture.md).

## Why another dashboard?

We surveyed ~40 tools before writing this ([docs/research.md](docs/research.md)).
Session monitors track tokens, cost or one machine's processes; experiment
trackers store runs and metrics. None of them records *why* an experiment
exists ("an ablation of A that drops long-term memory, to test H") or *what it
concluded*, and none links that to the agent session that ran it. That intent
lives only in the agent's context and is lost when the session ends. This
project is the thin layer that captures it, plus a board that is honest about
what every session is doing right now.

## How it works

```
 machine A ─┐                                   ┌─ machine C
 machine B ─┼─ wd agent ──ssh──▶  hub (realm "personal")    hub (realm "work") ◀──ssh── wd agent ─┤
            │  (allow-listed       127.0.0.1 + SQLite         127.0.0.1 + SQLite                 │
            │   fields only)              ▲                          ▲
            └──────────────── your browser merges both via ssh -L ───┘
```

* **Collector** reads Claude Code's own live registry (`<config>/sessions/*.json`:
  busy / idle / waiting, tmux pane, Remote Control id) and only the newest
  `ai-title`, `last-prompt` and `away_summary` records from each transcript —
  Claude's own "what I'm doing, what's next" note. No message bodies, no tool
  output. Fields not in `wd/snapshot.py` cannot be sent.
* **Transport is ssh** (`ssh <hub> wd rpc`): no new ports, no HTTP write path,
  your existing keys are the access control.
* **Realms** are privacy boundaries. Each realm's data is stored only on its own
  hub; the browser merges realms at view time.
* **Work items** are Markdown cards with YAML frontmatter on the hub (git-friendly,
  agent-editable). Relationship types borrow W3C PROV terms: `derived_from`,
  `ablation_of`, `variant_of`, `scaled_from`, `rerun_of`, `evaluates`,
  `supersedes`, `informed_by`. Status and verdict are separate, so "done, but the
  hypothesis was refuted" is expressible.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) (installs its own Python ≥ 3.11).

```bash
uv tool install git+https://github.com/lzrong0203/working-dashboard
mkdir -p ~/.config/wd && cp examples/home-server.toml ~/.config/wd/config.toml   # edit it
wd agent --print        # see exactly what would be sent; sends nothing
wd serve &              # on the hub
wd agent                # on every machine (see deploy/ for launchd/systemd units)
```

Record experiments from inside an agent session (the session is attached
automatically):

```bash
wd exp new "v7 without long-term memory" --from v7 --rel ablation_of \
  --rel-note "memory module disabled" \
  --hypothesis "most of v7's gain comes from long-term memory" \
  --expected "accuracy drops > 5 points" --launcher ./run-v7-nomem.sh
wd exp close v7-nomem --verdict supported --conclusion "−8.3 points; memory is the main driver"
```

The [`experiment-log` skill](skills/experiment-log/SKILL.md) teaches Claude Code
to do this on its own when it launches or finishes an experiment.

## Privacy model

See [docs/privacy.md](docs/privacy.md). In short: the repo contains code and
fake fixtures only; config lives in `~/.config/wd`, data in `~/.local/share/wd`,
cards in a separate (private) data dir; `scripts/privacy-check.sh` scans
commits against a private pattern list kept outside the repo.

## License

MIT
