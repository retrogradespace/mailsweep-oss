# Results

Aggregate, de-identified numbers from running MailSweep against one real,
long-neglected multi-account inbox — 7 personal and work accounts, years of
accumulated marketing mail, an old auto-forwarding rule, and (as it turned
out) real financial alerts buried in spam. No sender names, account names,
or message content below — just what moved.

## Backlog cleanup (one-time, marketing mail)

| Metric | Count |
|---|---|
| Accounts swept | 7 |
| Inbox messages surveyed | ~56,000 |
| Marketing/newsletter messages cleared | 28,100+ |
| Share of total inbox volume | ~50% |

Cleared by List-Unsubscribe / mailing-list header signals, moved to Trash
(recoverable), not permanently deleted.

## Ongoing triage (the standing tool)

| Metric | Count |
|---|---|
| Unsubscribes executed | 82 |
| Senders explicitly protected (never suggested again) | 17 |
| Calendar events surfaced from buried mail and approved | 21 |
| Event candidates reviewed and dismissed (false positives + already-past) | 106 |
| Likely cross-account duplicate groups flagged | 5 |

## Spam-folder audit

| Pass | Scanned | Flagged as possible false positive | Rescued to Inbox | Correctly left (confirmed phishing) |
|---|---|---|---|---|
| Manual, one account | 436 | 62 | 41 | 2 |
| First automated run | 245 | 5 | 4 | 1 |
| **Total** | **681** | **67** | **45** | **3** |

Every rescue and every "leave it" decision went through a person, one
message at a time — the tool only ever flags, never moves on its own.

## What broke, and got fixed, along the way

| Issue | Impact | Fix |
|---|---|---|
| `whose()` date predicate on Mail.app | Silently hung the daily scan for days, no error | Bulk-fetch + filter in-process instead |
| Mailbox name lookup does substring matching | Wrong mailbox recorded/resolved on Exchange-style accounts | Always read back the resolved name, never trust the search string |

## Net effect

- **~28,200 messages** moved out of active inboxes
- **45 legitimate messages** rescued from spam, including alerts that would
  have stayed buried
- **82 unsubscribes** executed with zero manual clicking
- **21 real events** recovered onto a calendar that would otherwise have
  been missed
- The daily automation now runs unattended — no more manual sweeps for
  either marketing mail or spam false positives
