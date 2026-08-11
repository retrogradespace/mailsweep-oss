// MailSweep: bulk-fetch cheap metadata (subject/sender/date/id) across the
// given mailboxes (default Inbox + Trash, since return/refund and delivery
// emails sometimes end up auto-filed or trashed) for every enabled account,
// for the purchase-review sweep ("mailsweep purchases scan"). Purchase-shaped
// filtering happens in Python; this is just the cheap discovery pass -- no
// whose() predicate (see fetch_mail.js for why that hangs on large mailboxes).
// Run: osascript -l JavaScript fetch_purchase_candidates.js '{"lookbackDays":60,"mailboxNames":["INBOX","Trash"],"accounts":[]}'
"use strict";

// Mailbox naming varies by account type (see move_by_id.js for the same
// issue with INBOX/Trash) -- try the requested name, then common variants.
function resolveMailbox(acct, requestedName) {
  const candidates = [requestedName];
  const upper = requestedName.toUpperCase();
  if (upper === "INBOX") candidates.push("INBOX", "Inbox");
  if (upper === "TRASH") candidates.push("Trash", "Deleted Items", "Deleted Messages", "Bin");
  for (let i = 0; i < candidates.length; i++) {
    try {
      const mbx = acct.mailboxes.byName(candidates[i]);
      mbx.name();
      return mbx;
    } catch (e) { continue; }
  }
  return null;
}

function run(argv) {
  const opts = JSON.parse(argv[0] || "{}");
  const lookbackDays = opts.lookbackDays || 60;
  const mailboxNames = (opts.mailboxNames && opts.mailboxNames.length) ? opts.mailboxNames : ["INBOX", "Trash"];
  const accountFilter = (opts.accounts || []).map(function (a) { return a.toLowerCase(); });
  const cutoff = new Date(Date.now() - lookbackDays * 86400 * 1000);

  const Mail = Application("Mail");
  const out = [];

  let accounts;
  try { accounts = Mail.accounts(); }
  catch (e) { return JSON.stringify({ error: "Cannot read accounts: " + e.message }); }

  for (let a = 0; a < accounts.length; a++) {
    let acctName = "";
    try {
      if (!accounts[a].enabled()) continue;
      acctName = accounts[a].name();
    } catch (e) { continue; }
    if (accountFilter.length && accountFilter.indexOf(acctName.toLowerCase()) === -1) continue;

    for (let mn = 0; mn < mailboxNames.length; mn++) {
      const mbx = resolveMailbox(accounts[a], mailboxNames[mn]);
      if (!mbx) continue;

      let ids, subjects, senders, dates;
      try {
        ids = mbx.messages.id();
        subjects = mbx.messages.subject();
        senders = mbx.messages.sender();
        dates = mbx.messages.dateReceived();
      } catch (e) { continue; }
      if (!ids || ids.length === 0) continue;

      for (let i = 0; i < ids.length; i++) {
        if (dates[i] && dates[i] > cutoff) {
          out.push({
            account: acctName,
            mailbox: mailboxNames[mn],
            mail_id: ids[i],
            subject: String(subjects[i] || ""),
            sender: String(senders[i] || ""),
            date_received: dates[i].toISOString()
          });
        }
      }
    }
  }
  return JSON.stringify(out);
}
