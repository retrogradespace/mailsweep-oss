// MailSweep: create an event in Calendar.app.
// Run:  osascript -l JavaScript calendar_create.js '{"calendar":"Home","title":"...","start":"ISO","end":"ISO","location":"","allDay":false}'
"use strict";

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

  const start = new Date(opts.start);
  const end = opts.end ? new Date(opts.end) : new Date(start.getTime() + 3600 * 1000);

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
