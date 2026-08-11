// MailSweep: pull recent Junk/Spam-folder messages from Apple Mail, for the
// spam false-positive audit. Tries a list of common junk mailbox names per
// account since it varies by provider (Gmail/Proton use "Spam" or "Junk",
// Exchange uses "Junk Email").
// Run: osascript -l JavaScript fetch_junk.js '{"lookbackDays":30,"maxMessages":500,"accounts":[],"knownMessageIds":[]}'
"use strict";

const JUNK_NAMES = ["Junk", "Junk Email", "Spam"];

function run(argv) {
  const opts = JSON.parse(argv[0] || "{}");
  const lookbackDays = opts.lookbackDays || 30;
  const maxMessages = opts.maxMessages || 500;
  const accountFilter = (opts.accounts || []).map(function (a) { return a.toLowerCase(); });
  const known = {};
  (opts.knownMessageIds || []).forEach(function (m) { known[m] = true; });
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

    let mbx = null, mbName = "";
    for (let j = 0; j < JUNK_NAMES.length; j++) {
      try {
        const candidate = accounts[a].mailboxes.byName(JUNK_NAMES[j]);
        // byName() does a fuzzy/substring match (e.g. "Junk" resolves to
        // "Junk Email"), so record the REAL resolved name, not the search
        // term -- callers need the actual name to look messages up later.
        const resolvedName = candidate.name();
        mbx = candidate;
        mbName = resolvedName;
        break;
      } catch (e) { continue; }
    }
    if (!mbx) continue;

    // Bulk property pulls across the whole mailbox -- no whose() predicate
    // (see fetch_mail.js for why: it hangs/times out on large mailboxes).
    let ids, messageIds, subjects, senders, dates;
    try {
      ids = mbx.messages.id();
      messageIds = mbx.messages.messageId();
      subjects = mbx.messages.subject();
      senders = mbx.messages.sender();
      dates = mbx.messages.dateReceived();
    } catch (e) { continue; }
    if (!ids || ids.length === 0) continue;

    const order = [];
    for (let i = 0; i < ids.length; i++) {
      if (dates[i] && dates[i] > cutoff) order.push(i);
    }
    order.sort(function (x, y) { return dates[y] - dates[x]; });

    let kept = 0;
    for (let k = 0; k < order.length && kept < maxMessages; k++) {
      const i = order[k];
      const mid = String(messageIds[i] || ("mail-id-" + ids[i]));
      if (known[mid]) continue;

      out.push({
        message_id: mid,
        account: acctName,
        mailbox: mbName,
        mail_id: ids[i],
        sender: String(senders[i] || ""),
        subject: String(subjects[i] || ""),
        date_received: dates[i] ? dates[i].toISOString() : ""
      });
      kept++;
    }
  }
  return JSON.stringify(out);
}
