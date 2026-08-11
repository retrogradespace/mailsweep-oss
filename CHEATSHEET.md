# MailSweep cheat sheet

## What runs automatically

A **launchd job** (`com.mailsweep.daily`) runs `mailsweep scan` every day at
**7:30am**, no action needed from you. Each run:

1. Pulls new mail (last 7 days, capped at 60/account) from every enabled
   Mail.app account via AppleScript/JXA.
2. Classifies each message (rules first, local Ollama LLM for anything
   ambiguous or event-shaped).
3. Queues newsletter/marketing/notification senders with a `List-Unsubscribe`
   header into the unsubscribe queue.
4. Cross-checks any detected events against your Calendar so it doesn't
   re-suggest things you already have.
5. Writes the digest and prints a terminal summary.

**It never moves mail, deletes anything, sends an unsubscribe, or creates a
calendar event on its own.** Those all require you to run a `review` command.

- Job status: `launchctl list com.mailsweep.daily`
- Logs: `/tmp/mailsweep.log` (stdout), `/tmp/mailsweep.err` (errors)
- Restart it: `launchctl unload ~/Library/LaunchAgents/com.mailsweep.daily.plist && launchctl load ~/Library/LaunchAgents/com.mailsweep.daily.plist`

## Commands you run

| Command | What it does |
|---|---|
| `mailsweep scan` | Run a scan right now instead of waiting for 7:30am. Add `--lookback N` to look back further than 7 days. |
| `mailsweep digest --open` | Re-render and open the HTML digest without rescanning. |
| `mailsweep unsub list` | Print pending unsubscribe candidates (no action). Each shows which account/inbox and recipient address (`to:`) it landed on. |
| `mailsweep unsub review` | Walk through each pending candidate (shows inbox, recipient address, last subject): **y**=unsubscribe now, **n**=skip, **p**=protect (never suggest again), **t**=move that sender's scanned messages to Trash, **a**=move them to Archive instead, **q**=quit. `t`/`a` re-prompt the same sender afterward, so you can still hit `y`/`n`/`p` for the subscription decision. |
| `mailsweep events list` | Print pending event candidates (no action), including location (`@ ...`), inbox/account, and recipient address. |
| `mailsweep events review` | Walk through each candidate (shows location, inbox, recipient address): **y**=add to Calendar, **n**=dismiss, **s**=skip for now, **t**=move the source email to Trash, **a**=move it to Archive instead, **p**=protect sender (never suggest events from them again, dismisses their other pending candidates too, then offers to trash/archive their scanned messages), **q**=quit. `t`/`a` re-prompt so you can still add/dismiss/skip afterward. Use `p` for senders whose "events" are really just notifications you don't want to track individually (e.g. package-delivery updates) — it stops future false-positive event suggestions from that address. |
| `mailsweep stats --days 30` | Category breakdown + noisiest senders leaderboard. Add `--review` to interactively bulk-move a noisy sender's scanned messages (per-sender **t**=trash all **a**=archive all **n**=skip **q**=quit). |
| `mailsweep dupes --days 30 --window-hours 72` | Likely cross-account duplicates (e.g. from an old forwarding rule) — same sender+subject landing in two different accounts within the time window. Read-only. |
| `mailsweep spam scan` | Audit each account's Junk/Spam folder for mail that shouldn't be there right now, instead of waiting for the daily run. |
| `mailsweep spam list` / `spam review` | List, or walk through, spam false-positive candidates: **y**=rescue to Inbox (this one), **a**=rescue and always trust this sender going forward, **n**=leave in spam, **p**=protect (confirm it's really junk, stop flagging), **q**=quit. |
| `mailsweep spam trust <email>` | Mark a sender as always-rescue without waiting for a candidate to review — retroactively rescues anything already flagged from them, too. |
| `mailsweep purchases scan` | Sweep order-confirmation/shipped/delivered/return emails (Inbox+Trash, last 60 days by default — `--lookback N` to override), group by order, resolve item names via the LLM, and draft a short review per item that looks delivered. Read-only against your mailboxes; only writes to MailSweep's own database. |
| `mailsweep purchases list --status ready` | Print drafted reviews awaiting your curation (or `--status pending_receipt` / `approved` / `returned_excluded` / `name_unresolvable` / `skipped` to see other buckets). |
| `mailsweep purchases review` | Walk through each drafted review: **y**=approve for export, **e**=edit the text then approve, **n**=skip (won't export), **r**=not actually received yet, **x**=return/exclude, **q**=quit. |
| `mailsweep purchases export` | Write a dated CSV (`purchase_reviews_YYYY-MM-DD.csv`, same location as the digest) of everything you've approved: `vendor,item,order_ref,review_text,status`. |

## Where things live

| What | Path |
|---|---|
| Config | `~/.config/mailsweep/config.toml` |
| Digest (HTML) | `~/MailSweep/digest-latest.html` (also dated copies) |
| Database | `~/.local/share/mailsweep/mailsweep.db` (SQLite — delete to start fresh) |
| Project / source | `~/Library/Dev/MailSweep` (example repo) |
| Installed CLI | `~/.local/bin/mailsweep` → pipx venv |

## Current config (`config.toml`)

- **Accounts**: all enabled Mail.app accounts (empty list = everything)
- **Lookback**: 7 days per run, max 60 messages/account
- **LLM**: Ollama `llama3.2:3b` at `localhost:11434`, rules-only fallback if it's down
- **Unsubscribe mode**: `one_click` (RFC 8058 POST where supported, opens browser otherwise)
- **Protected senders**: none set yet — add substrings (e.g. `"@youruni.edu"`) to `[unsubscribe].protected` in the config to never suggest them
- **Target calendar**: `Calendar`

## If something breaks

- **"AppleEvent timed out" / "Mail bridge error"**: make sure Mail.app is open
  and has synced recently. (We already fixed a `whose()`-predicate bug that
  caused this on large mailboxes — if it recurs on a *new* huge mailbox, the
  fix pattern is in `mailsweep/jxa/fetch_mail.js`.)
- **Ollama not reachable**: `brew services list` / `ollama serve` — scan
  degrades to rules-only automatically, nothing breaks, just less nuanced.
- **After editing code**: reinstall with `pipx install --force .` from the
  project folder for the CLI to pick up changes.
- **Run tests**: `python3 -m pytest tests/` (pipx venv needs `pytest`
  injected once: `pipx inject mailsweep pytest`).

## Safety model (why nothing bulk-deletes)

- Read-only against your mailboxes — no moves/deletes/sends outside `review`.
- Unsubscribe only fires after your explicit per-sender `y` in `unsub review`.
- Calendar events only get created after your explicit per-event `y` in `events review`.
- `[unsubscribe].protected` senders are never even suggested (config file, edited by hand).
- Event-source senders you `p`-protect in `events review` are never suggested
  again either — that list lives in the database (not the config file), since
  it's meant to be built up interactively as you hit false positives.
- Trashing/archiving (`t`/`a` in `unsub review` / `events review`, `t`/`a` in
  `stats --review`) only ever *moves* the message (to Trash or to Archive —
  Mail.app resolves the actual folder name per account, e.g. "All Mail" for
  Gmail), never permanently deletes — same as dragging a message in Mail.app.
  It only fires on your explicit per-item confirmation. Messages scanned
  before this feature existed (or older than the retained scan history)
  don't have a Mail.app id on file and can't be moved — you'll see a message
  saying so.
- `purchases scan` only *reads* your mailboxes (including Trash, to catch
  delivery/return emails that landed there) — it never moves or deletes
  anything. The only file it writes outside the database is the CSV from
  `purchases export`, and only for rows you explicitly approved in `review`.
