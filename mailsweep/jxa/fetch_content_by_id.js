// Fetch full content for a specific list of {account, mailbox, id} triples
// (from an earlier bulk metadata pass). Used by the purchase-review sweep to
// pull actual item names out of order-confirmation emails whose subject
// alone is generic.
// Run: osascript -l JavaScript fetch_content_by_id.js '{"targets":[{"account":"X","mailbox":"INBOX","id":123}]}'
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
    let content = "", subject = "";
    try {
      const mbx = Mail.accounts.byName(acctName).mailboxes.byName(mbName);
      const msg = mbx.messages.byId(id);
      subject = String(msg.subject() || "");
      content = String(msg.content() || "").slice(0, 3000);
    } catch (e) {
      content = "ERROR: " + e.message;
    }
    out.push({ account: acctName, mailbox: mbName, id: id, subject: subject, content: content });
  }
  return JSON.stringify(out);
}
