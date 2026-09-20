'use strict';
/* MailSweep UI. Vanilla JS, no build step. Everything derived from mail
   (senders, subjects, ...) goes into the page via textContent, never innerHTML. */
(() => {
const TOKEN = document.querySelector('meta[name="mailsweep-token"]').content;
const ROUTES = [
  { id: 'digest', label: 'Digest', icon: 'digest' },
  { id: 'unsub', label: 'Unsubscribe', icon: 'mail', badge: 'unsub_todo' },
  { id: 'events', label: 'Events', icon: 'cal', badge: 'events' },
  { id: 'spam', label: 'Spam audit', icon: 'shield', badge: 'spam' },
  { id: 'leaderboard', label: 'Leaderboard', icon: 'speaker' },
  { id: 'reviews', label: 'Reviews', icon: 'star', pro: true },
];
const PAGE = 50;
const S = { route: 'digest', summary: null, data: null, err: null, log: [], busy: false, seq: 0, ui: null };
const freshUi = () => ({ shown: PAGE, shownFailed: PAGE, shownAwaiting: PAGE, shownRepeat: PAGE, sel: new Set(), card: null, past: false, days: 30, status: 'ready', drafts: {} });

// ---------------------------------------------------------------- helpers
const $ = (sel) => document.querySelector(sel);
/* replaceChildren() takes flat nodes only: flatten nested arrays and drop false/null from `cond && node`. */
const nodes = (list) => [list].flat(Infinity).filter((n) => n && n.nodeType);
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'checked' || k === 'disabled' || k === 'value') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
function icon(name) {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('class', 'icon'); svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(NS, 'use');
  use.setAttribute('href', '#i-' + name);
  svg.append(use);
  return svg;
}
const nameOf = (raw) => {
  const m = /^\s*"?([^"<]*?)"?\s*<[^>]*>\s*$/.exec(raw || '');
  return (m && m[1].trim()) || (raw || '').replace(/[<>]/g, '').trim() || '(unknown)';
};
const date10 = (s) => (s || '').slice(0, 10) || '?';
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
function fmtWhen(start, allDay) {
  if (!start) return '(no time)';
  const day = { weekday: 'short', month: 'short', day: 'numeric' };
  if (start.length <= 10 || allDay) {
    const [y, m, d] = start.slice(0, 10).split('-').map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, day) + ' · all day';
  }
  const dt = new Date(start);
  if (isNaN(dt)) return start;
  return dt.toLocaleDateString(undefined, day) + ' · ' + dt.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
}
function fmtScan(iso) {
  if (!iso) return 'never';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const t = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false });
  return d.toDateString() === new Date().toDateString() ? t : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ' ' + t;
}

async function api(path, body) {
  const opts = { headers: { 'X-MailSweep-Token': TOKEN } };
  if (body !== undefined) {
    opts.method = 'POST';
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON error page */ }
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}

// ---------------------------------------------------------------- console + dialog
function say(kind, text) { S.log.push({ kind, text }); if (S.log.length > 80) S.log.shift(); }
function consoleBox() {
  const lines = S.log.length
    ? S.log.map((l) => h('div', { class: l.kind }, l.text))
    : [h('div', { class: 'idle' }, '$ mailsweep ui   # what you approve here is echoed below')];
  return h('div', { class: 'console', role: 'log', 'aria-live': 'polite', 'aria-label': 'Activity' }, lines);
}
function ask({ title, lines = [], confirm = 'Confirm', danger = false, checkbox, select }) {
  return new Promise((resolve) => {
    const dlg = $('#dlg');
    let cb, sel;
    dlg.returnValue = '';
    dlg.replaceChildren(...nodes([
      h('h3', null, title),
      lines.map((l) => h('p', null, l)),
      checkbox && h('label', null, cb = h('input', { type: 'checkbox', checked: !!checkbox.checked }), checkbox.label),
      select && h('label', null, select.label + ' ', sel = h('select', { class: 'sel' },
        select.options.map(([v, t]) => h('option', { value: v }, t)))),
      h('div', { class: 'btns' },
        h('button', { class: 'btn', onclick: () => dlg.close('no') }, 'Cancel'),
        h('button', { class: 'btn ' + (danger ? 'danger' : 'primary'), onclick: () => dlg.close('yes') }, confirm))]));
    dlg.addEventListener('close', () => resolve({
      ok: dlg.returnValue === 'yes', checked: cb ? cb.checked : false, value: sel ? sel.value : null,
    }), { once: true });
    dlg.showModal();
  });
}

// ---------------------------------------------------------------- data flow
const ENDPOINT = {
  digest: () => '/api/digest', unsub: () => '/api/unsub',
  events: () => `/api/events?past=${S.ui.past ? 1 : 0}`, spam: () => '/api/spam',
  leaderboard: () => `/api/leaderboard?days=${S.ui.days}`,
  reviews: () => `/api/reviews?status=${S.ui.status}`,
};
async function load() {
  const seq = ++S.seq;
  try {
    const [summary, data] = await Promise.all([api('/api/summary'), api(ENDPOINT[S.route]())]);
    if (seq !== S.seq) return;           // a newer navigation superseded this one
    S.summary = summary; S.data = data; S.err = null;
  } catch (e) {
    if (seq !== S.seq) return;
    S.err = e.message;
  }
  render();
}
/* POST an action, echo the outcome in the console, refresh. Returns the response or null. */
async function mutate(label, path, body, { showId = false } = {}) {
  if (S.busy) return null;
  S.busy = true; document.body.classList.add('busy');
  say('cmd', '$ ' + label);
  let res = null;
  try {
    res = await api(path, body);
    for (const r of res.results || []) {
      const opened = r.state === 'opened';
      say(opened ? 'warn' : r.ok ? 'ok' : 'bad', `  ${opened ? '↗' : r.ok ? '✓' : '✗'} ${showId ? r.id + ': ' : ''}${r.note}`);
      for (const e of r.errors || []) say('bad', `    ! ${e}`);
    }
  } catch (e) {
    say('bad', `  ✗ ${e.message}`);
  }
  S.busy = false; document.body.classList.remove('busy');
  await load();
  return res;
}

