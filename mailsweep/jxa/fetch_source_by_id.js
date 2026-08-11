// Fetch raw message source (not rendered content) for a specific list of
// {account, mailbox, id} triples. Some senders (e.g. Amazon) put full item
// names in the raw HTML that the plain-text content() rendering drops, so
// the purchase-review sweep uses this as a fallback extraction path.
// Run: osascript -l JavaScript fetch_source_by_id.js '{"targets":[{"account":"X","mailbox":"INBOX","id":123}]}'
"use strict";

function run(argv) {
  const opts = JSON.parse(argv[0] || "{}");
  const targets = opts.targets || [];
  const Mail = Application("Mail");
  const out = [];

  for (let t = 0; t < targets.length; t++) {
    const acctName = targets[t].account;
    const mbName = targets[t].mailbox;
    const id = targets[t].id;
    let src = "";
    try {
      const mbx = Mail.accounts.byName(acctName).mailboxes.byName(mbName);
      const msg = mbx.messages.byId(id);
      src = String(msg.source() || "");
    } catch (e) {
      src = "ERROR: " + e.message;
    }
    out.push({ account: acctName, mailbox: mbName, id: id, source: src });
  }
  return JSON.stringify(out);
}
