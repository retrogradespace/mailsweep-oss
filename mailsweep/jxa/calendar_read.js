// MailSweep: list upcoming events from Calendar.app.
// Run:  osascript -l JavaScript calendar_read.js '{"horizonDays":60,"calendars":[]}'
"use strict";

function run(argv) {
  const opts = JSON.parse(argv[0] || "{}");
  const horizonDays = opts.horizonDays || 60;
  const calFilter = (opts.calendars || []).map(function (c) { return c.toLowerCase(); });

  const now = new Date();
  const horizon = new Date(now.getTime() + horizonDays * 86400 * 1000);

  const Calendar = Application("Calendar");
  const out = [];

  let cals;
  try { cals = Calendar.calendars(); }
  catch (e) { return JSON.stringify({ error: "Cannot read calendars: " + e.message }); }

  for (let c = 0; c < cals.length; c++) {
    let calName = "";
    try { calName = cals[c].name(); } catch (e) { continue; }
    if (calFilter.length && calFilter.indexOf(calName.toLowerCase()) === -1) continue;

    let evts;
    try {
      evts = cals[c].events.whose({
        _and: [
          { startDate: { _greaterThan: now } },
          { startDate: { _lessThan: horizon } }
        ]
      });
      if (evts.length === 0) continue;
    } catch (e) { continue; }

    let summaries, starts, ends, locations;
    try {
      summaries = evts.summary();
      starts = evts.startDate();
      ends = evts.endDate();
      locations = evts.location();
    } catch (e) { continue; }

    for (let i = 0; i < summaries.length; i++) {
      out.push({
        calendar: calName,
        title: String(summaries[i] || ""),
        start: starts[i] ? starts[i].toISOString() : "",
        end: ends[i] ? ends[i].toISOString() : "",
        location: String(locations[i] || "")
      });
    }
  }
  return JSON.stringify(out);
}
