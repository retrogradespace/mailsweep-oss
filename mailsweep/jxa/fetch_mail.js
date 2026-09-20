// MailSweep: pull recent inbox messages from Apple Mail.
// Run:  osascript -l JavaScript fetch_mail.js '{"lookbackDays":7,"maxMessages":400,"accounts":[],"knownMessageIds":[]}'
// Emits one JSON object (array of messages) on stdout.
"use strict";

function run(argv) {
  const opts = JSON.parse(argv[0] || "{}");
  const lookbackDays = opts.lookbackDays || 7;
  const maxMessages = opts.maxMessages || 400;
  const accountFilter = (opts.accounts || []).map(function (a) { return a.toLowerCase(); });
  const known = {};
  (opts.knownMessageIds || []).forEach(function (m) { known[m] = true; });
  const cutoff = new Date(Date.now() - lookbackDays * 86400 * 1000);

  const Mail = Application("Mail");
  const out = [];

  let inboxes;
  try {
    inboxes = Mail.inbox.mailboxes();
  } catch (e) {
    return JSON.stringify({ error: "Cannot read Mail inbox: " + e.message });
  }

  for (let b = 0; b < inboxes.length; b++) {
    const mbx = inboxes[b];
    let acctName = "";
    try { acctName = mbx.account.name(); } catch (e) { continue; }
    if (accountFilter.length && accountFilter.indexOf(acctName.toLowerCase()) === -1) continue;

    // Bulk property pulls across the WHOLE mailbox (cheap, one Apple Event
    // per property, no predicate evaluation). `.whose({dateReceived: ...})`
    // was tried here previously and is catastrophically slow / times out on
    // large mailboxes -- Mail's predicate evaluation over thousands of
    // messages can hang for many minutes. Filtering in JS afterward avoids
    // that entirely and costs about the same as fetching the plain arrays.
    let ids, messageIds, subjects, senders, dates;
    try {
      ids = mbx.messages.id();
      messageIds = mbx.messages.messageId();
      subjects = mbx.messages.subject();
      senders = mbx.messages.sender();
      dates = mbx.messages.dateReceived();
    } catch (e) { continue; }
    if (!ids || ids.length === 0) continue;

    // Newest first, skip already-processed, cap per mailbox. Cutoff filtering
    // happens here (in JS) rather than via a Mail.app predicate.
    const order = [];
    for (let i = 0; i < ids.length; i++) {
      if (dates[i] && dates[i] > cutoff) order.push(i);
    }
    order.sort(function (a, b2) { return dates[b2] - dates[a]; });

    let kept = 0;
    for (let k = 0; k < order.length && kept < maxMessages; k++) {
      const i = order[k];
      const mid = String(messageIds[i] || ("mail-id-" + ids[i]));
      if (known[mid]) continue;

      let headerBlock = "";
      let content = "";
      let repliedStatus = false;
      try {
        const msg = mbx.messages.byId(ids[i]);
        headerBlock = String(msg.allHeaders() || "").slice(0, 8000);
        content = String(msg.content() || "").slice(0, 2000);
        repliedStatus = !!msg.wasRepliedTo();
      } catch (e) { /* keep going with what we have */ }

      out.push({
        message_id: mid,
        account: acctName,
        mailbox: "INBOX",
        mail_id: ids[i],
        sender: String(senders[i] || ""),
        subject: String(subjects[i] || ""),
        date_received: dates[i] ? dates[i].toISOString() : "",
        header_block: headerBlock,
        snippet: content,
        replied_status: repliedStatus
      });
      kept++;
    }
  }
  return JSON.stringify(out);
}
