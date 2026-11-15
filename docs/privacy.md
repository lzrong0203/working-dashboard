# Privacy model

The code is public. Your sessions and work are not. The boundary between the
two is enforced in three places.

## 1. What leaves a machine

Only the fields of `SessionSnap` (`src/wd/snapshot.py`) are sent:

| Category | Fields |
|---|---|
| Identity | realm, host label, profile label, pid, session_id, name, kind, version |
| State | status, status_since, updated_at, started_at |
| Location | cwd, git_branch, tmux pane, Remote Control id |
| Content (clipped) | `ai_title` (120 chars), `away_summary` (600), `last_prompt` (240) |

Never sent: message bodies, tool calls and results, file contents,
environment, credentials.

Set `send = "minimal"` on a realm and the content and location rows are dropped
too. Run `wd agent --print` at any time to see exactly what would be sent.

## 2. Where data is stored

| Data | Location | In this repo? |
|---|---|---|
| config | `~/.config/wd/config.toml` | no (`.gitignore`) |
| session store | `~/.local/share/wd/<realm>.db` on the realm's hub | no |
| work-item cards | `<hub data_dir>/items/*.md`; make it a **private** git repo | no |
| tests | `tests/`, fabricated data only | yes |

Realms never mix:

- A hub refuses requests addressed to another realm.
- The browser merges realms at view time, so data is never copied from one hub
  to another.
- Pushes from the work realm can be limited to a bare count (`detail = "count"`).

## 3. Keeping the public repo clean

`scripts/privacy-check.sh` scans the staged files, or the whole tree with
`--all`. It checks for:

- private IPv4 addresses, emails and tokens (built-in patterns);
- **your own private patterns** from `~/.config/wd/private-patterns.txt`, one
  regex per line: company names, project codenames, hostnames, colleague names.
  The file lives outside the repo, so the list itself is never published.

Install it as a pre-commit hook:

```bash
ln -sf ../../scripts/privacy-check.sh .git/hooks/pre-commit
```

Documentation and examples use generic machine names (laptop, home-server,
work-mini) and invented projects.

## Third parties

- **ntfy**: pushes go through the ntfy server you configure. They carry a
  session name and host label (`detail = "full"`) or only a count
  (`detail = "count"`), never titles, summaries or prompts. Use a long random
  topic name, or self-host ntfy.
- **claude.ai/code links**: the board links to a session's Remote Control page
  if one exists. That page is governed by your Claude account, not by this
  project.