// ---------------------------------------------------------------- layout
function renderSide() {
  const sm = S.summary;
  const items = ROUTES.map((r) => {
    const n = r.badge && sm ? sm[r.badge] : 0;
    return h('a', { class: 'nav', href: '#/' + r.id, 'aria-current': r.id === S.route ? 'page' : null },
      icon(r.icon), r.label,
      n > 0 && h('span', { class: 'pill', 'aria-label': `${n} pending` }, n > 99 ? '99+' : n),
      r.pro && h('span', { class: 'pill pro' }, 'PRO'));
  });
  $('#side').replaceChildren(...nodes([
    h('div', { class: 'brand' }, 'MailSweep'), items,
    h('div', { class: 'side-foot' },
      sm && sm.demo && h('div', { class: 'demo' }, 'DEMO DATA'),
      sm ? 'last scan ' + fmtScan(sm.last_scan) : '',
      sm && h('div', null, sm.llm.up ? 'LLM ' + sm.llm.model : 'LLM off · rules only'))]));
}
function shell(title, cmd, ...kids) {
  return [
    h('div', { class: 'mbrand' }, 'MailSweep'),
    h('header', { class: 'ph' }, h('h1', null, title), cmd && h('code', { class: 'chip' }, '$ ' + cmd)),
    S.summary && S.summary.unsub_mode === 'never' && route('unsub') &&
      h('p', { class: 'notice' }, "Unsubscribe mode is 'never' in your config: approvals are recorded but nothing is sent."),
    ...kids, consoleBox(),
  ];
}
const route = (id) => S.route === id;
function render() {
  const y = window.scrollY;
  renderSide();
  document.title = 'MailSweep · ' + ROUTES.find((r) => r.id === S.route).label;
  const main = $('#main');
  if (S.err) main.replaceChildren(...nodes([h('div', { class: 'mbrand' }, 'MailSweep'),
    h('p', { class: 'error', role: 'alert' }, S.err),
    h('div', { class: 'bar' }, h('button', { class: 'btn', onclick: load }, 'Retry'))]));
  else if (!S.data) main.replaceChildren(h('p', { class: 'empty' }, 'Loading…'));
  else main.replaceChildren(...nodes(PAGES[S.route]()));
  window.scrollTo(0, y);
  const c = main.querySelector('.console');
  if (c) c.scrollTop = c.scrollHeight;
}
function navigate() {
  const id = (location.hash.match(/^#\/(\w+)/) || [])[1];
  S.route = ROUTES.some((r) => r.id === id) ? id : 'digest';
  S.ui = freshUi(); S.data = null; S.err = null;
  render();
  $('#main').focus({ preventScroll: true });
  load();
}

// ---------------------------------------------------------------- shared list bits
function pager(items, renderRow, key = 'shown') {
  const shown = items.slice(0, S.ui[key]);
  const rest = items.length - shown.length;
  return [
    h('div', { class: 'rows' }, shown.map(renderRow)),
    rest > 0 && h('div', { class: 'bar' }, h('button', { class: 'btn small', onclick: () => { S.ui[key] += PAGE; render(); } },
      `Show ${Math.min(PAGE, rest)} more (${rest} remaining)`)),
  ];
}
const empty = (msg) => h('p', { class: 'empty' }, msg);

/* Review-each card mode: one item at a time, same single-key shortcuts as the CLI review flows. */
function startCard(items) { S.ui.card = { queue: items.slice(), i: 0, actions: [] }; render(); }
function cardView({ title, cmd, keyOf, facts, who, actions }) {
  const c = S.ui.card;
  const back = h('button', { class: 'btn small', onclick: () => { S.ui.card = null; render(); } }, '← Back to list');
  if (c.i >= c.queue.length) {
    c.actions = [];
    return shell(title, cmd, h('div', { class: 'card' }, h('div', { class: 'who' }, 'Queue cleared'),
      h('div', { class: 'facts' }, `${plural(c.queue.length, 'item')} reviewed this pass.`), back));
  }
  const item = c.queue[c.i];
  const left = c.queue.length - c.i - 1;
  c.actions = actions.map((a) => ({ ...a, go: async () => {
    if (S.busy) return;
    await a.run(item);
    if (!a.stay) {
      const gone = a.local || !(S.data.items || []).some((x) => keyOf(x) === keyOf(item));
      if (gone) c.i += 1;              // acted on (or already handled elsewhere): next item
    }
    render();
  } }));
  return shell(title, cmd,
    h('div', { class: 'bar' }, back, h('span', { class: 'dim mono' }, `${left} left`)),
    h('div', { class: 'card' },
      h('div', { class: 'who' }, who(item)),
      h('div', { class: 'facts' }, facts(item).filter(Boolean).map((f) => h('div', null, f))),
      h('div', { class: 'actions' }, c.actions.map((a) =>
        h('button', { class: 'btn ' + (a.tone || ''), onclick: a.go }, a.label, h('kbd', null, a.key)))),
      h('div', { class: 'keys' }, 'keys: ' + c.actions.map((a) => a.key).join(' ') + ' — same as the CLI review')));
}
document.addEventListener('keydown', (e) => {
  const c = S.ui && S.ui.card;
  if (!c || e.metaKey || e.ctrlKey || e.altKey || $('#dlg').open) return;
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
  const a = c.actions.find((x) => x.key === e.key.toLowerCase());
  if (a) { e.preventDefault(); a.go(); }
});

// ---------------------------------------------------------------- digest
const MIX_ORDER = ['personal', 'work', 'transactional', 'newsletter', 'marketing', 'notification'];
function digestPage() {
  const d = S.data, sm = d.summary;
  const sec = (id, title, ...kids) => h('section', { class: 'sec' },
    h('h2', null, id ? h('a', { href: '#/' + id }, title) : title), kids);
  const top = d.unsub.top[0];
  return shell('MailSweep digest', 'mailsweep digest',
    h('p', { class: 'meta' }, `last scan ${fmtScan(sm.last_scan)} · ${sm.llm.up ? 'LLM ' + sm.llm.model : 'LLM off, rules only'}`),
    Object.keys(d.mix).length > 0 && h('div', { class: 'chips', 'aria-label': 'Last 7 days by category' },
      Object.entries(d.mix).sort((a, b) => b[1] - a[1]).map(([k, v]) => h('span', { class: 'tag' }, `${k} ${v}`))),
    sec('unsub', `Unsubscribe queue — ${plural(d.unsub.total, 'candidate')}`,
      top ? h('p', null, 'Top offender: ', h('b', null, nameOf(top.sender)), `, ${plural(top.msg_count, 'message')} seen.`) : empty('Queue is empty.'),
      d.unsub.top.length > 1 && h('ul', null, d.unsub.top.slice(1).map((u) =>
        h('li', null, h('b', null, nameOf(u.sender)), h('span', { class: 'muted' }, plural(u.msg_count, 'msg'))))),
      (sm.unsub_failed + sm.unsub_awaiting + sm.unsub_repeat) > 0 && h('p', null,
        [sm.unsub_awaiting && `${plural(sm.unsub_awaiting, 'page')} opened and waiting for you to confirm`,
         sm.unsub_failed && `${plural(sm.unsub_failed, 'attempt')} unconfirmed`,
         sm.unsub_repeat && `${plural(sm.unsub_repeat, 'sender')} still mailing after you unsubscribed`].filter(Boolean).join(', ') + '.'),
      (d.unsub.total + sm.unsub_failed + sm.unsub_awaiting + sm.unsub_repeat) > 0 && h('div', { class: 'bar' }, h('a', { class: 'btn primary', href: '#/unsub' }, 'Review queue →'))),
    d.forged.total > 0 && sec(null, `Forged senders kept out — ${d.forged.total}`,
      h('p', { class: 'muted small' }, "These failed their sender's DMARC check, so the From line is very likely forged. They were not added to the unsubscribe queue."),
      h('ul', null, d.forged.top.map((f) => h('li', null, h('b', null, nameOf(f.sender)), f.subject, h('span', { class: 'muted' }, date10(f.date)))))),
    sec('events', `Events found — ${d.events.total}`,
      d.events.next.length ? h('ul', null, d.events.next.map((e) => h('li', null,
        h('span', { class: 'mono dim' }, fmtWhen(e.start, e.all_day)), h('b', null, e.title),
        e.location && h('span', { class: 'muted' }, e.location))))
        : empty('Nothing upcoming detected.')),
    sec(null, 'Needs your attention',
      d.attention.length ? h('ul', null, d.attention.map((m) => h('li', null,
        h('b', null, nameOf(m.sender)), m.subject, h('span', { class: 'muted' }, m.account))))
        : empty('No high-importance mail flagged this week.')),
    sec('spam', 'Spam audit',
      d.spam.total
        ? [h('p', null, `${plural(d.spam.total, 'possible false positive')} flagged for your review`
            + (d.spam.phishing ? `, ${d.spam.phishing} that look like phishing.` : '.')),
           h('ul', null, d.spam.top.map((s) => h('li', null, h('b', null, nameOf(s.sender)), s.subject,
             s.phishing_risk && h('span', { class: 'tag warn' }, 'possible phishing'))))]
        : empty('No receipt or security-style mail found in Junk this run.')),
    sec('leaderboard', 'Noisiest senders (30 days)',
      d.noise.length ? h('ul', null, d.noise.map((n, i) => h('li', null,
        h('span', { class: 'mono dim' }, `${i + 1}.`), h('b', null, nameOf(n.sender)),
        h('span', { class: 'muted' }, plural(n.n, 'msg')), h('span', { class: 'tag' }, n.category))))
        : empty('No noise recorded yet — run a scan first.')));
}

// ---------------------------------------------------------------- unsubscribe
const METHOD = { one_click: 'one-click', browser: 'opens browser', mailto: 'mail compose', none: 'no target' };
function unsubAction(action, emails, label) {
  return mutate(`mailsweep unsub review   # ${label}`, '/api/unsub', { action, emails }, { showId: true });
}
/* A link two unrelated senders share is how a spoofed From line files someone else's link under a real sender. */
const authTag = (u) => u.auth === 'unverified' && [' ', h('span', { class: 'tag warn', title: u.auth_detail || '' }, 'sender not verified')];
const authLine = (u) => u.auth === 'unverified' && u.auth_detail ? `⚠ sender not verified: ${u.auth_detail}` : null;
const linkTags = (u) => [
  u.blocked_by && [' ', h('span', { class: 'tag warn', title: `Same link as ${u.blocked_by}, a sender you protected` }, 'same link as protected sender')],
  !u.blocked_by && u.link_conflict && [' ', h('span', { class: 'tag warn', title: 'Another, unrelated sender uses this exact link' }, 'link shared with another sender')],
];
const linkLine = (u) => {
  const odd = (u.peers || []).filter((p) => !p.same_org || p.protected);
  return odd.length ? 'this exact link is also on: ' + odd.map((p) => `${p.sender_email} (${p.status})`).join(', ') : null;
};
const priorLine = (u) => u.prior && `unsubscribed from ${u.prior.sender_email} (same domain) on ${date10(u.prior.when)}`;
async function approveUnsubs(rows, { action = 'approve', title = 'Unsubscribe from', confirm = 'Unsubscribe' } = {}) {
  const by = (m) => rows.filter((r) => r.method === m).length;
  const lines = [];
  if (by('one_click')) lines.push(`${plural(by('one_click'), 'sender')}: one-click POST first. If the sender rejects it, the page opens in your browser and waits for you to confirm.`);
  if (by('browser')) lines.push(`${plural(by('browser'), 'sender')}: opens the unsubscribe page in your browser and waits for you to confirm it worked.`);
  if (by('mailto')) lines.push(`${plural(by('mailto'), 'sender')}: opens a mail compose window; send it, then confirm.`);
  if (by('none')) lines.push(`${plural(by('none'), 'sender')}: no usable target, recorded as approved only.`);
  if (rows.length > 5 && by('none') < rows.length) lines.push('At most 5 pages open per click; anything past that stays queued for your next click.');
  const blocked = rows.filter((r) => r.blocked_by).length, shared = rows.filter((r) => !r.blocked_by && r.link_conflict).length;
  if (blocked) lines.push(`${plural(blocked, 'sender')} use the same link as a sender you protected and will be refused.`);
  const unverified = rows.filter((r) => r.auth === 'unverified').length;
  if (unverified) lines.push(`${plural(unverified, 'sender')} could not be verified (nothing authenticated the From address). The link comes from an unauthenticated header, so check the sender first.`);
  if (shared) lines.push(`${plural(shared, 'sender')} carry a link that another, unrelated sender also uses. Check who the message is really from.`);
  const prior = rows.filter((r) => r.prior).length;
  if (prior) lines.push(`${plural(prior, 'sender')} ${prior === 1 ? 'is' : 'are'} at a domain you've unsubscribed from before.`);
  const r = await ask({ title: `${title} ${plural(rows.length, 'sender')}?`, lines, confirm });
  if (!r.ok) return false;
  rows.forEach((x) => S.ui.sel.delete(x.sender_email));
  const res = await unsubAction(action, rows.map((x) => x.sender_email), `${action} ${plural(rows.length, 'sender')}`);
  return !!res;
}
const retryRows = (rows) => approveUnsubs(rows, { action: 'retry', title: 'Retry the unsubscribe for', confirm: 'Retry' });
async function bulkQuiet(rows, action, title, line, confirm) {
  const r = await ask({ title: `${title} ${plural(rows.length, 'sender')}?`, lines: [line], confirm });
  if (r.ok) await unsubAction(action, rows.map((x) => x.sender_email), `${action} ${plural(rows.length, 'sender')}`);
}
function unsubCard() {
  const c = (action, label) => async (it) => {
    const res = await unsubAction(action, [it.sender_email], `${label} ${it.sender_email}`);
    return !!res;
  };
  const move = (dest, action) => async (it) => {
    const r = await ask({ title: `Move existing mail from ${nameOf(it.sender)} to ${dest}?`,
      lines: ['Moves every scanned message on file from this sender, the same as dragging it in Mail.app. Recoverable; nothing is deleted.'],
      confirm: `Move to ${dest}`, danger: dest === 'Trash' });
    return r.ok ? !!(await unsubAction(action, [it.sender_email], `${action} existing mail from ${it.sender_email}`)) : false;
  };
  return cardView({
    title: 'Unsubscribe — review each', cmd: 'mailsweep unsub review', keyOf: (x) => x.sender_email,
    who: (it) => nameOf(it.sender),
    facts: (it) => [it.sender_email, `${plural(it.msg_count, 'message')} seen · last ${date10(it.last_received)}`,
      `“${it.last_subject || ''}”`, `inbox: ${it.account} · to: ${it.last_to || '(unknown)'}`,
      `on approval: ${METHOD[it.method]}${it.host ? ' → ' + it.host : ''}`, priorLine(it), authLine(it), linkLine(it) && '⚠ ' + linkLine(it)],
    actions: [
      { key: 'y', label: 'Unsub →', tone: 'primary', run: async (it) => {
        const r = await ask({ title: `Unsubscribe from ${nameOf(it.sender)}?`,
          lines: [`${METHOD[it.method]}${it.host ? ' → ' + it.host : ''}`, priorLine(it)].filter(Boolean), confirm: 'Unsubscribe' });
        return r.ok ? !!(await unsubAction('approve', [it.sender_email], `approve ${it.sender_email}`)) : false;
      } },
      { key: 'n', label: '← Skip', run: c('skip', 'skip') },
      { key: 'p', label: 'Protect', run: c('protect', 'protect') },
      { key: 't', label: 'Trash existing', stay: true, run: move('Trash', 'trash') },
      { key: 'a', label: 'Archive existing', stay: true, run: move('Archive', 'archive') },
    ],
  });
}
/* Senders we already tried to unsubscribe from: attempts that didn't confirm, and senders still mailing afterwards. */
function secondLook(d) {
  if (!d.failed.length && !d.repeat.length && !d.awaiting.length) return null;
  const one = (action, u, label) => unsubAction(action, [u.sender_email], `${label} ${u.sender_email}`);
  const btn = (label, cls, fn, opts = {}) => h('button', { class: 'btn small ' + cls, onclick: fn, disabled: opts.disabled, title: opts.title }, label);
  const blockedTitle = (u) => u.blocked_by ? `Refused: same link as ${u.blocked_by}, a sender you protected` : undefined;
  const repeat = [...d.repeat].sort((a, b) => a.within_grace - b.within_grace || b.n - a.n);
  return h('section', { class: 'attn', id: 'attn' },
    h('h2', null, `Second look — ${d.awaiting_total + d.failed_total + d.repeat_total}`),
    d.awaiting.length > 0 && [
      h('h3', null, `Opened in your browser — did it work? — ${d.awaiting_total}`),
      h('p', { class: 'muted small' }, "These pages were opened for you to finish. Some unsubscribe the moment they load, others still want a click. Nothing here counts as done until you say so."),
      h('div', { class: 'bar' },
        btn(`They all worked (${d.awaiting.length})`, '', () => bulkQuiet(d.awaiting, 'markdone', 'Mark done for',
          'Only do this if you checked each page. Anything you confirm here is treated as unsubscribed.', 'They all worked'))),
      pager(d.awaiting, (u) => h('div', { class: 'row' },
        h('div', { class: 'body' },
          h('div', { class: 'name' }, nameOf(u.sender), ' ', h('span', { class: 'tag hot' }, `opened ${date10(u.opened_at)}`), linkTags(u), authTag(u)),
          h('div', { class: 'sub' }, u.sender_email),
          u.last_note && h('div', { class: 'sub' }, u.last_note),
          linkLine(u) && h('div', { class: 'sub warnline' }, linkLine(u))),
        h('div', { class: 'side-r' },
          btn('It worked', 'primary', () => one('markdone', u, 'confirmed')),
          u.can_open && btn('Open again', '', () => one('open', u, 'open again'), { disabled: !!u.blocked_by, title: blockedTitle(u) }),
          btn('Give up', '', () => one('skip', u, 'give up on')))), 'shownAwaiting')],
    d.failed.length > 0 && [
      h('h3', null, `Attempted, not confirmed — ${d.failed_total}`),
      h('p', { class: 'muted small' }, 'These were approved but the unsubscribe never came back as done: the request failed, or nothing was sent.'),
      h('div', { class: 'bar' },
        btn(`Retry all ${d.failed.length}`, 'primary', () => retryRows(d.failed)),
        btn(`Give up on all ${d.failed.length}`, '', () => bulkQuiet(d.failed, 'skip', 'Give up on',
          'Marks them skipped: they leave this list and MailSweep stops trying.', 'Give up'))),
      pager(d.failed, (u) => h('div', { class: 'row' },
        h('div', { class: 'body' },
          h('div', { class: 'name' }, nameOf(u.sender), ' ', h('span', { class: 'tag warn' }, `attempt ${u.attempts || 1}`), linkTags(u), authTag(u)),
          h('div', { class: 'sub' }, `${u.sender_email} · last tried ${date10(u.updated_at)}`),
          h('div', { class: 'sub note' }, u.last_note || 'no result was recorded for this attempt'),
          linkLine(u) && h('div', { class: 'sub warnline' }, linkLine(u))),
        h('div', { class: 'side-r' },
          btn('Retry', 'primary', () => retryRows([u]), { disabled: !!u.blocked_by, title: blockedTitle(u) }),
          u.can_open && btn('Open page', '', () => one('open', u, 'open'), { disabled: !!u.blocked_by, title: blockedTitle(u) }),
          btn('Mark done', '', () => one('markdone', u, 'mark done')),
          btn('Give up', '', () => one('skip', u, 'give up on')))), 'shownFailed')],
    d.repeat.length > 0 && [
      h('h3', null, `Unsubscribed, but still sending — ${d.repeat_total}`),
      h('p', { class: 'muted small' }, 'Mail from these senders arrived after your unsubscribe went through. Senders get about 10 business days to comply, so recent ones are only flagged, not failed.'),
      h('div', { class: 'bar' },
        btn(`Ignore all ${repeat.length}`, '', () => bulkQuiet(repeat, 'ack', 'Ignore',
          "They'll only be flagged again if they send more.", 'Ignore'))),
      pager(repeat, (u) => h('div', { class: 'row' },
        h('div', { class: 'body' },
          h('div', { class: 'name' }, nameOf(u.sender), ' ',
            u.within_grace ? h('span', { class: 'tag', title: 'Unsubscribed under 14 days ago' }, 'within 14 days')
              : h('span', { class: 'tag warn' }, 'still sending'), linkTags(u), authTag(u)),
          h('div', { class: 'sub' }, `${u.sender_email} · unsubscribed ${date10(u.since)} · ${plural(u.n, 'message')} since, latest ${date10(u.last_received)}`),
          h('div', { class: 'sub' }, `“${u.last_subject || ''}”`),
          linkLine(u) && h('div', { class: 'sub warnline' }, linkLine(u))),
        h('div', { class: 'side-r' },
          btn('Unsub again', 'primary', () => retryRows([u]), { disabled: !!u.blocked_by, title: blockedTitle(u) }),
          u.can_open && btn('Open page', '', () => one('open', u, 'open'), { disabled: !!u.blocked_by, title: blockedTitle(u) }),
          btn('Ignore', '', () => one('ack', u, 'ignore')))), 'shownRepeat')]);
}
function unsubPage() {
  if (S.ui.card) return unsubCard();
  const d = S.data, items = d.items, sel = S.ui.sel;
  const chosen = items.filter((x) => sel.has(x.sender_email));
  const target = chosen.length ? chosen : items;
  const all = items.length > 0 && chosen.length === items.length;
  const second = d.awaiting_total + d.failed_total + d.repeat_total;
  return shell('Unsubscribe queue', 'mailsweep unsub list',
    second > 0 && h('div', { class: 'notice' },
      `${second} sender${second === 1 ? '' : 's'} need a second look: `
      + [d.awaiting_total && `${d.awaiting_total} waiting for you to confirm`, d.failed_total && `${d.failed_total} unconfirmed`,
         d.repeat_total && `${d.repeat_total} still sending after you unsubscribed`].filter(Boolean).join(', ') + '. ',
      h('button', { class: 'linkbtn', onclick: () => { const el = document.getElementById('attn'); if (el) el.scrollIntoView({ behavior: 'smooth' }); } }, 'Jump to them ↓')),
    items.length === 0 ? empty('Queue is empty. Nothing is pending.') : h('div', { class: 'queue' }, [
      h('div', { class: 'bar sticky' },
        h('label', { class: 'selall' }, h('input', { type: 'checkbox', checked: all, 'aria-label': 'Select all',
          onchange: (e) => { if (e.target.checked) items.forEach((x) => sel.add(x.sender_email)); else sel.clear(); render(); } }),
          chosen.length ? `${chosen.length} selected` : 'Select all'),
        h('span', { class: 'grow' }),
        chosen.length > 0 && h('button', { class: 'btn small', onclick: async () => {
          const em = chosen.map((x) => x.sender_email); sel.clear(); await unsubAction('skip', em, `skip ${plural(em.length, 'sender')}`); } }, 'Skip selected'),
        chosen.length > 0 && h('button', { class: 'btn small', onclick: async () => {
          const em = chosen.map((x) => x.sender_email); sel.clear(); await unsubAction('protect', em, `protect ${plural(em.length, 'sender')}`); } }, 'Protect selected'),
        h('button', { class: 'btn', onclick: () => startCard(items) }, 'Review each'),
        h('button', { class: 'btn primary', onclick: () => approveUnsubs(target) },
          chosen.length ? `Approve selected ${chosen.length}` : `Approve all ${items.length}`)),
      pager(items, (u) => h('div', { class: 'row' + (sel.has(u.sender_email) ? ' sel' : '') },
        h('input', { type: 'checkbox', checked: sel.has(u.sender_email), 'aria-label': 'Select ' + nameOf(u.sender),
          onchange: (e) => { e.target.checked ? sel.add(u.sender_email) : sel.delete(u.sender_email); render(); } }),
        h('div', { class: 'body' },
          h('div', { class: 'name' }, nameOf(u.sender), ' ', h('span', { class: 'sub' }, `· ${plural(u.msg_count, 'msg')}`),
            u.prior && [' ', h('span', { class: 'tag', title: priorLine(u) }, 'unsubscribed at this domain before')], linkTags(u), authTag(u)),
          h('div', { class: 'sub' }, `${u.sender_email} · last ${date10(u.last_received)} · “${u.last_subject || ''}”`),
          u.prior && h('div', { class: 'sub' }, priorLine(u)),
          authLine(u) && h('div', { class: 'sub warnline' }, authLine(u)),
          linkLine(u) && h('div', { class: 'sub warnline' }, linkLine(u))),
        h('div', { class: 'side-r' }, h('span', { class: 'tag' + (u.method === 'one_click' ? ' hot' : '') }, METHOD[u.method])))),
      d.total > items.length && h('p', { class: 'dim' }, `Showing the first ${items.length} of ${d.total}.`)]),
    secondLook(d));
}

// ---------------------------------------------------------------- events
function evAct(action, it, label, extra = {}) {
  return mutate(`mailsweep events review   # ${label}`, '/api/events', { action, id: it.id, ...extra });
}
async function evOk(res) { return !!res && res.results.every((r) => r.ok); }
function eventActions() {
  return [
    { key: 'y', label: 'Add to calendar', tone: 'primary', run: async (it) => evOk(await evAct('add', it, `add "${it.title}"`)) },
    { key: 'n', label: 'Dismiss', run: async (it) => evOk(await evAct('dismiss', it, `dismiss "${it.title}"`)) },
    { key: 's', label: 'Skip for now', local: true, run: async () => true },
    { key: 't', label: 'Trash source', run: async (it) => evOk(await evAct('trash', it, `trash source of "${it.title}"`)) },
    { key: 'a', label: 'Archive source', run: async (it) => evOk(await evAct('archive', it, `archive source of "${it.title}"`)) },
    { key: 'p', label: 'Protect sender', run: async (it) => {
      const email = it.source_sender_email || nameOf(it.source_sender);
      const r = await ask({ title: `Never suggest events from ${email}?`,
        lines: ['Dismisses their pending candidates and stops future ones.'], confirm: 'Protect sender',
        select: { label: 'Also move their scanned messages:', options: [['', 'leave alone'], ['Trash', 'to Trash'], ['Archive', 'to Archive']] } });
      return r.ok ? evOk(await evAct('protect', it, `protect ${email}`, { move: r.value || null })) : false;
    } },
  ];
}
function eventsPage() {
  if (S.ui.card) return cardView({
    title: 'Events — review each', cmd: 'mailsweep events review', keyOf: (x) => x.id, who: (it) => it.title,
    facts: (it) => [fmtWhen(it.start, it.all_day) + (it.end ? ' – ' + it.end : ''), it.location || '(no location)',
      `from ${nameOf(it.source_sender)} — “${it.source_subject}”`, `${Math.round(it.confidence * 100)}% confidence`,
      `inbox: ${it.inbox || '?'} · to: ${it.to || '(unknown)'}`],
    actions: eventActions(),
  });
  const d = S.data;
  const [add, dismiss] = eventActions();
  return shell('Events found', 'mailsweep events list',
    h('div', { class: 'bar' },
      h('label', { class: 'selall' }, h('input', { type: 'checkbox', checked: S.ui.past,
        onchange: (e) => { S.ui.past = e.target.checked; S.data = null; render(); load(); } }),
        `include past candidates${d.hidden_past ? ` (${d.hidden_past} hidden)` : ''}`),
      h('span', { class: 'grow' }),
      d.items.length > 0 && h('button', { class: 'btn primary', onclick: () => startCard(d.items) }, 'Review each')),
    d.items.length === 0 ? empty('No pending event candidates.') : pager(d.items, (e) => h('div', { class: 'row' },
      h('div', { class: 'body' },
        h('div', { class: 'name' }, e.title, ' ', h('span', { class: 'tag' }, Math.round(e.confidence * 100) + '%')),
        h('div', { class: 'sub mono' }, fmtWhen(e.start, e.all_day) + (e.location ? ' · ' + e.location : '')),
        h('div', { class: 'sub' }, `from ${nameOf(e.source_sender)} — “${e.source_subject}”`)),
      h('div', { class: 'side-r' },
        h('button', { class: 'btn small primary', onclick: () => add.run(e) }, 'Add to calendar'),
        h('button', { class: 'btn small', onclick: () => dismiss.run(e) }, 'Dismiss')))));
}

// ---------------------------------------------------------------- spam audit
function spAct(action, it, label) {
  return mutate(`mailsweep spam review   # ${label}`, '/api/spam', { action, id: it.message_id });
}
function spamActions() {
  const rescue = (action, label) => async (it) => {
    if (it.phishing_risk) {
      const r = await ask({ title: 'This one may be phishing', danger: true, confirm: 'Rescue anyway',
        lines: ["The sender's display name doesn't match its domain. Moving it to your Inbox makes it easier to click."] });
      if (!r.ok) return false;
    }
    return evOk(await spAct(action, it, `${label} "${it.subject}"`));
  };
  return [
    { key: 'y', label: 'Rescue to inbox', tone: 'primary', run: rescue('rescue', 'rescue') },
    { key: 'a', label: 'Always rescue sender', run: rescue('trust', 'rescue + trust') },
    { key: 'n', label: 'Leave in spam', run: async (it) => evOk(await spAct('leave', it, `leave "${it.subject}"`)) },
    { key: 'p', label: 'Confirm junk', run: async (it) => evOk(await spAct('protect', it, `protect ${it.sender_email}`)) },
  ];
}
function spamPage() {
  const spamFacts = (it) => [`${it.account} / ${it.mailbox} · ${date10(it.date_received)}`, `reason: ${it.reason}`];
  if (S.ui.card) return cardView({
    title: 'Spam audit — review each', cmd: 'mailsweep spam review', keyOf: (x) => x.message_id,
    who: (it) => nameOf(it.sender),
    facts: (it) => [`“${it.subject}”`, ...spamFacts(it), it.phishing_risk && '⚠ possible phishing: sender display name doesn’t match its domain'],
    actions: spamActions(),
  });
  const d = S.data;
  const [rescue, , leave] = spamActions();
  return shell('Spam audit', 'mailsweep spam list',
    d.items.length > 0 && h('div', { class: 'bar' }, h('span', { class: 'grow' }),
      h('button', { class: 'btn primary', onclick: () => startCard(d.items) }, 'Review each')),
    d.items.length === 0 ? empty('No spam false-positive candidates pending.') : pager(d.items, (s) => h('div', { class: 'row' },
      h('div', { class: 'body' },
        h('div', { class: 'name' }, nameOf(s.sender), ' ', s.phishing_risk && h('span', { class: 'tag warn' }, 'possible phishing')),
        h('div', { class: 'sub' }, `“${s.subject}”`), h('div', { class: 'sub' }, spamFacts(s).join(' · '))),
      h('div', { class: 'side-r' },
        h('button', { class: 'btn small primary', onclick: () => rescue.run(s) }, 'Rescue'),
        h('button', { class: 'btn small', onclick: () => leave.run(s) }, 'Leave')))));
}

// ---------------------------------------------------------------- leaderboard
function leaderboardPage() {
  const d = S.data, max = Math.max(1, ...d.items.map((x) => x.n));
  const move = async (it, action, dest) => {
    const r = await ask({ title: `Move ${plural(it.movable, 'scanned message')} from ${nameOf(it.sender)} to ${dest}?`,
      lines: ['Same as dragging them in Mail.app. Recoverable; nothing is permanently deleted.'],
      confirm: `Move to ${dest}`, danger: dest === 'Trash',
      checkbox: it.can_unsub ? { label: 'Also unsubscribe from this sender now', checked: false } : null });
    if (!r.ok) return;
    await mutate(`mailsweep stats --review   # ${action} all from ${it.sender_email}`, '/api/leaderboard',
      { action, sender_email: it.sender_email, unsub: r.checked });
  };
  return shell('Noise leaderboard', `mailsweep stats --days ${d.days}`,
    h('div', { class: 'bar' }, h('label', { class: 'selall' }, 'Window ',
      h('select', { class: 'sel', 'aria-label': 'Window in days', onchange: (e) => { S.ui.days = +e.target.value; S.data = null; render(); load(); } },
        [7, 30, 90].map((n) => h('option', { value: n, selected: n === d.days }, `${n} days`))))),
    d.items.length === 0 ? empty('No noise recorded yet — run a scan first.') : pager(d.items, (r, i) => h('div', { class: 'row' },
      h('div', { class: 'rank' }, i + 1),
      h('div', { class: 'body' },
        h('div', { class: 'name' }, nameOf(r.sender), ' ', h('span', { class: 'tag' }, r.category)),
        h('div', { class: 'sub' }, `${plural(r.n, 'msg')} · last ${date10(r.last_received)} · “${r.last_subject || ''}”`),
        h('div', { class: 'meter', 'aria-hidden': 'true' }, (() => { const b = h('i'); b.style.width = Math.round(100 * r.n / max) + '%'; return b; })())),
      h('div', { class: 'side-r' },
        [['trash', 'Trash', 'Trash all'], ['junk', 'Junk', 'Junk'], ['archive', 'Archive', 'Archive']].map(([a, dest, text]) =>
          h('button', { class: 'btn small', disabled: r.movable === 0, title: r.movable ? '' : 'no scanned messages on file for this sender',
            onclick: () => move(r, a, dest) }, text))))));
}

// ---------------------------------------------------------------- reviews
const REVIEW_TABS = [['ready', 'Ready'], ['pending_receipt', 'Awaiting receipt'], ['approved', 'Approved'],
  ['skipped', 'Skipped'], ['returned_excluded', 'Returned'], ['name_unresolvable', 'Unresolved']];
function reviewsPage() {
  const d = S.data;
  const act = (action, it, label) => mutate(`mailsweep purchases review   # ${label}`, '/api/reviews',
    { action, key: it.key, text: S.ui.drafts[it.key] ?? it.review_text ?? null });
  return shell('Purchase reviews', 'mailsweep purchases list',
    h('div', { class: 'tabs', role: 'group', 'aria-label': 'Status' }, REVIEW_TABS.map(([id, label]) =>
      h('button', { class: 'tab', 'aria-pressed': String(d.status === id),
        onclick: () => { S.ui.status = id; S.ui.shown = PAGE; S.data = null; render(); load(); } },
        `${label} ${d.counts[id] || 0}`))),
    d.status === 'approved' && d.items.length > 0 && h('div', { class: 'bar' },
      h('button', { class: 'btn primary', onclick: () => mutate('mailsweep purchases export', '/api/reviews/export', {}) },
        `Export ${d.items.length} to CSV`)),
    d.items.length === 0 ? empty('Nothing here.') : pager(d.items, (r) => h('div', { class: 'row' },
      h('div', { class: 'body' },
        h('div', { class: 'name' }, `${r.vendor} — ${r.item}`),
        h('div', { class: 'sub' }, `order ${r.order_ref || '(no ref found)'} · ${date10(r.order_date)}`),
        (r.review_text != null || d.status === 'ready') && h('textarea', { class: 'draft', 'aria-label': 'Review draft',
          placeholder: 'No draft — write one, or skip.',
          oninput: (e) => { S.ui.drafts[r.key] = e.target.value; } }, S.ui.drafts[r.key] ?? r.review_text ?? ''),
        h('div', { class: 'side-r' },
          d.status !== 'approved' && h('button', { class: 'btn small primary', onclick: () => act('approve', r, `approve ${r.item}`) }, 'Approve'),
          d.status !== 'skipped' && h('button', { class: 'btn small', onclick: () => act('skip', r, `skip ${r.item}`) }, 'Skip'),
          d.status !== 'pending_receipt' && h('button', { class: 'btn small', onclick: () => act('notreceived', r, `not received ${r.item}`) }, 'Not received'),
          d.status !== 'returned_excluded' && h('button', { class: 'btn small', onclick: () => act('exclude', r, `exclude ${r.item}`) }, 'Returned / exclude'))))));
}

const PAGES = { digest: digestPage, unsub: unsubPage, events: eventsPage, spam: spamPage,
  leaderboard: leaderboardPage, reviews: reviewsPage };

window.addEventListener('hashchange', navigate);
document.addEventListener('visibilitychange', () => { if (!document.hidden && !S.busy && S.ui && !S.ui.card) load(); });
navigate();
})();
