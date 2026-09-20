// MailSweep: list upcoming events from Calendar.app.
// Run:  osascript -l JavaScript calendar_read.js '{"horizonDays":60,"calendars":[]}'
"use strict";

// Local wall-clock "YYYY-MM-DDTHH:MM", matching how candidate events are stored.
// toISOString() is UTC, which puts any event after ~8pm Eastern on the next day
// and made the "already on your Calendar" date match miss evening events.
function localIso(d) {
  if (!d) return "";
  const p = function (n) { return (n < 10 ? "0" : "") + n; };
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
    "T" + p(d.getHours()) + ":" + p(d.getMinutes());
}

function run(argv) {
  const opts = JSON.parse(argv[0] || "{}");
  const horizonDays = opts.horizonDays || 60;
  const todayOnly = !!opts.todayOnly;
  const calFilter = (opts.calendars || []).map(function (c) { return c.toLowerCase(); });

  const now = new Date();
  // todayOnly: local midnight-to-midnight, so a briefing run mid-morning
  // still shows meetings earlier that day instead of only "from now on".
  const lower = todayOnly
    ? new Date(now.getFullYear(), now.getMonth(), now.getDate())
    : now;
  const horizon = todayOnly
    ? new Date(lower.getTime() + 86400 * 1000)
    : new Date(now.getTime() + horizonDays * 86400 * 1000);

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
          { startDate: { _greaterThan: lower } },
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
        start: localIso(starts[i]),
        end: localIso(ends[i]),
        location: String(locations[i] || "")
      });
    }
  }
  return JSON.stringify(out);
}
