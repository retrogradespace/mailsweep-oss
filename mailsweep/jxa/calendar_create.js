// MailSweep: create an event in Calendar.app.
// Run:  osascript -l JavaScript calendar_create.js '{"calendar":"Home","title":"...","start":"ISO","end":"ISO","location":"","allDay":false}'
"use strict";

// "YYYY-MM-DD[THH:MM]" is local wall-clock time. new Date("YYYY-MM-DD") parses as
// UTC (the evening before, in US timezones) and a Z/+00:00 suffix shifts by the
// offset, so build the Date from its parts instead of handing JS the string.
function parseLocal(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?/.exec(String(s || ""));
  if (!m) return null;
  return new Date(+m[1], +m[2] - 1, +m[3], +(m[4] || 0), +(m[5] || 0));
}

function run(argv) {
  const opts = JSON.parse(argv[0] || "{}");
  if (!opts.title || !opts.start) {
    return JSON.stringify({ error: "title and start are required" });
  }

  const Calendar = Application("Calendar");
  let target = null;
  try {
    const cals = Calendar.calendars();
    for (let i = 0; i < cals.length; i++) {
      if (cals[i].name() === opts.calendar) { target = cals[i]; break; }
    }
  } catch (e) {
    return JSON.stringify({ error: "Cannot read calendars: " + e.message });
  }
  if (!target) return JSON.stringify({ error: "Calendar not found: " + opts.calendar });

  const start = parseLocal(opts.start);
  if (!start) return JSON.stringify({ error: "Unreadable start time: " + opts.start });
  const end = (opts.end && parseLocal(opts.end)) || new Date(start.getTime() + 3600 * 1000);

  try {
    const evt = Calendar.Event({
      summary: opts.title,
      startDate: start,
      endDate: end,
      location: opts.location || "",
      alldayEvent: !!opts.allDay
    });
    target.events.push(evt);
    return JSON.stringify({ ok: true });
  } catch (e) {
    return JSON.stringify({ error: "Create failed: " + e.message });
  }
}
