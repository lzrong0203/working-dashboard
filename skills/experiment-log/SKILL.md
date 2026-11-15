---
name: experiment-log
description: Record an experiment's intent before launching it and its outcome after, using the `wd` CLI (working-dashboard). Use whenever you are about to start an experiment, ablation, eval, benchmark run, training run or parameter sweep, when such a run finishes or fails, or when the user asks what was tried, why, or how experiments relate. Also use when you notice the user re-running a variant of an earlier experiment.
---

# Experiment log

The user runs many experiments that build on each other. The reason each one
exists is known to you right now and to no one later. Your job is to write it
down at the two moments that matter, launch and outcome, so it isn't lost when
this session ends.

`wd` attaches this session automatically and chooses the right realm (personal
or work) from the environment. If `wd` is not on PATH, try `~/.local/bin/wd`.
If neither exists, skip this skill silently.

## Before launching a run

1. Find the parent, i.e. the experiment this one changes:
   ```bash
   wd item list --type experiment            # ids, status, verdict, title
   wd item show <id-or-slug>                 # full card, including its hypothesis
   ```
2. Create the card **before** you start the run:
   ```bash
   wd exp new "<short title>" \
     --from <parent id/slug> --rel <ablation_of|variant_of|scaled_from|rerun_of|derived_from> \
     --rel-note "<what differs from the parent, in a few words>" \
     --hypothesis "<what we believe / want to learn>" \
     --expected "<measurable expectation, e.g. acc drops > 5 pts>" \
     --launcher "<exact command or script>" --metric "<primary metric>" \
     --slug <memorable-slug> --project <project>
   ```
   Pick the relation:
   - `ablation_of`: something was removed or disabled.
   - `variant_of`: a design alternative.
   - `scaled_from`: only the size changed (data, eval set, model).
   - `rerun_of`: same config, new seed or retry.
   - `derived_from`: anything else.

   Omit `--from` only for a genuinely new line of work.
3. Tell the user the id in one line, e.g. `recorded as exp-20261115-a1b2 (ablation of v7)`.

## During a run

Record observations that change the plan:

```bash
wd item note <id> "OOM at batch 64, retrying at 32" --kind observation
wd item note <id> "dropping the 600-sample eval, too slow" --kind decision
```

## After a run

```bash
wd exp close <id> --verdict <supported|refuted|inconclusive|n/a> \
  --conclusion "<one sentence with the number that matters>" \
  --results "<path to results or a short metric summary>" [--status failed|abandoned]
```

The verdict is about the hypothesis, not about whether the code ran. A crash is
`--status failed --verdict n/a`.

If a new experiment replaces an old one as the reference, run
`wd item set <new> --edge supersedes:<old>`.

## Rules

- Write the hypothesis in the user's language, in one or two sentences. State it
  as a falsifiable claim, not as a task.
- Never invent a parent or a result. If you're unsure which experiment is the
  parent, ask the user once.
- Don't create cards for smoke tests or for runs that only check the code works.
- If `wd` prints `queued (hub unreachable)`, that's fine: it retries
  automatically.
