# MailSweep

Local, private mail triage for Apple Mail. Scans your inboxes, uses a small
local LLM (via [Ollama](https://ollama.com)) plus cheap header heuristics to:

- surface **events buried in email** (invitations, appointments, deadlines)
  and cross-check them against Calendar.app so it never nags about things
  you already scheduled — with one-key approval to add the rest;
- build a ranked **unsubscribe queue** from real mailing-list signals
  (`List-Unsubscribe` headers, sender frequency) — nothing is ever sent
  without your approval, and RFC 8058 one-click unsubscribe is used where
  supported;
- **audit your Junk/Spam folder** for receipt- and security-shaped mail that
  shouldn't be there, with a phishing hint for spoofed senders — queued for
  your review, never moved on its own;
- flag **likely cross-account duplicates** (handy after an old forwarding
  rule leaves copies of the same mail in two mailboxes);
- sweep order-confirmation/shipping/delivery mail into a **purchase-review
  queue**, drafting a short review per item so a backlog of "leave a review"
  emails turns into a five-minute approval pass instead of a chore;
- write a **daily HTML digest**: what needs attention, pending events,
  unsubscribe candidates, spam false positives, and a noise leaderboard of
  your loudest senders.

Everything runs on your Mac. No mail content leaves the machine — the only
network calls are to `localhost` (Ollama) and, when *you* approve one, the
unsubscribe endpoint itself.

See [de-identified results from real-world use](https://retrogradespace.github.io/mailsweep-oss/)
(also in [RESULTS.md](RESULTS.md)) — aggregate stats from running MailSweep
against a long-neglected, multi-account inbox.

## How it reads your mail

Through Apple Mail via AppleScript/JXA, so every account already set up in
Mail.app is covered automatically — iCloud, Gmail, Exchange, and **Proton**.

> **Proton note:** Proton Mail doesn't speak IMAP directly. Install
> [Proton Mail Bridge](https://proton.me/mail/bridge) (requires a paid Proton
> plan), add the account it exposes to Mail.app, and MailSweep sees it like
> any other account. No extra MailSweep configuration needed.

## Install

```bash
# 1. The tool (from this directory)
python3 -m pip install --user .        # or use a venv/pipx

# 2. The local model
brew install ollama                     # or download from ollama.com
ollama pull llama3.2:3b                 # ~2 GB; qwen2.5:3b also works well

# 3. First-time setup
mailsweep init                          # writes ~/.config/mailsweep/config.toml
```

Edit the config: put your Mail.app account names in `[mail].accounts`
(or leave empty for all), and add senders that must never be suggested for
unsubscribe to `[unsubscribe].protected`.

## First run

```bash
mailsweep scan --lookback 30            # seed sender stats from the last month
```

macOS will ask "Terminal would like to control Mail / Calendar" — allow both
(System Settings › Privacy & Security › Automation if you need to fix it
later). The first scan is the slowest; daily runs only process unseen mail.

## Daily use

```bash
mailsweep scan              # scan + refresh the digest (usually via launchd)
mailsweep events review     # y/n/skip/trash/archive/protect each detected event
mailsweep unsub review      # y/n/protect/trash/archive each unsubscribe candidate
mailsweep spam review       # rescue/leave/protect each spam false-positive
mailsweep spam trust <addr> # always auto-rescue this sender's Junk mail
mailsweep dupes             # likely cross-account duplicates (read-only)
mailsweep purchases scan    # sweep order mail, draft reviews for delivered items
mailsweep purchases review  # approve/edit/skip each drafted review
mailsweep purchases export  # write a CSV of everything you approved
mailsweep stats             # noise leaderboard (--review to bulk-trash/archive)
mailsweep digest --open     # re-render and open the HTML digest
```

The digest lands in `~/MailSweep/digest-latest.html`.

## Schedule the morning digest

```bash
cp launchd/com.mailsweep.daily.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.mailsweep.daily.plist
```

Edit the plist first if `mailsweep` isn't on the default `PATH` (point
`ProgramArguments` at your venv's `bin/mailsweep`). Runs at 7:30 daily;
log at `/tmp/mailsweep.log`.

## How classification works

1. **Rules first** (free, instant): `List-Unsubscribe` / `List-ID` /
   `Precedence` headers, no-reply sender patterns, receipt/security subjects.
   These settle most newsletter/marketing/notification traffic.
2. **LLM for the residue**: anything the rules can't place — plus anything
   that *smells* like an event — goes to the local Ollama model, which
   returns category, importance, and a structured event candidate
   (title/start/location/confidence).
3. If Ollama is down, MailSweep degrades gracefully to rules-only and says so.

## Safety properties

- Read-only against your mailboxes by default: scanning and classification
  never move, delete, or send mail on their own.
- Unsubscribes fire only after explicit per-sender approval (`unsub review`).
- Calendar writes only happen from `events review` after you approve.
- Spam rescues only happen from `spam review` after you approve, per message;
  `protect` marks a sender as correctly-spam so it stops getting re-flagged.
- `[unsubscribe].protected` senders are never even suggested.
- Event-source senders you `p`-protect in `events review` are never
  suggested again either — built up interactively as you hit false
  positives, so recurring notification senders stop generating candidates.
- Trashing/archiving (`t`/`a` in `unsub review` / `events review`, or in
  `stats --review`) only ever *moves* the message, never permanently
  deletes — same as dragging a message in Mail.app — and only fires on your
  explicit per-item confirmation.
- `purchases scan` only *reads* your mailboxes (including Trash, to catch
  delivery/return emails that landed there) — it never moves or deletes
  anything. The only file it writes outside the database is the CSV from
  `purchases export`, and only for rows you explicitly approved in `review`.
- State lives in `~/.local/share/mailsweep/mailsweep.db` (SQLite); delete it
  to start fresh.

## Project layout

```
mailsweep/
  cli.py        subcommands (init/scan/digest/unsub/events/spam/dupes/purchases/stats)
  bridge.py     osascript runner + header parsing
  jxa/          JXA scripts for Mail.app and Calendar.app
  rules.py      header/subject heuristics, spam-audit + phishing-hint rules,
                purchase-email classification
  classify.py   Ollama prompt + response parsing, hybrid pipeline, purchase
                item/review extraction
  store.py      SQLite schema and queries
  unsub.py      RFC 8058 one-click + browser fallback
  digest.py     HTML + terminal rendering
launchd/        LaunchAgent template for the daily run
tests/          unit tests (pure-Python parts; run: python3 -m pytest)
```

## Field-testing notes / known limits

- Mail.app must be running (or at least synced recently) for fresh mail.
- Very large inboxes: the first scan caps at `max_messages` per account;
  raise `lookback_days` gradually instead of all at once.
- The 3B model occasionally mis-dates events — that's why event creation is
  approval-gated and shows its confidence. If accuracy disappoints, try
  `qwen2.5:7b` (slower, noticeably sharper).
- Junk already caught by Mail's own filter never reaches the inbox, so it
  won't appear here; MailSweep targets the *legitimate* noise that does.
- **Never filter Mail.app with `whose()` on a date predicate.** It's the
  obvious way to ask for "messages since X," and it can hang for minutes on
  a large mailbox with no error — a scheduled job just silently stops
  producing output. Pull cheap bulk properties (id/date/sender/subject) for
  every message in one call instead (fast, even at tens of thousands of
  messages) and filter in Python.
- **Mail.app's mailbox lookup does a case-sensitive substring match, not an
  exact one, and fails silently on a near-miss.** `mailboxes.byName("Junk")`
  can silently resolve to `"Junk Email"` on an Exchange account; `"INBOX"`
  fails outright where the real name is `"Inbox"`. Always read back the
  resolved name from the mailbox object rather than trusting the string you
  searched for, and expect the Inbox/Junk mailbox name to vary by account
  type (Gmail/Proton vs. Exchange).

## License

MIT — see [LICENSE](LICENSE).

