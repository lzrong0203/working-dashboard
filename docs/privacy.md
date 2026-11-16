# Privacy model

The code is public. Your sessions and work are not. The boundary between the
two is enforced in three places.

## 1. What leaves a machine

Only the fields of `SessionSnap` (`src/wd/snapshot.py`) are sent:

| Category | Fields |
|---|---|
| Identity | realm, host label, profile label, pid, session_id, name, kind, version |
| State | status, status_since, activity (category of the last tool call: edit / read / run / delegate / plan — never its arguments), status_detail (what a waiting session waits for, e.g. "permission prompt"), updated_at, started_at |
| Location | cwd, git_branch, tmux pane, Remote Control id |
| Content (clipped) | `ai_title` (120 chars), `away_summary` (600), `last_prompt` (600), `last_reply` (1200: the start of Claude's latest message to you, so the board can show a recap) |

Never sent: earlier messages, subagent messages, tool calls and results, file
contents, environment, credentials.

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

## The browser-profile opener

`wd/opener.py` is the only local endpoint that *does* something: it opens a
link in a browser profile. It has these guards:

- It listens on `127.0.0.1` only, and checks that `Host` and `Origin` are
  loopback.
- It accepts only `POST` with a custom header, so a foreign web page can't fire
  it without a CORS preflight, which it refuses.
- It opens only `https://claude.ai/` URLs.
- It opens them only for realms you configured.
- It launches the browser with an argument list. No shell is involved.

The Claude account email shown on the board is read from each profile's local
`.claude.json`. Like other content fields, it goes only to that realm's hub and
is dropped in minimal mode.

## Third parties

- **Google Fonts**: the board page loads its fonts from Google Fonts. Delete the
  `<link>` tags in `web/index.html` if you want it fully offline; everything
  falls back to system fonts.

- **ntfy**: pushes go through the ntfy server you configure. They carry a
  session name and host label (`detail = "full"`) or only a count
  (`detail = "count"`), never titles, summaries or prompts. Use a long random
  topic name, or self-host ntfy.
- **claude.ai/code links**: the board links to a session's Remote Control page
  if one exists. That page is governed by your Claude account, not by this
  project.
