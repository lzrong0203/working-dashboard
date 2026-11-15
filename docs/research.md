# Prior-art survey (2026-11-15)

Before writing code we surveyed existing tools in three areas. Star counts and
last-push dates come from the GitHub API on the survey date. The question for
each area: **reuse, or build?**

## 1. Coding-agent session monitoring

**Verdict:** many tools track sessions on one machine; a few new and small ones
work across machines. None combines (a) a central view of metadata only, with
transcripts staying put, (b) several Claude Code config dirs per host plus tmux,
and (c) grouping sessions by goal or experiment. **Build a thin collector;
reuse Claude Code's own data sources.**

| Tool | ★ / last push | What it does | Why not (as the whole answer) |
|---|---|---|---|
| [Codealong](https://github.com/codealong/codealong) | 65 / 2026-11 | macOS app; reads agents' session files; peer-to-peer over Tailscale/iroh; Needs you / Working / Idle board | Weeks old; no goal or experiment layer |
| [ai-beacon](https://github.com/manusa/ai-beacon) | 44 / 2026-11 | Hook heartbeats to a central server; working / idle / awaiting-permission; push alerts | Sends task text to its server |
| [Hush](https://github.com/hushctl/hush) | 6 / 2026-07 | Per-host Rust daemon owns PTYs; browser merges hosts | Sessions must start inside Hush; gossip has no authentication |
| [agentsview](https://github.com/kenn-io/agentsview) | 6.4k / 2026-11 | History and search for 50+ agents; can push to Postgres/ClickHouse | Ships full content; parser not importable |
| [ccusage](https://github.com/ccusage/ccusage) | 19.4k / 2026-11 | Tokens and cost; accepts several config dirs | Cost only |
| [abtop](https://github.com/graykode/abtop) | 3.9k / 2026-09 | htop for agents: live processes, tokens, context | Local only |
| [claude-squad](https://github.com/smtg-ai/claude-squad), [vibe-kanban](https://github.com/BloopAI/vibe-kanban), [agent-deck](https://github.com/asheshgoplani/agent-deck), [Happy](https://github.com/slopus/happy) | 1k–29k | Orchestrators | See only the sessions they launch; single host (vibe-kanban is sunsetting) |
| [disler/claude-code-hooks-multi-agent-observability](https://github.com/disler/claude-code-hooks-multi-agent-observability) | 1.6k / 2026-02 | All 12 hook events → Bun + SQLite → Vue | Captures prompts; single host as shipped. Good template for hooks |
| [ai-observer](https://github.com/tobilg/ai-observer) | 293 / 2026-11 | OTLP receiver + JSONL watcher, DuckDB, React | Telemetry-centric, no work layer |
| Langfuse / Phoenix / Braintrust hook plugins | — | Full turns as traces | Built for debugging traces, not for "what is each session doing" |
| tmux-claude-hatch, claude-tmux | 200–400 | Scrape tmux panes to guess the state | Guessing; Claude Code already writes the state to disk |

**Official Claude Code interfaces**

- `<config>/sessions/<pid>.json`: Claude Code's live registry, with status
  (`busy`/`idle`/`waiting`), name, cwd, tmux pane and Remote Control id. This is
  our primary source because reading it costs almost nothing.
- `claude agents --json`: the documented script interface (≈0.2 s and ≈150 MB
  peak RSS per call). Used as a fallback.
- Transcript records `ai-title`, `last-prompt` and `system/away_summary`: the last
  is Claude's own first-person "what I'm doing, what's next" note, so no
  summarisation pass is needed.
- OpenTelemetry (`CLAUDE_CODE_ENABLE_TELEMETRY=1`): token, cost and activity
  metrics, with content redacted by default. It has no idle/waiting signal and
  no host name unless you set one. Optional.
- Remote Control and claude.ai/code: the only official cross-machine list of
  sessions. It is per account, cloud-stored, and has no project or experiment
  semantics.
- `CLAUDE_CODE_SESSION_ID` is present in the agent's Bash tool environment, which
  lets the CLI attach the calling session to the items it creates.

## 2. Experiment lineage

**Verdict:** no tracker stores "B is an ablation of A, tested hypothesis H,
concluded X, superseded by C" as structured data. They store runs, metrics and
at most one parent pointer. **Build a thin registry; optionally use MLflow or
Trackio for metrics.**

| Tool | ★ / status | Lineage model | Notes |
|---|---|---|---|
| [MLflow](https://github.com/mlflow/mlflow) | 28.8k, v3.18 (2026-11) | Nested runs (one parent) plus tags and a note | Lightest full tracker; `mlflow autolog claude` traces sessions. Best metrics backend candidate |
| [W&B](https://wandb.ai) | 11.3k | Run groups; artifact lineage graph | Self-host is Docker and 1 seat, non-corporate |
| [DVC](https://github.com/treeverse/dvc) | 16k (acquired by lakeFS 2025-11) | Experiments are git refs off a baseline | Requires restructuring into dvc.yaml |
| [Trackio](https://github.com/gradio-app/trackio) | 1.8k, v0.45 (2026-11) | None | Local-first SQLite, wandb-compatible API: a good light metrics store |
| Aim, Sacred/Omniboard, Guild AI, Neptune | dormant, dead or shut down | — | Avoid |
| [Inspect AI](https://inspect.aisi.org.uk) | 3.3k, active | Compares runs through `evals_df()`; Inspect Flow handles config matrices | Strong for eval matrices if the evals are written as Inspect tasks |
| [promptfoo](https://github.com/promptfoo/promptfoo) | 27.2k | Diffs one eval against another | Prompt × provider matrices |
| Braintrust | SaaS | Each experiment is auto-diffed against a `base_experiment` | The comparison UX to copy |
| [OpenResearch / orx](https://github.com/alphaXiv/OpenResearch) | 8.1k, created 2026-06 | Experiment tree; each node is a git branch with a stated hypothesis; nodes freeze once answered | Closest concept. One run command per project, and variants must differ in code, not args or env |

**Schema precedents worth borrowing**

- W3C PROV-O verbs: `wasDerivedFrom`, `wasRevisionOf`, `wasInformedBy`, `used`.
- Sumatra's per-run *reason* and *outcome*.
- RD-Agent's `Hypothesis{hypothesis, reason}` paired with
  `Feedback{decision, observations, new_hypothesis}`.
- AI Scientist v2 nodes typed as ablation, hyperparameter, replication or
  aggregation.
- OpenLineage's split between a stable *job* and its *run* instances.

## 3. Machine health and dashboards

**Verdict:** don't rebuild machine metrics. Start pages can't host per-work-type
interactive views. **Build a small custom front end; link or embed existing
health tools.**

- Machine health:
  - [Beszel](https://github.com/henrygd/beszel) (27.4k): push-mode agents over
    WebSocket, NVIDIA and Apple GPUs, REST API, but no per-process list.
  - [Glances](https://github.com/nicolargo/glances) (34k): the best per-host
    process and GPU JSON.
  - Netdata: its UI is closed-source and the free tier is capped at 5 nodes.
  - Prometheus + Grafana: the most powerful option, with the most moving parts.
- Start pages:
  - [Homepage](https://github.com/gethomepage/homepage) and Dashy: link pages
    with stat tiles.
  - [Glance](https://github.com/glanceapp/glance): Go-template HTML widgets, but
    no auto-refresh.
  - [Homarr](https://github.com/homarr-labs/homarr): custom JSX widgets, still
    young.
- Transport patterns:
  - Push beats pull when machines roam or sit behind NAT.
  - ssh is already authenticated everywhere.
  - Tailscale's free plan is for non-commercial use only. Overlay VPNs on
    company-managed machines usually need IT approval.

## What we build vs. reuse

| Layer | Decision |
|---|---|
| Session status | **Reuse** Claude Code's registry, transcript summary records and `claude agents --json` |
| Transport | **Reuse** ssh (no new ports, existing keys) |
| Experiment intent and lineage | **Build** (no existing tool models it); PROV vocabulary |
| Metrics per run | **Reuse** later: MLflow or Trackio run ids referenced from cards |
| Machine health | **Reuse** existing dashboards or Beszel; link from the board |
| UI | **Build**: a small vanilla-JS view registry, one view per work type |
