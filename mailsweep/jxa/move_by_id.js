// Move a specific list of {account, mailbox, id} messages to a target
// mailbox in the SAME account. Used for rescuing false-positive spam,
// trashing/archiving messages from unsub/events/stats review, and similar
// corrective moves.
// Run: osascript -l JavaScript move_by_id.js '{"targets":[{"account":"X","mailbox":"Spam","id":123}],"destMailbox":"INBOX"}'
"use strict";

// Special-mailbox naming varies by account type (Gmail/Proton use "INBOX",
// Exchange-style accounts use "Inbox"/"Deleted Items"), and byName() does a
// case-sensitive substring match with no fallback -- so a literal "INBOX"
// or "Trash" silently fails to resolve on some accounts. Try the requested
// name first, then common provider-specific variants, rather than assuming
// one spelling.
function resolveMailbox(acct, requestedName) {
  const candidates = [requestedName];
  const upper = requestedName.toUpperCase();
  if (upper === "INBOX") candidates.push("INBOX", "Inbox");
  if (upper === "TRASH") candidates.push("Trash", "Deleted Items", "Deleted Messages", "Bin");
  if (upper === "ARCHIVE") candidates.push("Archive", "All Mail", "Archived");
  if (upper === "JUNK") candidates.push("Junk", "Junk Email", "Spam");
  for (let i = 0; i < candidates.length; i++) {
    try {
      const mbx = acct.mailboxes.byName(candidates[i]);
      mbx.name(); // force resolution
      return mbx;
    } catch (e) { continue; }
  }
  throw new Error("no mailbox matched any of: " + candidates.join(", "));
}

function run(argv) {
  const opts = JSON.parse(argv[0] || "{}");
  const targets = opts.targets || [];
  const destMailbox = opts.destMailbox;
  const Mail = Application("Mail");
  let moved = 0;
  const errors = [];

  for (let t = 0; t < targets.length; t++) {
    const acctName = targets[t].account;
    const mbName = targets[t].mailbox;
    const id = targets[t].id;
    try {
      const acct = Mail.accounts.byName(acctName);
      const mbx = resolveMailbox(acct, mbName);
      const dest = resolveMailbox(acct, destMailbox);
      const msg = mbx.messages.byId(id);
      Mail.move(msg, { to: dest });
      moved++;
    } catch (e) {
      errors.push(acctName + "/" + mbName + "/" + id + ": " + e.message);
    }
  }
  return JSON.stringify({ moved: moved, total: targets.length, errors: errors });
}
