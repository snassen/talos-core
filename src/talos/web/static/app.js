// Talos UI. Vanilla modules, no build step. All data is inserted as text, never as markup:
// mail is untrusted input, so there is no innerHTML anywhere in this file.

const $main = document.getElementById('main');
const $rail = document.getElementById('rail');

// ---------------------------------------------------------------- helpers
function add(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
}
// replaceChildren() would print a null child as the text "null"; add() skips it.
function fill(el, ...kids) { el.replaceChildren(); add(el, kids); return el; }
function h(tag, a, ...kids) {
  const el = document.createElement(tag);
  if (a) for (const [k, v] of Object.entries(a)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style') el.style.cssText = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  add(el, kids);
  return el;
}
const SVGNS = 'http://www.w3.org/2000/svg';
function s(tag, a, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  if (a) for (const [k, v] of Object.entries(a)) if (v != null) el.setAttribute(k, v);
  add(el, kids);
  return el;
}
const fmt = n => Number(n || 0).toLocaleString('sv-SE');
const day = iso => iso ? new Date(iso).toLocaleDateString('sv-SE') : '—';
const when = iso => {
  if (!iso) return '—';
  const d = new Date(iso), now = new Date();
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString('sv-SE', {hour: '2-digit', minute: '2-digit'});
  if (d.getFullYear() === now.getFullYear()) return d.toLocaleDateString('sv-SE', {day: 'numeric', month: 'short'});
  return d.toLocaleDateString('sv-SE');
};
// Teams messages (medium teams_chat / teams_channel) share the mail views; these mark them.
const isTeams = m => typeof m.medium === 'string' && m.medium.startsWith('teams');
const teamsPill = m => isTeams(m) ? h('span', {class: 'pill ac', title: m.medium === 'teams_channel' ? 'Teams channel' : 'Teams chat'}, 'Teams') : null;
// A shared Teams file is a link, opened only when it is a plain https URL (message content is untrusted).
const safeLink = u => typeof u === 'string' && /^https:\/\//i.test(u) ? u : null;
const bytes = n => n > 1e9 ? (n / 1e9).toFixed(1) + ' GB' : n > 1e6 ? (n / 1e6).toFixed(1) + ' MB' : n > 1e3 ? Math.round(n / 1e3) + ' kB' : (n || 0) + ' B';
async function fetchJSON(path, opts) {
  const r = await fetch(path, opts);
  // The door (talos.webauth): a session that ended goes back to the sign-in page; a session that
  // has read a lot in a short time is asked for an authenticator code, then the request goes again.
  if (r.status === 401) {
    location.assign('/login?next=' + encodeURIComponent(location.pathname + location.hash));
    throw new Error('Signed out');
  }
  if (r.status === 429) {
    const d = await r.clone().json().catch(() => ({}));
    if (d.step_up && await stepUp(d.error)) return fetchJSON(path, opts);
  }
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}
let stepUpAsk = null;
function stepUp(why) {
  if (stepUpAsk) return stepUpAsk;
  stepUpAsk = (async () => {
    for (let tries = 0; tries < 3; tries++) {
      const code = prompt((why || 'Enter a code from your authenticator app to go on.') + (tries ? '\n\nThat code did not work; try the next one.' : ''));
      if (!code) return false;
      const r = await fetch('/auth/step-up', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Talos': '1'}, body: JSON.stringify({code})});
      if (r.ok) return true;
    }
    return false;
  })().finally(() => { stepUpAsk = null; });
  return stepUpAsk;
}
async function signOut() {
  await fetch('/auth/logout', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Talos': '1'}, body: '{}'}).catch(() => {});
  location.assign('/login');
}
// GET answers are kept in memory, so returning to a page is instant. An answer older than FRESH_MS is shown
// at once and refreshed behind it; if the refresh differs, the view redraws in place. A write does not throw
// the memory away (every page would be slow again after it): it marks every answer from before it as stale,
// and for WRITE_FRESH_MS after the write such an answer is fetched fresh instead of shown, so the page that
// made the change shows it at once; later ones show the old answer at once and redraw when the new one is in.
// The same request asked twice at once goes to the server once. CACHE.clear() still forgets everything.
const CACHE = new Map(), FRESH_MS = 5000, CACHE_MAX = 200, WRITE_FRESH_MS = 5000;
const INFLIGHT = new Map();  // path -> {p, at}
let WRITE_AT = 0;
function fetchShared(path) {
  const cur = INFLIGHT.get(path);
  if (cur && cur.at >= WRITE_AT) return cur.p;
  const at = Date.now();
  const p = fetchJSON(path).then(data => ({data, at})).finally(() => { if (INFLIGHT.get(path) && INFLIGHT.get(path).p === p) INFLIGHT.delete(path); });
  INFLIGHT.set(path, {p, at});
  return p;
}
// Stored with the time the request started, so an answer asked before a write counts as from before it.
function remember(path, r) {
  CACHE.delete(path);
  CACHE.set(path, {data: r.data, at: r.at, json: JSON.stringify(r.data)});
  if (CACHE.size > CACHE_MAX) CACHE.delete(CACHE.keys().next().value);  // the least recently used goes first
}
async function api(path, opts) {
  if (opts && opts.method && opts.method !== 'GET') return fetchJSON(path, opts);
  learnPath(path);
  const hit = CACHE.get(path);
  if (hit && !(hit.at < WRITE_AT && Date.now() - WRITE_AT < WRITE_FRESH_MS)) {
    CACHE.delete(path); CACHE.set(path, hit);
    if (Date.now() - hit.at > FRESH_MS || hit.at < WRITE_AT) revalidate(path, hit);
    return hit.data;
  }
  const r = await fetchShared(path);
  remember(path, r);
  return r.data;
}
function revalidate(path, hit) {
  if (hit.pending) return;
  hit.pending = true;
  const view = S.view;
  fetchShared(path).then(r => {
    const json = JSON.stringify(r.data);
    hit.pending = false;
    hit.at = r.at;
    if (json === hit.json) return;
    hit.data = r.data;
    hit.json = json;
    if (S.view === view && !(PANE.open && PANE.layout.mode === 'overlay')) render(true);
  }).catch(() => { hit.pending = false; });
}
// Every write carries X-Talos: the server refuses a POST without it, so no other page can post here.
// A write marks the answers from before it stale; a read-only POST (a rule preview) passes keep=true.
const post = (path, body, keep) => {
  if (!keep) WRITE_AT = Date.now();
  return api(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Talos': '1'}, body: JSON.stringify(body)});
};

// ---- fetching ahead. Each view's requests are learned while it draws (paths only), and fetched before they are
// needed: while the pointer rests on a place in the rail, and for the main places when Talos has been idle a moment.
// Never a message's, a conversation's or an attachment's own request (those count against the read budget,
// docs/security.md), a search, or anything that syncs or refreshes.
const NO_PREFETCH = /^\/api\/(messages\/\d|threads\/\d|attachments\/\d|teams\/refresh|calendar\/sync|drafts)|[?&](q|refresh|around)=/;
const WARM_VIEWS = ['messages', 'work', 'calendar', 'teams', 'today'];
let VIEW_PATHS = {};
try { const x = JSON.parse(localStorage.getItem('talos-view-paths') || '{}'); if (x && typeof x === 'object' && !Array.isArray(x)) VIEW_PATHS = x; } catch (e) {}
const LEARN = {view: null, until: 0, list: null, timer: null};
let PREFETCHING = 0;
function saveLearned() {
  if (LEARN.view && LEARN.list && LEARN.list.length) {
    VIEW_PATHS = {...VIEW_PATHS, [LEARN.view]: LEARN.list.slice(0, 12)};
    try { localStorage.setItem('talos-view-paths', JSON.stringify(VIEW_PATHS)); } catch (e) {}
  }
  LEARN.view = null;
}
function beginLearning(view) {
  clearTimeout(LEARN.timer);
  if (LEARN.view !== view || !LEARN.list || !LEARN.list.length || Date.now() > LEARN.until) saveLearned();  // what the last view asked for
  else return;  // the same view drawn again in place: keep learning into the same list
  // Mail is learned in its default view only: that is what a place in the rail opens.
  const ok = view !== 'messages' || JSON.stringify(S.f) === JSON.stringify(defaults());
  Object.assign(LEARN, {view: ok ? view : null, until: Date.now() + 1500, list: []});
  LEARN.timer = setTimeout(saveLearned, 1600);
}
function learnPath(path) {
  if (PREFETCHING || !LEARN.view || Date.now() > LEARN.until || NO_PREFETCH.test(path) || LEARN.list.includes(path)) return;
  LEARN.list.push(path);
}
// One request at a time, so a click's own requests never wait behind a burst of them.
async function prefetchView(view) {
  if (!view || view === S.view || document.hidden) return;
  for (const path of VIEW_PATHS[view] || []) {
    const hit = CACHE.get(path);
    if (NO_PREFETCH.test(path) || (hit && Date.now() - hit.at <= FRESH_MS && hit.at >= WRITE_AT)) continue;
    PREFETCHING++;
    try { remember(path, await fetchShared(path)); } catch (e) { /* a prefetch that fails is simply not there */ }
    finally { PREFETCHING--; }
  }
}
let hoverTimer = null;
const hubView = hb => HUB_LAST[hb.id] && hb.tabs.some(([id]) => tabView(id) === HUB_LAST[hb.id]) ? HUB_LAST[hb.id] : tabView(hb.tabs[0][0]);
function prefetchSoon(hb) { clearTimeout(hoverTimer); hoverTimer = setTimeout(() => prefetchView(hubView(hb)), 90); }
async function warmUp() {
  for (const v of WARM_VIEWS) await prefetchView(v);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) setTimeout(warmUp, 1500); });

// One colour, letter and name per account, keyed by id so they are the same on every page and never
// shift when an account is added or has no messages in a window. They come from the owner's
// accounts.json (/api/owner, read once before the first page): each account's ui settings, else its
// display name, its first letter and a colour by its place. OWNER.id is the name the owner's own
// decisions are stored under.
const OWNER = {id: null};
const ACCOUNT_COLOR = {}, ACCOUNT_NAME = {}, ACCOUNT_LETTER = {}, ACCOUNT_INK = {}, ACCOUNT_ORDER = [];
async function loadOwner() {
  try {
    const o = await api('/api/owner');
    OWNER.id = o.id;
    for (const a of o.accounts) {
      ACCOUNT_ORDER.push(a.id);
      ACCOUNT_NAME[a.id] = a.name; ACCOUNT_LETTER[a.id] = a.letter; ACCOUNT_COLOR[a.id] = `var(--series-${a.color})`;
      if (a.ink) ACCOUNT_INK[a.id] = a.ink;
    }
  } catch (e) { /* the pages still work: an account's id stands in for its name */ }
}
const acctColor = id => ACCOUNT_COLOR[id] || 'var(--series-5)';
const acctName = id => ACCOUNT_NAME[id] || id;
// An account in a row or a table: its letter in its colour (acctMark, with the Appearance pane).
const acctDot = id => acctMark(id);
function acctLegend(ids) {
  return h('div', {class: 'legend acct-legend'}, ids.map(id => h('span', null, acctMark(id), acctName(id))));
}
// The legend for rows that carry an account colour: the accounts those rows come from.
const rowsLegend = rows => rows.length ? acctLegend([...new Set(rows.map(m => m.account_id).filter(Boolean))].sort(byAccount)) : null;
// Accounts in a fixed order (accounts.json's), so the chips never jump; unknown ones after, by id.
const byAccount = (a, b) => ((ACCOUNT_ORDER.indexOf(a) + 1 || 99) - (ACCOUNT_ORDER.indexOf(b) + 1 || 99)) || String(a).localeCompare(String(b));
// Messages opens with every account on except these, and Reset returns to it. Excluding (rather
// than listing the ones to include) means a newly added account shows up on its own.
// Mail and Teams are places of their own (30 September 2026): Mail lists e-mail only (medium=email), so no
// account is left out by default any more; Teams has its own place, filters and list.
const DEFAULT_EXCLUDED_ACCOUNTS = [];
function card(title, sub, right, ...body) {
  return h('section', {class: 'card'}, h('div', {class: 'card-h'}, h('div', null, h('h2', null, title), sub ? h('p', null, sub) : null), right || null), ...body);
}
// ---- actions (docs/design.md, Controls): three kinds of button, main (filled), act (coloured outline) and
// quiet (grey outline), each with a line symbol and, where it has one, its shortcut letter. Related actions sit
// in an outlined group with a small title. o: color (--ac), key, on (pressed), title.
function abtn(kind, sym, label, onclick, o = {}) {
  return h('button', {class: `ab ab-${kind}` + (o.on ? ' on' : ''), style: o.color ? `--ac:${o.color}` : null, onclick, title: o.title || null,
      'aria-pressed': o.pressed != null ? String(!!o.pressed) : null, 'data-key': o.key || null, 'aria-keyshortcuts': o.key ? o.key.toUpperCase() : null},
    icon(sym), h('span', null, label), o.key ? h('kbd', {class: 'ak', 'aria-hidden': 'true'}, o.key.toUpperCase()) : null);
}
function agroup(title, kids, o = {}) {
  return h('fieldset', {class: 'agrp' + (o.cls ? ' ' + o.cls : '')}, h('legend', null, title), h('div', {class: 'agrp-b'}, kids),
    o.note ? h('div', {class: 'agrp-n'}, o.note) : null);
}
// The message pane's shortcut letters: one key each, while a message or a conversation is in the pane.
const MESSAGE_KEYS = {reply: 'r', reply_all: 'a', forward: 'f', work: 'w', link: 'l', important: 'i', not_important: 'n', conversation: 'c', binder: 'b'};
document.addEventListener('keydown', e => {
  if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || e.key.length !== 1 || menuEl) return;
  if (!PANE.open || !PANE.cur || !/^[mt]:/.test(PANE.cur.key || '') || ['gold', 'goldcheck', 'discovery', 'studio'].includes(S.view)) return;
  // Never while something waits for a confirmation to send or post (promise 1): a stray key must not land there.
  if (PANE.body.querySelector('.tconfirm') || document.querySelector('dialog[open]')) return;
  const t = e.target;
  if (t && (t.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName))) return;
  // In a conversation every open message has its own actions: the key goes to the last one open, the newest.
  const btns = [...PANE.body.querySelectorAll(`[data-key="${CSS.escape(e.key.toLowerCase())}"]`)].filter(b => b.offsetParent && !b.disabled);
  if (!btns.length) return;
  e.preventDefault();
  btns[btns.length - 1].click();
});
function header(title, sub, right) {
  return h('div', {class: 'head'}, h('div', null, h('h1', null, title), h('div', {class: 'sub'}, sub)), h('div', {class: 'head-r'}, right || null));
}
function seg(opts, cur, on) {
  return h('div', {class: 'seg', role: 'group'}, opts.map(([v, l]) => h('button', {'aria-pressed': String(v === cur), onclick: () => on(v)}, l)));
}
function note(text) {
  return h('div', {class: 'note'}, h('span', {class: 'i', 'aria-hidden': 'true'}, 'i'), h('div', null, text));
}
function empty(title, text) { return h('div', {class: 'empty'}, h('b', null, title), text); }
// The mark, larger, for a page with nothing on it yet: the beacon in its calm colour.
const heroMark = cls => beaconMark('hero-mark' + (cls ? ' ' + cls : ''));
function emptyPage(title, text) { return h('div', {class: 'empty empty-page'}, heroMark(), h('b', null, title), text); }
// A title with the manual's ⓘ after it (helpBtn, the live manual below).
const withHelp = (title, id) => [title, helpBtn(id)];

// ---------------------------------------------------------------- the live manual
// docs/manual.md (talos.manual): the field guide's descriptions of places and controls. A small ⓘ beside a
// thing the manual describes opens its section over the page, in a <dialog>: the page stays where it was,
// dimmed behind it. A click outside, × or Esc closes it. Only what the manual describes gets an ⓘ, and
// tests/test_manual.py holds every helpBtn id to a section.
let MANUAL = null;  // the sections, fetched on the first ⓘ
function helpBtn(id) {
  return h('button', {class: 'help-i', type: 'button', 'aria-haspopup': 'dialog', 'aria-label': 'What is this? Opens the manual',
    title: 'What is this?', 'data-help': id,
    // A title may sit in a row that opens on a click or on Enter; the ⓘ is its own button.
    onclick: e => { e.preventDefault(); e.stopPropagation(); openManual(id, e.currentTarget); },
    onkeydown: e => e.stopPropagation()}, 'i');
}
function manualBlock(b) {
  if (b.kind === 'h') return h('h3', null, mdInline(b.text));
  if (b.kind === 'ul' || b.kind === 'ol') return h(b.kind, null, b.items.map(t => h('li', null, mdInline(t))));
  if (b.kind === 'tip') return h('div', {class: 'man-tip'}, mdInline(b.text));
  return h('p', null, mdInline(b.text));
}
async function openManual(id, from) {
  try { MANUAL = MANUAL || await fetchJSON('/api/manual'); } catch (e) { flash(errText(e)); return; }
  const secs = MANUAL;
  if (!secs[id]) { flash('The manual has nothing on this yet.'); return; }
  document.querySelectorAll('dialog.manual').forEach(d => { d.close(); d.remove(); });
  const kicker = h('div', {class: 'man-k'}), title = h('h2', {id: 'man-h'}), body = h('div', {class: 'man-b', tabindex: '-1'});
  const trail = [];  // the sections walked through See also, for Back
  const back = h('button', {class: 'btn ghost sm', type: 'button', onclick: () => { trail.pop(); show(trail[trail.length - 1]); }}, '‹ Back');
  const show = sid => {
    const sec = secs[sid], see = sec.see.filter(x => secs[x]);
    fill(kicker, 'Manual', sec.guide ? ` · field guide, ${sec.guide}` : '');
    fill(title, sec.title);
    fill(body, sec.blocks.map(manualBlock),
      see.length ? h('div', {class: 'man-see'}, h('span', {class: 'man-see-l'}, 'See also'),
        see.map(x => h('button', {class: 'fc', type: 'button', onclick: () => { trail.push(x); show(x); }}, secs[x].title))) : null);
    back.hidden = trail.length < 2;
    body.scrollTop = 0;
  };
  // A click lands on the dialog itself only outside its box (on the backdrop); pressing inside and
  // letting go outside (selecting text) is not a click outside.
  let downOutside = false;
  const dlg = h('dialog', {class: 'manual', 'aria-labelledby': 'man-h',
      onpointerdown: e => { downOutside = e.target === dlg; },
      onclick: e => { if (e.target === dlg && downOutside) close(); },
      // Keys stay in the dialog: Esc closes it and not the reading pane behind it, and list keys don't move the page.
      onkeydown: e => { e.stopPropagation(); if (e.key === 'Escape') { e.preventDefault(); close(); } },
      oncancel: e => { e.preventDefault(); close(); }},
    h('div', {class: 'man-head'}, h('div', null, kicker, title),
      h('button', {class: 'man-x', type: 'button', 'aria-label': 'Close the manual', title: 'Close (Esc)', onclick: () => close()}, '×')),
    body,
    h('div', {class: 'man-foot'}, back, h('span', null, 'If the manual and the screen disagree, the screen wins.')));
  const close = () => { dlg.close(); dlg.remove(); if (from && from.isConnected) from.focus({preventScroll: true}); };
  trail.push(id);
  show(id);
  document.body.append(dlg);
  dlg.showModal();
  body.focus({preventScroll: true});
}

// ---------------------------------------------------------------- state
const VIEWS = ['studio', 'today', 'teams', 'space', 'discover', 'work', 'calendar', 'timeline', 'overview', 'messages', 'objects', 'events', 'rules', 'gold', 'goldcheck', 'accept', 'jobs', 'changesets', 'structure', 'sources', 'argus', 'clusters', 'discovery', 'releases'];
const S = {view: 'today', obj: null, objNested: '', objKind: '', objArchived: '', fs: {mail: defaults(), teams: teamsDefaults()}};
// S.f is the filters of the list in view: Mail's, or Teams's own, so the two never mix (each keeps its
// selection while the owner is in the other). Everything that reads or sets S.f keeps working as it did.
const fScope = () => S.view === 'teams' ? 'teams' : 'mail';
Object.defineProperty(S, 'f', {get() { return S.fs[fScope()]; }, set(v) { S.fs[fScope()] = v; }});
// '#objects/12' opens object 12, '#goldcheck/5' the fifth item of the check; every other hash is a view name.
function fromHash() {
  const [v, id] = location.hash.slice(1).split('/');
  return VIEWS.includes(v) ? {v, obj: (v === 'objects' || v === 'goldcheck') && /^\d+$/.test(id || '') ? Number(id) : null} : null;
}
{ const r = fromHash(); if (r) { S.view = r.v; S.obj = r.obj; } }

function go(v, obj) {
  if (v === 'objects' && (obj || null) !== S.obj) S.objKind = '';
  S.view = v; S.obj = obj || null;
  const hb = hubOf(v);
  if (hb) rememberTab(hb, v);
  try { history.replaceState(null, '', '#' + v + (S.obj ? '/' + S.obj : '')); } catch (e) {}
  closePane(false);
  render();
  scrollTo(0, 0);
}
window.addEventListener('hashchange', () => { const r = fromHash(); if (r && (r.v !== S.view || r.obj !== S.obj)) { S.view = r.v; S.obj = r.obj; closePane(false); render(); } });

// ---------------------------------------------------------------- the places: the rail, the hub tabs, the phone's tab bar
// Six places and a room for tuning (docs/design.md). Each place is a hub of one or more views; a
// hub with several shows them as tabs above the view. The views keep their own names in the hash
// (#rules, #accept …), so every old link still works.
const ICONS = {
  today: 'M12 8a4 4 0 1 0 0 8a4 4 0 1 0 0-8zM12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4',
  space: 'M3 3h8v8H3zM13 3h8v5h-8zM13 10h8v11h-8zM3 13h8v8H3z',
  discover: 'M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18zM15.5 8.5l-2 5-5 2 2-5z',
  work: 'M4 4h4v16H4zM10 4h4v10h-4zM16 4h4v13h-4z',
  calendar: 'M4 6h16v14H4zM4 10h16M8 3v5M16 3v5M8 14h2M14 14h2M8 17h2',
  timeline: 'M3 4v16M6 6h8M9 11h11M6 16h6M16 16h2',
  overview: 'M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z',
  messages: 'M3 6h18v13H3zM3.5 6.5l8.5 6.5 8.5-6.5',
  teams: 'M4 5h12v9H8l-4 3zM16 9h4v8l-3-2h-6v-1',
  binders: 'M5 3h12a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5zM9 3v18M12.5 8h3.5',
  objects: 'M12 3l8 4.5v9L12 21l-8-4.5v-9zM4 7.5l8 4.5 8-4.5M12 12v9',
  events: 'M2 12h4l3-8 6 16 3-8h4',
  tune: 'M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1M15 4v4M9 10v4M17 16v4',
  rules: 'M4 6h16M4 12h16M4 18h16M9 4v4M15 10v4M7 16v4',
  gold: 'M8 16a4 4 0 1 1 0-8a4 4 0 1 1 0 8zM12 12h9M18 12v3M21 12v4',
  accept: 'M4 20h16M6 16V9M12 16V4M18 16v-5M3 12h18',
  changesets: 'M4 4h4v16H4zM10 4h4v10h-4zM16 4h4v13h-4z',
  structure: 'M4 4h6v4H4zM14 10h6v4h-6zM14 17h6v4h-6zM7 8v11h7M7 12h7',
  sources: 'M9 2v6M15 2v6M6 8h12v4a6 6 0 0 1-12 0zM12 18v4',
  clusters: 'M4 7a3 3 0 1 0 6 0a3 3 0 1 0-6 0M14 7a3 3 0 1 0 6 0a3 3 0 1 0-6 0M9 16a3 3 0 1 0 6 0a3 3 0 1 0-6 0',
  discovery: 'M4 12l4 4 8-9M3 5h7M3 19h18M14 12h7',
  argus: 'M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12zM12 9a3 3 0 1 0 0 6a3 3 0 1 0 0-6z',
  search: 'M10.5 4a6.5 6.5 0 1 0 0 13a6.5 6.5 0 1 0 0-13zM15.5 15.5L20 20',
  appearance: 'M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18zM12 3v18M12 7.5h6M12 12h8.5M12 16.5h6',
  more: 'M5 12h.01M12 12h.01M19 12h.01',
  // the action symbols (docs/design.md, Controls)
  reply: 'M9 14L4 9l5-5M4 9h10a6 6 0 0 1 6 6v3',
  replyall: 'M7 14L2 9l5-5M12 14L7 9l5-5M7 9h7a6 6 0 0 1 6 6v3',
  forward: 'M15 14l5-5-5-5M20 9H10a6 6 0 0 0-6 6v3',
  important: 'M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18zM12 7.5v6M12 16.5v.5',
  notimp: 'M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18zM8 12h8',
  question: 'M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18zM9.5 9.5a2.5 2.5 0 1 1 3.6 2.2c-.7.4-1.1.9-1.1 1.8M12 16.5v.5',
  workitem: 'M4 4h16v16H4zM8 12l3 3 5-6',
  link: 'M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1',
  binderadd: 'M5 3h11a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5zM9 3v18M14 10v6M11 13h6',
  convo: 'M4 5h16v10H10l-6 4z',
  takein: 'M4 14v5h16v-5M12 4v10M8 10l4 4 4-4',
  pause: 'M9 5v14M15 5v14',
  check: 'M5 12l5 5 9-10',
  remove: 'M6 6l12 12M18 6L6 18',
  raw: 'M8 4h8l4 4v12H4V4zM9 13l-2 2 2 2M15 13l2 2-2 2',
  theme: 'M12 3a9 9 0 1 0 9 9 7 7 0 0 1-9-9z',
  pane: 'M4 5h16v14H4zM14 5v14',
  maximise: 'M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7',
  restore: 'M20 10h-6V4M4 14h6v6M14 10l6-6M10 14l-6 6'
};
const icon = k => s('svg', {class: 'ico' + (k === 'more' ? ' ico-dots' : ''), viewBox: '0 0 24 24', 'aria-hidden': 'true'}, s('path', {d: ICONS[k]}));
// A tab is a view, or a view in a mode ('work:list'); Tune's tabs come in groups.
const HUBS = [
  {id: 'today', label: 'Today', icon: 'today', tabs: [['today', 'Today']]},
  {id: 'mail', label: 'Mail', icon: 'messages', tabs: [['messages', 'Mail']]},
  {id: 'teams', label: 'Teams', icon: 'teams', tabs: [['teams', 'Teams']]},
  {id: 'work', label: 'Work', icon: 'work', tabs: [['work:board', 'Board'], ['work:list', 'List'], ['timeline', 'Timeline']]},
  {id: 'calendar', label: 'Calendar', icon: 'calendar', tabs: [['calendar', 'Calendar']]},
  {id: 'binders', label: 'Binders', icon: 'binders', tabs: [['space', 'Areas'], ['objects', 'All binders']]},
  {id: 'discover', label: 'Discover', icon: 'discover', tabs: [['discover', 'Patterns'], ['events', 'Events'], ['overview', 'Insights']]},
  {id: 'tune', label: 'Tune', icon: 'tune', groups: [
    ['Classify', [['studio', 'Studio'], ['gold', 'Answer key'], ['accept', 'Acceptance'], ['jobs', 'Jobs & fruit']]],
    ['Rules', [['rules', 'Rules'], ['structure', 'Structure'], ['changesets', 'Changesets']]],
    ['Sources & health', [['sources', 'Sources'], ['clusters', 'Clusters'], ['discovery', 'Systems'], ['argus', 'Argus']]]]},
];
HUBS.forEach(hb => { if (hb.groups) hb.tabs = hb.groups.flatMap(([, t]) => t); });
const tabView = id => id.split(':')[0];
const VIEW_HUB = {};
HUBS.forEach(hb => hb.tabs.forEach(([id]) => { VIEW_HUB[tabView(id)] = VIEW_HUB[tabView(id)] || hb; }));
VIEW_HUB.goldcheck = VIEW_HUB.gold;
const hubOf = v => VIEW_HUB[v] || null;
const VIEW_NAME = {...Object.fromEntries(HUBS.flatMap(hb => hb.tabs.map(([id, l]) => [tabView(id), l]))),
                   work: 'Work', messages: 'Mail', teams: 'Teams', goldcheck: 'Answer key check', releases: 'Release notes'};
// Each hub opens on the tab the owner was last on, in this browser.
let HUB_LAST = {};
try { const x = JSON.parse(localStorage.getItem('talos-hub-last') || '{}'); if (x && typeof x === 'object') HUB_LAST = x; } catch (e) {}
function rememberTab(hb, v) {
  if (HUB_LAST[hb.id] === v) return;
  HUB_LAST = {...HUB_LAST, [hb.id]: v};
  try { localStorage.setItem('talos-hub-last', JSON.stringify(HUB_LAST)); } catch (e) {}
}
// Mail from the rail, the tab bar or Search always starts from its default view (received mail, every account
// but DEFAULT_EXCLUDED_ACCOUNTS); the detours that open Mail on a selection (a watcher, a sender, a thread)
// set S.f themselves and call go('messages') directly.
function freshMail() { S.f = defaults(); SIDE.all = false; }
function goHub(hb) {
  if (hb.id === 'mail') freshMail();
  if (hb.id === 'teams') freshTeams();
  const last = HUB_LAST[hb.id];
  go(last && hb.tabs.some(([id]) => tabView(id) === last) ? last : tabView(hb.tabs[0][0]));
}
function tabCurrent(id) {
  const [v, mode] = id.split(':');
  return (S.view === v || (v === 'gold' && S.view === 'goldcheck')) && (!mode || WORK.mode === mode);
}
function goTab(id) {
  const [v, mode] = id.split(':');
  if (mode) { WORK.mode = mode; try { localStorage.setItem('talos-work-mode', mode); } catch (e) {} }
  if (S.view === v) render(); else go(v);
}
// The tabs of the hub the view is in, above the view (none for a hub of one view).
function hubTabs() {
  const hb = hubOf(S.view);
  if (!hb || hb.tabs.length < 2) return null;
  const tab = ([id, l]) => h('a', {class: 'htab', href: '#' + tabView(id), 'aria-current': tabCurrent(id) ? 'page' : null,
    onclick: e => { e.preventDefault(); goTab(id); }}, l);
  return h('nav', {class: 'htabs' + (hb.groups ? ' grouped' : ''), 'aria-label': hb.label},
    hb.groups ? hb.groups.map(([g, tabs]) => h('div', {class: 'htg', role: 'group', 'aria-label': g}, h('span', {class: 'htg-h', 'aria-hidden': 'true'}, g), tabs.map(tab)))
      : hb.tabs.map(tab));
}
// A number beside a place: only where there is something to act on (Today: what needs the owner).
const RAIL_BADGE = {today: null};
// What needs the owner: attention that is more than "in the inbox", plus watchers with news when known.
const needsCount = (att, watchers) => att.filter(r => !r.reasons.every(x => x === 'inbox')).length + (watchers || []).filter(w => w.fresh && !w.paused).length;
function refreshBadges() {
  Promise.all([api('/api/work/attention?limit=100'), api('/api/importance/today?limit=20&hours=24').catch(() => []), api('/argus/status').catch(() => null)])
    .then(([att, important, argus]) => {
      const n = Array.isArray(att) ? needsCount(att) : null;
      const lit = setBeacon(beaconLevel(att, important, argus));
      if (n !== RAIL_BADGE.today || lit) { RAIL_BADGE.today = n; renderRail(); }
    }).catch(() => {});
}
// ---- the beacon's light says whether anything needs a look: calm (the accent), attention (amber: important
// mail today, a review due, a service late) or alarm (red: overdue work, a service down or failing). Pure, so a
// test can run it. Only the mark in the rail takes the colour; the tab's icon stays as it is.
function beaconLevel(att, important, argus) {
  const count = (n, one, many) => n ? [`${n} ${n === 1 ? one : many}`] : [];
  const has = k => (att || []).filter(r => (r.reasons || []).includes(k)).length;
  const svc = st => ((argus && argus.services) || []).filter(v => st.includes(v.status)).length;
  const high = (important || []).filter(m => m.level === 'high').length;
  const alarm = [...count(has('overdue'), 'work item overdue', 'work items overdue'), ...count(svc(['down', 'failing']), 'service down', 'services down')];
  const attn = [...count(high, 'important mail today', 'important mails today'), ...count(has('review'), 'review due', 'reviews due'), ...count(svc(['late']), 'service late', 'services late')];
  return {level: alarm.length ? 'alarm' : attn.length ? 'attn' : 'calm', why: [...alarm, ...attn]};
}
const BEACON = {level: 'calm', why: []};
function setBeacon(b) {
  const changed = b.level !== BEACON.level || b.why.join() !== BEACON.why.join();
  Object.assign(BEACON, b);
  return changed;
}
setInterval(() => { if (!document.hidden) refreshBadges(); }, 5 * 60e3);
// Search cuts across the pages: it opens Mail with the search field in focus (⌘K or Ctrl+K anywhere).
function openSearch() {
  if (S.view !== 'messages') { freshMail(); go('messages'); }
  const focus = (n = 0) => { const q = document.getElementById('q'); if (q) { q.focus(); q.select(); } else if (n < 40) setTimeout(() => focus(n + 1), 50); };
  focus();
}
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'k') { e.preventDefault(); openSearch(); }
});
// The places by number (1 Today, 2 Mail … in the rail's order, the number shown beside each), "[" and "]" for the
// place's tabs, "/" for search. Not while typing, not in the views that use keys of their own (the answer key, its
// check, Systems), and never while a menu, a dialog or a send or post confirmation is open.
const placeKey = hb => HUBS.indexOf(hb) < 9 ? String(HUBS.indexOf(hb) + 1) : null;
document.addEventListener('keydown', e => {
  if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || menuEl) return;
  if (['gold', 'goldcheck', 'discovery', 'studio'].includes(S.view) || document.querySelector('dialog[open]') || (PANE.open && PANE.body.querySelector('.tconfirm'))) return;
  const t = e.target;
  if (t && (t.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName))) return;
  const hb = HUBS.find(x => placeKey(x) === e.key);
  if (hb) { e.preventDefault(); goHub(hb); return; }
  if (e.key === '/') { e.preventDefault(); openSearch(); return; }
  if (e.key === '[' || e.key === ']') {
    const cur = hubOf(S.view);
    if (!cur || cur.tabs.length < 2) return;
    const i = cur.tabs.findIndex(([id]) => tabCurrent(id));
    const next = cur.tabs[(Math.max(0, i) + (e.key === ']' ? 1 : cur.tabs.length - 1)) % cur.tabs.length];
    e.preventDefault();
    goTab(next[0]);
  }
});
// The mark: a beacon keeping watch (Talos is often shown beside a lighthouse), drawn in the theme's colours:
// the tower in the text colour, the lamp and its beams in the beacon's colour (beaconLevel). On hover the beams sweep.
function beaconMark(cls = 'brand-mark') {
  const cut = d => s('path', {d, fill: 'none', stroke: 'black', 'stroke-width': '20', 'stroke-linecap': 'round'});
  return s('svg', {class: 'beacon ' + cls, viewBox: '0 0 512 512', 'aria-hidden': 'true'},
    s('defs', null, s('mask', {id: 'beacon-cut', maskUnits: 'userSpaceOnUse', x: '0', y: '0', width: '512', height: '512'},
      s('rect', {width: '512', height: '512', fill: 'white'}),
      s('rect', {x: '228', y: '140', width: '56', height: '40', rx: '6', fill: 'black'}),
      cut('M198 316 H314 M190 392 H322'))),
    s('g', {mask: 'url(#beacon-cut)'},
      s('path', {class: 'bm-beam bm-beam-l', d: 'M204 136 L70 96 L70 190 Z'}), s('path', {class: 'bm-beam bm-beam-r', d: 'M308 136 L442 96 L442 190 Z'}),
      s('g', {class: 'bm-tower'}, s('path', {d: 'M196 124 L256 64 L316 124 Z'}), s('rect', {x: '204', y: '124', width: '104', height: '72'}),
        s('rect', {x: '170', y: '196', width: '172', height: '34', rx: '6'}), s('path', {d: 'M208 230 H304 L330 468 H182 Z'}))),
    s('rect', {class: 'bm-lamp', x: '228', y: '140', width: '56', height: '40', rx: '6'}));
}
const onThisMac = () => location.hostname === '127.0.0.1' || location.hostname === 'localhost';
function renderRail() {
  const cur = hubOf(S.view);
  const place = hb => h('a', {class: 'nav-a', href: '#' + tabView(hb.tabs[0][0]), 'aria-current': cur === hb ? 'page' : null,
      title: placeKey(hb) ? `${hb.label} (${placeKey(hb)})` : hb.label, 'aria-keyshortcuts': placeKey(hb) || null,
      onpointerenter: () => prefetchSoon(hb), onfocus: () => prefetchSoon(hb), onpointerleave: () => clearTimeout(hoverTimer),
      onclick: e => { e.preventDefault(); goHub(hb); }},
    icon(hb.icon), h('span', null, hb.label), RAIL_BADGE[hb.id] ? h('span', {class: 'badge', title: `${RAIL_BADGE[hb.id]} things need you`}, fmt(RAIL_BADGE[hb.id])) : null,
    placeKey(hb) ? h('kbd', {class: 'nav-k', 'aria-hidden': 'true'}, placeKey(hb)) : null);
  const tune = HUBS.find(hb => hb.id === 'tune');
  $rail.replaceChildren();
  add($rail, [
    h('a', {class: 'brand bc-' + BEACON.level, href: '#today', title: 'Talos · ' + (BEACON.why.join(', ') || 'all calm'),
        'aria-label': 'Talos: go to Today. ' + (BEACON.why.join(', ') || 'All calm'), onclick: e => { e.preventDefault(); go('today'); }},
      beaconMark(), h('b', null, 'Talos')),
    h('button', {class: 'nav-a nav-search', title: 'Search mail (⌘K or /)', 'aria-keyshortcuts': 'Meta+K /', onclick: openSearch}, icon('search'), h('span', null, 'Search'), h('kbd', {class: 'badge kbd'}, '⌘K')),
    h('div', {class: 'navsec'}, HUBS.filter(hb => hb !== tune).map(place)),
    h('div', {class: 'navsec nav-tune'}, place(tune)),
    h('div', {class: 'rail-foot'},
      h('button', {class: 'nav-a', 'aria-expanded': PANE.open && PANE.cur && PANE.cur.key === 'look' ? 'true' : 'false', onclick: openAppearance}, icon('appearance'), h('span', null, 'Appearance')),
      h('div', {class: 'foot-row'},
        RELEASE.version ? h('a', {class: 'ver', href: '#releases', 'aria-current': S.view === 'releases' ? 'page' : null,
            title: RELEASE.fresh ? 'New in this version: see the release notes' : 'Release notes',
            onclick: e => { e.preventDefault(); go('releases'); }},
          `Talos ${RELEASE.version}`, RELEASE.fresh ? h('span', {class: 'ver-new', 'aria-label': 'new'}) : null) : null,
        h('span', {class: 'meta-foot'}, onThisMac() ? 'On this Mac' : 'Over Tailscale')),
      h('button', {class: 'btn ghost sm signout', title: 'End this session. You sign in again with your password and a code.', onclick: signOut}, 'Sign out'))
  ]);
  renderTabbar(cur);
}
// On a phone the rail gives way to a tab bar at the bottom: four places and More.
let $tabbar = null;
function renderTabbar(cur) {
  if (!$tabbar) { $tabbar = h('nav', {class: 'tabbar', 'aria-label': 'Places'}); document.body.append($tabbar); }
  const main = ['today', 'mail', 'work', 'calendar'].map(id => HUBS.find(hb => hb.id === id));
  const moreOn = cur && !main.includes(cur);
  const more = h('button', {class: 'tb', 'aria-haspopup': 'menu', 'aria-current': moreOn ? 'page' : null, onclick: () => moreMenu(more)},
    icon('more'), h('span', null, moreOn ? cur.label : 'More'));
  fill($tabbar, main.map(hb => h('a', {class: 'tb', href: '#' + tabView(hb.tabs[0][0]), 'aria-current': cur === hb ? 'page' : null,
      onclick: e => { e.preventDefault(); goHub(hb); }},
    icon(hb.icon), h('span', null, hb.label), RAIL_BADGE[hb.id] ? h('span', {class: 'tb-n'}, fmt(RAIL_BADGE[hb.id])) : null)), more);
}
function moreMenu(anchor) {
  if (menuEl) { closeMenu(true); return; }
  menuReturn = anchor;
  const item = (label, fn, ic) => h('button', {class: 'menu-item mi-row', role: 'menuitem', tabindex: '-1', onclick: () => { closeMenu(); fn(); }}, ic ? icon(ic) : null, label);
  menuEl = h('div', {class: 'menu more-menu', role: 'menu', 'aria-label': 'More places', onkeydown: menuKeys},
    ['binders', 'discover', 'tune'].map(id => { const hb = HUBS.find(x => x.id === id); return item(hb.label, () => goHub(hb), hb.icon); }),
    h('div', {class: 'menu-sep', role: 'separator'}),
    item('Search', openSearch, 'search'), item('Appearance', openAppearance, 'appearance'),
    RELEASE.version ? item(`Release notes · ${RELEASE.version}`, () => go('releases')) : null,
    item('Sign out', signOut));
  document.body.append(menuEl);
  const rc = anchor.getBoundingClientRect();
  menuEl.style.right = '8px';
  menuEl.style.bottom = (innerHeight - rc.top + 6) + 'px';
  menuEl.querySelector('.menu-item').focus();
}

// ---------------------------------------------------------------- appearance: the theme manager
// How Talos looks, and nothing else: a preset (surfaces, text, accent), light or dark, an accent of
// their own, text size, headings, density, corners, how accounts are marked, and motion. Kept in this
// browser (talos-look); index.html applies it before the first paint, applyLook() after a change.
// Every setting is a data-* attribute on <html>; style.css turns each into tokens.
const LOOK_DEFAULT = {mode: 'system', preset: 'talos', accent: '', size: '0', headings: 'sans', density: 'comfortable', corners: 'soft', marker: 'letter', motion: 'full', tint: 'subtle', controls: 'clear'};
const LOOK_KEYS = Object.keys(LOOK_DEFAULT);
let LOOK = {...LOOK_DEFAULT};
try {
  const x = JSON.parse(localStorage.getItem('talos-look') || 'null');
  if (x && typeof x === 'object') { for (const k of LOOK_KEYS) if (typeof x[k] === 'string') LOOK[k] = x[k]; }
  else { const t = localStorage.getItem('talos-theme'); if (t === 'light' || t === 'dark') LOOK.mode = t; }  // the old mode switch
} catch (e) {}
// The presets as the Appearance pane draws their tiles: plane, surface, text and accent, light then dark.
const PRESETS = [
  ['talos', 'Talos', 'Warm grey, blue', ['#f8f8f6', '#ffffff', '#111110', '#2f6fc4'], ['#0e0e0d', '#232322', '#f5f5f3', '#6ea3ec']],
  ['slate', 'Slate', 'Cool and crisp', ['#f5f7fa', '#ffffff', '#0f1720', '#4f55c9'], ['#0b0f14', '#1a222b', '#eef2f6', '#8e93f0']],
  ['paper', 'Paper', 'Warm, for reading', ['#f3eee4', '#fffdf8', '#1f1a14', '#16727a'], ['#16130f', '#29241e', '#f3ece0', '#4fb8c0']],
  ['graphite', 'Graphite', 'High contrast', ['#ffffff', '#f2f2f2', '#000000', '#222222'], ['#000000', '#181818', '#ffffff', '#e6e6e6']],
  ['midnight', 'Midnight', 'Deep blue nights', ['#f4f5fa', '#ffffff', '#111427', '#2f6fc4'], ['#0b1020', '#19203f', '#e8ebf7', '#6ea3ec']],
  ['bronze', 'Bronze', 'The icon’s colour', ['#f7f5f1', '#ffffff', '#16130f', '#8f5a24'], ['#100e0b', '#24201b', '#f4efe8', '#d19a5c']],
];
const ACCENTS = [['blue', 'Blue', '#2f6fc4', '#6ea3ec'], ['indigo', 'Indigo', '#4f55c9', '#8e93f0'], ['teal', 'Teal', '#16727a', '#4fb8c0'],
                 ['bronze', 'Bronze', '#8f5a24', '#d19a5c'], ['plum', 'Plum', '#8a3f86', '#cf8ccb'], ['graphite', 'Graphite', '#222222', '#e6e6e6']];
const darkNow = () => LOOK.mode === 'dark' || (LOOK.mode !== 'light' && matchMedia('(prefers-color-scheme: dark)').matches);
function applyLook() {
  const r = document.documentElement;
  if (LOOK.mode === 'light' || LOOK.mode === 'dark') r.setAttribute('data-theme', LOOK.mode); else r.removeAttribute('data-theme');
  for (const k of LOOK_KEYS.slice(1)) { const v = LOOK[k]; if (!v || v === LOOK_DEFAULT[k]) r.removeAttribute('data-' + k); else r.setAttribute('data-' + k, v); }
  // The browser's own bar (and the phone's status bar) takes the plane of the theme.
  const plane = resolveColor('--plane');
  if (plane) document.querySelectorAll('meta[name="theme-color"]').forEach(m => m.setAttribute('content', plane));
}
function saveLook(patch) {
  LOOK = {...LOOK, ...patch};
  try { localStorage.setItem('talos-look', JSON.stringify(LOOK)); localStorage.removeItem('talos-theme'); } catch (e) {}
  document.documentElement.classList.add('look-changing');
  applyLook();
  setTimeout(() => document.documentElement.classList.remove('look-changing'), 260);
}
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { applyLook(); if (PANE.open && PANE.cur && PANE.cur.key === 'look') openAppearance(true); });
// A token's colour as the page shows it now (light-dark() resolved), as rgb().
function resolveColor(token) {
  const probe = h('span', {style: `position:absolute;visibility:hidden;color:var(${token})`});
  document.body.append(probe);
  const c = getComputedStyle(probe).color;
  probe.remove();
  return c;
}
function contrastOf(a, b) {
  const lum = c => { const m = (c.match(/[\d.]+/g) || []).slice(0, 3).map(Number).map(v => v / 255).map(v => v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)); return 0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2]; };
  const x = lum(a), y = lum(b);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}
function openAppearance(replace) {
  openPane(appearanceForm(), {key: 'look', title: 'Appearance', replace: replace === true});
  renderRail();
}
function appearanceForm() {
  const dark = darkNow();
  const redraw = () => openAppearance(true);
  const set = (k, v) => { saveLook({[k]: v}); redraw(); };
  const lookSeg = (k, opts) => { const x = seg(opts, LOOK[k], v => set(k, v)); x.classList.add('seg-fill'); return x; };
  const field = (label, control, note) => h('div', {class: 'lk-f'}, h('div', {class: 'lk-l'}, h('span', null, label), note ? h('span', {class: 'muted'}, note) : null), control);
  const presetTile = ([id, name, sub, light, darkc]) => {
    const c = dark ? darkc : light, on = LOOK.preset === id;
    return h('button', {class: 'lk-tile', 'aria-pressed': String(on), onclick: () => { saveLook({preset: id, accent: ''}); redraw(); }},
      h('span', {class: 'lk-sw', 'aria-hidden': 'true'}, c.map(x => h('i', {style: `background:${x}`}))),
      h('b', null, name), h('small', null, sub));
  };
  const accent = ([id, name, l, d]) => h('button', {class: 'lk-acc', 'aria-pressed': String(LOOK.accent === id), 'aria-label': name, title: name,
    style: `background:${dark ? d : l}`, onclick: () => set('accent', id)});
  const txt = resolveColor('--text-primary'), muted = resolveColor('--text-muted'), plane = resolveColor('--plane'),
        acc = resolveColor('--accent'), ink = resolveColor('--accent-ink');
  const grade = (label, v) => [h('span', null, label), h('b', {class: 'num'}, v.toFixed(1) + ':1'),
    h('span', {class: v >= 4.5 ? 'lk-ok' : 'lk-no'}, v >= 4.5 ? 'AA' : 'below AA')];
  const preset = PRESETS.find(p => p[0] === LOOK.preset) || PRESETS[0];
  return h('div', {class: 'look'},
    h('p', {class: 'lk-intro'}, 'How Talos looks, and nothing else: mail, rules and work are untouched. It applies at once and is kept in this browser.'),
    field('Mode', lookSeg('mode', [['system', 'System'], ['light', 'Light'], ['dark', 'Dark']]), LOOK.mode === 'system' ? (dark ? 'follows the system: dark now' : 'follows the system: light now') : null),
    field('Theme', h('div', {class: 'lk-tiles'}, PRESETS.map(presetTile)), `${preset[1]}, ${dark ? 'dark' : 'light'}`),
    field('Accent', h('div', {class: 'lk-accs'},
      h('button', {class: 'lk-own', 'aria-pressed': String(!LOOK.accent), onclick: () => set('accent', '')}, 'The theme’s own'),
      ACCENTS.map(accent)), 'buttons, links, selection'),
    h('div', {class: 'lk-grid'},
      field('Text size', lookSeg('size', [['0', 'Default'], ['1', 'Larger'], ['2', 'Largest']])),
      field('Headings', lookSeg('headings', [['sans', 'Sans'], ['serif', 'Serif']])),
      field('Density', lookSeg('density', [['comfortable', 'Comfortable'], ['compact', 'Compact']])),
      field('Corners', lookSeg('corners', [['square', 'Square'], ['soft', 'Soft'], ['round', 'Round']])),
      field('Motion', lookSeg('motion', [['full', 'Full'], ['reduced', 'Reduced'], ['off', 'Off']])),
      field('Colour', lookSeg('tint', [['off', 'Off'], ['subtle', 'Subtle'], ['more', 'More']])),
      field('Controls', lookSeg('controls', [['clear', 'Clear'], ['quiet', 'Quiet']]))),
    field('Accounts in lists', lookSeg('marker', [['letter', 'Letter'], ['bar', 'Bar'], ['wash', 'Wash']]),
      LOOK.marker === 'letter' ? 'a letter in the account’s colour' : LOOK.marker === 'bar' ? 'a thin bar in the account’s colour' : 'a bar, and the row tinted'),
    h('div', {class: 'lk-accts'}, ACCOUNT_ORDER.filter(id => id !== 'local').map(id =>
      h('span', {class: 'lk-acct acct-tint', style: `--acct:${acctColor(id)}`}, acctMark(id), acctName(id)))),
    h('div', {class: 'lk-cx'}, grade('Body text', contrastOf(txt, plane)), grade('Dates and counts', contrastOf(muted, plane)), grade('Text on buttons', contrastOf(ink, acc))),
    h('div', {class: 'rrow lk-foot'},
      h('button', {class: 'btn', onclick: () => { saveLook({...LOOK_DEFAULT}); redraw(); }}, 'Reset to Talos'),
      h('button', {class: 'btn ghost', onclick: e => { try { navigator.clipboard.writeText(JSON.stringify(LOOK, null, 2)); e.target.textContent = 'Copied'; } catch (x) {} }}, 'Copy as JSON')));
}

// ---- accounts: a letter in the account's colour, so they are told apart by letter and by colour.
// Appearance › Accounts in lists turns it into a thin bar, or a bar and a tint over the row.
// An account's ink is dark on the light fills (orange, green, amber), else white: both readable.
const acctMark = id => h('span', {class: 'acct-mark', role: 'img', style: `--acct:${acctColor(id)};--acct-ink:${ACCOUNT_INK[id] || 'var(--on-fill)'}`,
  title: acctName(id), 'aria-label': acctName(id)}, ACCOUNT_LETTER[id] || String(id || '?').slice(0, 1).toUpperCase());

// ---------------------------------------------------------------- the reading pane
// Whatever is opened (a message, a work item, a note, a changeset) shows in a reading pane beside
// or below the view, behind a divider that resizes it. Nothing is greyed out and the list stays
// usable; the row it came from stays marked. Per view, this browser remembers where the pane goes
// (right, bottom, or off, which is the old overlay drawer) and its size. Below SHEET_BELOW pixels
// it is a full-screen sheet over the view.
//
// Every opener goes through openPane(content, opts) or paneLoad(opts, load). opts.key names what
// is shown ('m:12' a message, 'w:3' a work item, 'n:7' a note, 'c:2' a changeset); list rows carry
// the same key as data-pane-key, which is how the open row is marked, even after a redraw. Opening
// something from inside the pane keeps the previous content one or two steps back.
const PANE_MIN = {w: 340, h: 180}, LIST_MIN = {w: 480, h: 160}, SHEET_BELOW = 900;
const PANE_DEFAULT = {pos: 'right', w: 520, h: 380};
const PANE_PLACES = ['right', 'bottom', 'off'];
let PANE_PREFS = {};
try { const x = JSON.parse(localStorage.getItem('talos-pane') || '{}'); if (x && typeof x === 'object') PANE_PREFS = x; } catch (e) {}
function paneSettings(view) {
  const x = PANE_PREFS[view] || {};
  return {pos: PANE_PLACES.includes(x.pos) ? x.pos : PANE_DEFAULT.pos, w: Number(x.w) || PANE_DEFAULT.w, h: Number(x.h) || PANE_DEFAULT.h};
}
function setPanePref(view, patch, save = true) {
  PANE_PREFS = {...PANE_PREFS, [view]: {...paneSettings(view), ...patch}};
  if (save) try { localStorage.setItem('talos-pane', JSON.stringify(PANE_PREFS)); } catch (e) {}
}
// Where the pane goes and how big it is, from the chosen place and the window (pure). "Right"
// keeps the list at least LIST_MIN.w wide; when even the narrowest pane leaves less than that, it
// shows at the bottom instead, without changing the choice.
function paneLayout(pref, vw, vh, railW, max) {
  if (pref.pos === 'off') return {mode: 'overlay'};
  if (vw < SHEET_BELOW) return {mode: 'sheet'};
  const room = vw - railW;
  const mode = pref.pos === 'right' && room - PANE_MIN.w < LIST_MIN.w ? 'bottom' : pref.pos;
  const [want, lo, hi] = mode === 'right' ? [pref.w, PANE_MIN.w, room - LIST_MIN.w] : [pref.h, PANE_MIN.h, Math.max(PANE_MIN.h, vh - LIST_MIN.h)];
  return {mode, max: !!max, auto: mode !== pref.pos, size: Math.round(Math.min(hi, Math.max(lo, want))), min: lo, maxSize: hi};
}

const PANE = {open: false, el: null, body: null, scrim: null, view: null, layout: null, max: false,
              cur: null, stack: [], origin: null, rootKey: null, token: 0};
// The element a click or key press last landed on: where an opening came from.
let lastTarget = null;
for (const t of ['pointerdown', 'keydown']) document.addEventListener(t, e => { lastTarget = e.target; }, true);
const railWidth = () => $rail.getBoundingClientRect().width;
const paneSplit = () => PANE.open && PANE.layout && (PANE.layout.mode === 'right' || PANE.layout.mode === 'bottom');
const byPaneKey = k => k ? $main.querySelector(`[data-pane-key="${CSS.escape(k)}"]`) : null;

function paneShell() {
  if (PANE.el) return;
  PANE.title = h('div', {class: 'pane-t', id: 'pane-title'});
  PANE.backSlot = h('span', {class: 'pane-back'});
  PANE.optBtn = h('button', {class: 'pane-btn', 'aria-haspopup': 'menu', 'aria-label': 'Reading pane options', title: 'Reading pane: right, bottom or off',
    onmousedown: e => { if (menuEl) e.stopPropagation(); }, onclick: () => paneMenu()}, icon('pane'));
  PANE.maxBtn = h('button', {class: 'pane-btn', 'aria-pressed': 'false', onclick: () => paneMaximise(!PANE.max)});
  PANE.div = h('div', {class: 'pane-div', role: 'separator', tabindex: '0', 'aria-controls': 'rpane', 'aria-label': 'Resize the reading pane',
    title: 'Drag, or use the arrow keys, to resize; double-click for the default size',
    onpointerdown: paneDrag, onkeydown: paneDividerKeys, ondblclick: () => paneResize(PANE.layout.mode === 'right' ? PANE_DEFAULT.w : PANE_DEFAULT.h, true)});
  PANE.body = h('div', {class: 'pane-b', tabindex: '-1'});
  PANE.el = h('aside', {class: 'rpane', id: 'rpane', 'aria-labelledby': 'pane-title'},
    PANE.div,
    h('div', {class: 'pane-h'}, PANE.backSlot, PANE.title, h('span', {class: 'sp'}), PANE.optBtn, PANE.maxBtn,
      h('button', {class: 'pane-btn pane-x', 'aria-label': 'Close', title: 'Close (Esc)', onclick: () => closePane()}, h('span', {'aria-hidden': 'true'}, '×'), h('span', {class: 'pane-x-t'}, 'Close'))),
    PANE.body);
  PANE.scrim = h('div', {class: 'scrim', onclick: () => closePane()});
}

// Open content in the pane (or the overlay, when the pane is off for this view). opts:
//   key      what is shown, as the rows' data-pane-key
//   title    the pane header's title; label names it on the Back link
//   origin   the element it came from (default: what was last clicked); focus returns there
//   replace  the same entry again (loaded, saved): no new history step, focus stays
//   focus    false leaves focus where it is (the list's arrow keys)
function openPane(content, opts = {}) {
  let from = opts.origin || lastTarget;
  if (from && from.closest && from.closest('.menu') && menuReturn) from = menuReturn;
  const wasOpen = PANE.open;
  const same = wasOpen && PANE.cur && opts.key && PANE.cur.key === opts.key;
  const inside = wasOpen && !!from && PANE.el.contains(from);
  const hadFocus = wasOpen && PANE.el.contains(document.activeElement);
  const fresh = !opts.replace && !same;
  if (fresh) {
    PANE.token++;
    if (inside && PANE.cur) {
      PANE.stack.push({...PANE.cur, node: PANE.body.firstChild, scroll: PANE.body.scrollTop});
      if (PANE.stack.length > 2) PANE.stack.shift();
    } else {
      PANE.stack = [];
      PANE.origin = from && from.closest ? from.closest('[data-pane-key]') || from.closest('button, a, [tabindex], tr') || null : null;
      PANE.rootKey = opts.key || null;
    }
  } else if (opts.replace && PANE.cur && opts.key && PANE.rootKey === PANE.cur.key) PANE.rootKey = opts.key;  // 'w:new' saved as 'w:12'
  PANE.cur = {key: opts.key || null, title: opts.title || 'Details', label: opts.label || opts.title || ''};
  if (!wasOpen) {
    paneShell();
    Object.assign(PANE, {open: true, view: S.view, max: false});
    document.body.append(PANE.el);
  }
  fill(PANE.body, content);
  if (fresh) PANE.body.scrollTop = 0;
  paneHead();
  paneApply();
  paneMark();
  if (fresh && opts.focus !== false) PANE.body.focus({preventScroll: true});
  else if (hadFocus && !PANE.el.contains(document.activeElement)) PANE.body.focus({preventScroll: true});
  if (fresh && !inside && PANE.origin && $main.contains(PANE.origin) && paneSplit()) PANE.origin.scrollIntoView({block: 'nearest'});
  return PANE.token;
}
// "Loading…" at once, then what load() returns ({node, label}), unless something else was opened
// meanwhile. Opening what is already shown refreshes it in place and keeps its scroll position.
async function paneLoad(opts, load) {
  const same = PANE.open && PANE.cur && PANE.cur.key === opts.key;
  const keep = same ? PANE.body.scrollTop : 0;
  const t = same ? PANE.token : openPane(skeleton(), opts);
  let r;
  try { r = await load(); } catch (e) { r = {node: h('div', {class: 'err'}, errText(e))}; }
  if (t !== PANE.token || !PANE.open) return;
  openPane(r.node, {...opts, label: r.label || opts.label, replace: true});
  if (same) PANE.body.scrollTop = keep;
}
// Loading: grey lines the size of the text to come, pulsing quietly, instead of a word.
const skeleton = () => h('div', {class: 'skel-lines', role: 'status', 'aria-label': 'Loading'},
  h('div', {class: 'skel tall'}), h('div', {class: 'skel w80'}), h('div', {class: 'skel w60'}), h('div', {class: 'skel'}), h('div', {class: 'skel w80'}), h('div', {class: 'skel w40'}));
function paneHead() {
  fill(PANE.title, PANE.cur.title);
  const prev = PANE.stack[PANE.stack.length - 1];
  fill(PANE.backSlot, prev ? h('button', {class: 'linkbtn pane-backbtn', title: 'Back to ' + (prev.label || prev.title), onclick: paneBack},
    h('span', {'aria-hidden': 'true'}, '‹'), 'Back', h('span', {class: 'pane-backl'}, prev.label || prev.title)) : null);
}
function paneBack() {
  const prev = PANE.stack.pop();
  if (!prev) return;
  PANE.token++;
  PANE.cur = {key: prev.key, title: prev.title, label: prev.label};
  fill(PANE.body, prev.node);
  PANE.body.scrollTop = prev.scroll;
  paneHead();
  paneMark();
  PANE.body.focus({preventScroll: true});
}
function closePane(restore = true) {
  if (!PANE.open) return;
  closeMenu();
  PANE.el.remove();
  PANE.scrim.remove();
  keepInPlace(() => document.documentElement.classList.remove('pane-right', 'pane-bottom', 'pane-sheet', 'pane-dragging'));
  Object.assign(PANE, {open: false, cur: null, stack: [], max: false, layout: null});
  PANE.token++;
  paneMark();
  const o = PANE.origin && $main.contains(PANE.origin) ? PANE.origin : byPaneKey(PANE.rootKey);
  PANE.origin = PANE.rootKey = null;
  if (restore && o) o.focus();
}
// The view gets narrower or shorter when the pane opens, moves or closes, and its rows reflow.
// The row in hand (the open one) stays where it was on the screen, so the list does not jump.
function keepInPlace(change) {
  let el = $main.querySelector('.is-open') || (PANE.origin && $main.contains(PANE.origin) ? PANE.origin : null);
  const top = el ? el.getBoundingClientRect().top : null;
  if (top == null || top < 0 || top > innerHeight) el = null;
  change();
  if (el) window.scrollBy(0, el.getBoundingClientRect().top - top);
}
// Place the pane by the current choice and window size.
function paneApply() { keepInPlace(paneApplyNow); }
function paneApplyNow() {
  if (!PANE.open) return;
  const L = PANE.layout = paneLayout(paneSettings(PANE.view), innerWidth, innerHeight, railWidth(), PANE.max);
  const split = L.mode === 'right' || L.mode === 'bottom', modal = L.mode === 'overlay' || L.mode === 'sheet';
  PANE.el.className = 'rpane rp-' + L.mode + (L.max ? ' rp-max' : '');
  const root = document.documentElement;
  root.classList.toggle('pane-right', L.mode === 'right');
  root.classList.toggle('pane-bottom', L.mode === 'bottom');
  root.classList.toggle('pane-sheet', L.mode === 'sheet');
  if (split) root.style.setProperty('--pane-size', L.size + 'px');
  if (L.mode === 'overlay') { if (!PANE.scrim.isConnected) PANE.el.before(PANE.scrim); } else PANE.scrim.remove();
  if (modal) { PANE.el.setAttribute('role', 'dialog'); PANE.el.setAttribute('aria-modal', 'true'); }
  else { PANE.el.removeAttribute('role'); PANE.el.removeAttribute('aria-modal'); }
  PANE.div.hidden = !split || L.max;
  if (split) {
    PANE.div.setAttribute('aria-orientation', L.mode === 'right' ? 'vertical' : 'horizontal');
    PANE.div.setAttribute('aria-valuemin', L.min);
    PANE.div.setAttribute('aria-valuemax', L.maxSize);
    PANE.div.setAttribute('aria-valuenow', L.size);
    PANE.div.setAttribute('aria-valuetext', `${L.size} pixels ${L.mode === 'right' ? 'wide' : 'high'}`);
  }
  PANE.maxBtn.hidden = !split;
  PANE.maxBtn.setAttribute('aria-pressed', String(!!L.max));
  PANE.maxBtn.setAttribute('aria-label', L.max ? 'Restore the split' : 'Maximise the pane');
  PANE.maxBtn.title = L.max ? 'Restore the split' : 'Open full width';
  fill(PANE.maxBtn, icon(L.max ? 'restore' : 'maximise'));
}
window.addEventListener('resize', paneApply);
function paneResize(size, save) {
  const L = PANE.layout;
  if (!L || !L.size) return;
  const v = Math.round(Math.min(L.maxSize, Math.max(L.min, size)));
  setPanePref(PANE.view, L.mode === 'right' ? {w: v} : {h: v}, save);
  paneApply();
}
function paneDrag(e) {
  const L = PANE.layout;
  if (e.button !== 0 || !L || !L.size) return;
  e.preventDefault();
  const right = L.mode === 'right', start = right ? e.clientX : e.clientY, from = L.size;
  let size = from;
  const div = PANE.div;
  div.setPointerCapture(e.pointerId);
  document.documentElement.classList.add('pane-dragging');
  const move = ev => { size = from + start - (right ? ev.clientX : ev.clientY); paneResize(size, false); };
  const up = () => {
    div.removeEventListener('pointermove', move); div.removeEventListener('pointerup', up); div.removeEventListener('pointercancel', up);
    document.documentElement.classList.remove('pane-dragging');
    paneResize(size, true);
  };
  div.addEventListener('pointermove', move); div.addEventListener('pointerup', up); div.addEventListener('pointercancel', up);
}
function paneDividerKeys(e) {
  const L = PANE.layout;
  if (!L || !L.size) return;
  const step = e.shiftKey ? 80 : 20, right = L.mode === 'right';
  const v = e.key === (right ? 'ArrowLeft' : 'ArrowUp') ? L.size + step : e.key === (right ? 'ArrowRight' : 'ArrowDown') ? L.size - step
    : e.key === 'Home' ? L.min : e.key === 'End' ? L.maxSize : null;
  if (v == null) return;
  e.preventDefault();
  paneResize(v, true);
}
function paneMaximise(on) { PANE.max = !!on; paneApply(); }
function panePlace(pos) {
  setPanePref(PANE.view, {pos});
  if (pos === 'off') PANE.max = false;
  paneApply();
  paneMark();
}
// Mark the row the pane shows: what it came from, or, after a nested opening, the nested item
// when the view lists it. After a redraw the origin is found again by its key.
function paneMark() {
  $main.querySelectorAll('.is-open').forEach(x => { x.classList.remove('is-open'); x.removeAttribute('aria-current'); });
  if (!PANE.open || !PANE.cur) return;
  if (PANE.origin && !$main.contains(PANE.origin)) PANE.origin = byPaneKey(PANE.rootKey);
  const el = PANE.cur.key !== PANE.rootKey && byPaneKey(PANE.cur.key) || (PANE.origin && PANE.origin.dataset.paneKey === PANE.rootKey ? PANE.origin : byPaneKey(PANE.rootKey));
  if (el) { el.classList.add('is-open'); el.setAttribute('aria-current', 'true'); }
}
function paneMenu() {
  if (menuEl) { closeMenu(true); return; }
  menuReturn = PANE.optBtn;
  const pref = paneSettings(PANE.view), L = PANE.layout || {};
  const split = L.mode === 'right' || L.mode === 'bottom';
  const radio = (pos, label, sub) => h('button', {class: 'menu-item', role: 'menuitemradio', 'aria-checked': String(pref.pos === pos), tabindex: '-1',
      onclick: () => { closeMenu(true); panePlace(pos); }},
    h('span', {class: 'mi-l'}, h('span', {class: 'mi-c', 'aria-hidden': 'true'}, pref.pos === pos ? '✓' : ''), label), sub ? h('span', {class: 'mi-s'}, sub) : null);
  const act = (label, fn) => h('button', {class: 'menu-item', role: 'menuitem', tabindex: '-1', onclick: () => { closeMenu(true); fn(); }},
    h('span', {class: 'mi-l'}, h('span', {class: 'mi-c', 'aria-hidden': 'true'}), label));
  menuEl = h('div', {class: 'menu pane-menu', role: 'menu', 'aria-label': 'Reading pane options', onkeydown: menuKeys},
    h('div', {class: 'menu-h'}, `Reading pane · ${VIEW_NAME[PANE.view] || PANE.view}`),
    radio('right', 'On the right', pref.pos === 'right' && L.auto ? 'The window is narrow, so it shows at the bottom for now' : null),
    radio('bottom', 'At the bottom'),
    radio('off', 'Off', 'Open over the page, as before'),
    split ? [h('div', {class: 'menu-sep', role: 'separator'}),
      act(L.max ? 'Restore the split' : 'Open full width', () => paneMaximise(!L.max)),
      L.max ? null : act('Default size', () => paneResize(L.mode === 'right' ? PANE_DEFAULT.w : PANE_DEFAULT.h, true))] : null,
    h('div', {class: 'mi-s pane-menu-f'}, 'Remembered for this view in this browser'));
  document.body.append(menuEl);
  PANE.optBtn.setAttribute('aria-expanded', 'true');
  placeMenu(PANE.optBtn);
  (menuEl.querySelector('[aria-checked="true"]') || menuEl.querySelector('.menu-item')).focus();
}
document.addEventListener('keydown', e => {
  if (!PANE.open || e.defaultPrevented || menuEl) return;
  // F6 moves between the list and the pane, as in Outlook.
  if (e.key === 'F6' && paneSplit()) {
    e.preventDefault();
    if (PANE.el.contains(document.activeElement)) { const o = PANE.origin && $main.contains(PANE.origin) ? PANE.origin : byPaneKey(PANE.rootKey); if (o) o.focus(); else $main.focus(); }
    else PANE.body.focus({preventScroll: true});
    return;
  }
  if (e.key !== 'Escape') return;
  // Esc in a field being edited belongs to the field: in the pane it leaves the field (a second
  // Esc closes), elsewhere it does nothing to the pane.
  const t = e.target;
  const editing = t && (t.isContentEditable || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' ||
    (t.tagName === 'INPUT' && !['checkbox', 'radio', 'button', 'submit', 'reset', 'range', 'color', 'file'].includes(t.type)));
  if (editing) { if (PANE.el.contains(t)) { e.preventDefault(); PANE.body.focus({preventScroll: true}); } return; }
  e.preventDefault();
  closePane();
});

function openMessage(id, opts) {
  return paneLoad({key: 'm:' + id, title: 'Message', ...opts}, async () => {
    const m = await api(`/api/messages/${id}`);
    return {node: messageDetail(m), label: m.subject || '(no subject)'};
  });
}
// A message as the pane shows it. Its sections are builders of their own (attachmentsSection,
// workSection, importanceSection, objectsSection), so a conversation (threadDetail) shows the same
// actions under each of its messages.
// Where a message is, when it has left the mailbox (search.REMOVED_COLUMN): a red chip on its row, in the
// pane and on a Studio card. Only the server's folders decide, so a message restored from the trash loses it.
const REMOVED = {purged: ['Purged', 'In Deleted Items: moved there by a Talos cleanup. Talos keeps the original.'],
                 deleted: ['Deleted', 'In Deleted Items or the trash: moved there outside Talos. Talos keeps the original.'],
                 gone: ['Not on server', 'The server no longer has it anywhere. Talos keeps the original.']};
function removedChip(m) {
  const r = m && REMOVED[m.removed];
  if (!r) return null;
  const by = m.purged_by ? ` Changeset ${m.purged_by.changeset_id}${m.purged_by.applied_at ? ', ' + day(m.purged_by.applied_at) : ''}: ${m.purged_by.title}.` : '';
  return h('span', {class: 'pill hi rm', title: r[1] + by}, r[0]);
}
function messageDetail(m) {
  const people = role => m.participants.filter(p => p.role === role).map(p => p.name ? `${p.name} <${p.address}>` : p.address).join(', ');
  const inThread = m.thread_id && (m.thread.length > 1 || isTeams(m));
  // A Teams message opens its chat at that message; a mail its thread.
  const convo = () => isTeams(m) ? openThread(m.thread_id, {around: m.id}) : openThread(m.thread_id);
  return h('div', null,
    h('div', {class: 'meta', style: 'margin:0 0 6px'}, h('span', {class: 'm-acctname'}, acctMark(m.account_id), acctName(m.account_id)),
      h('span', null, `· ${({in: 'received', out: 'sent', self: 'between your accounts'})[m.direction] || m.direction} · ${new Date(m.received_at || m.sent_at || 0).toLocaleString('sv-SE', {dateStyle: 'medium', timeStyle: 'short'})}`)),
    h('h2', null, m.subject || '(no subject)'),
    h('div', {style: 'display:flex;gap:6px;flex-wrap:wrap;align-items:center'},
      removedChip(m),
      teamsPill(m),
      (m.labels || []).map(l => h('span', {class: 'pill'}, l)),
      m.is_automated ? h('span', {class: 'pill md'}, 'automated') : null,
      valueTags(m), helpBtn('values'),
      (m.events || []).map(ev => h('span', {class: 'pill ' + (ev.status === 'failed' ? 'hi' : ev.status === 'ok' ? 'ok' : '')}, `${ev.kind}: ${ev.status}`)),
      null),
    replyBar(m),
    workSection(m),
    h('div', {class: 'agrp-row'}, importanceSection(m),
      inThread ? agroup('Conversation', abtn('act', 'convo', isTeams(m) ? 'Open the chat' : `Open · ${fmt(m.thread.length)}`, convo,
        {color: 'var(--accent)', key: MESSAGE_KEYS.conversation, title: isTeams(m) ? 'The whole chat, as a chat' : 'Every message in this thread, in order'})) : null),
    objectsSection(m),
    h('div', {class: 'dsec'}, h('h4', null, 'People'),
      h('dl', {class: 'kv', style: 'display:grid;grid-template-columns:auto minmax(0,1fr);gap:4px 12px;font-size:12.5px;margin:0'},
        ['from', 'to', 'cc'].filter(r => people(r)).map(r => [h('dt', {class: 'muted'}, r), h('dd', {style: 'margin:0;overflow-wrap:anywhere'}, people(r))]))),
    attachmentsSection(m),
    m.thread.length > 1 ? h('div', {class: 'dsec'}, h('h4', null, `Thread · ${fmt(m.thread.length)} messages`),
      // A long chat is thousands of lines: the table is for mail-sized threads, the conversation for the rest.
      m.thread.length > THREAD_TABLE_MAX ? h('div', {class: 'small muted'}, 'Too long to list here. ', h('button', {class: 'linkbtn', style: 'padding:0', onclick: convo}, isTeams(m) ? 'Open the chat' : 'Open the conversation')) :
      h('table', {class: 't'}, h('tbody', null, m.thread.map(t => h('tr', {class: 'click', tabindex: '0', onclick: () => openMessage(t.id), onkeydown: e => { if (e.key === 'Enter') openMessage(t.id); }},
        h('td', {style: 'width:70px'}, when(t.received_at)), h('td', null, h('b', null, t.from_name || t.from_address), h('div', {class: 'small muted'}, t.snippet || '')),
        h('td', {class: 'r'}, t.id === m.id ? h('span', {class: 'pill ac'}, 'this') : '')))))) : null,
    mailBody(m, () => h('div', {class: 'body-text'}, m.body_text || '(no text)'), {heading: true}),
    agroup('More', [rawLink(m),
      m.thread_id ? abtn('quiet', 'convo', 'Thread as a list', () => { S.f = {...blank(), thread: m.thread_id}; go('messages'); }) : null]));
}
// A message's values as tags: solid when they count (accepted, a rule's, the owner's), dashed when Jev only
// proposed them (the latest run's proposal, where the field has no value that counts). The
// boundaries show their value alone (Work, Automated, Worth keeping), the rest field: value.
const BOUNDARY_TAGS = ['sender_kind', 'sphere', 'form', 'keep'];
const tagText = v => BOUNDARY_TAGS.includes(v.dimension_id) ? (v.label || v.value) : `${v.dimension_id}: ${v.label || v.value}`;
const SOURCE_TEXT = {human: 'set by you', rule: 'set by a rule', model: 'accepted from Jev', import: 'imported'};
const LEVEL_TEXT = {fact: 'Fact', certain: 'Certain', confident: 'Confident', likely: 'Likely', maybe: 'Maybe', doubtful: 'Doubtful', guess: 'Guess'};
const levelWord = v => v.level ? h('span', {class: 'lvw lv-' + v.level}, LEVEL_TEXT[v.level] || v.level) : null;
function valueTags(m) {
  const order = v => { const i = BOUNDARY_TAGS.indexOf(v.dimension_id); return i < 0 ? 10 : i; };
  const vals = [...(m.values || [])].sort((a, b) => order(a) - order(b));
  const props = [...(m.proposals || [])].sort((a, b) => order(a) - order(b));
  return [vals.map(v => h('span', {class: 'pill tag' + (BOUNDARY_TAGS.includes(v.dimension_id) ? ' btag' : ''),
                           title: `${v.dimension_label || v.dimension_id}: ${v.label || v.value} · ${LEVEL_TEXT[v.level] || ''}${v.confidence != null && v.source_kind === 'model' ? ' (' + Math.round(v.confidence * 100) + '%)' : ''} · ${SOURCE_TEXT[v.source_kind] || v.source_kind}${v.source_kind === 'rule' || v.source_kind === 'model' ? ' (' + (v.source_ref || '') + ')' : ''}${v.via === 'thread' ? ' · set on the thread' : ''}`}, tagText(v), levelWord(v))),
          props.map(v => h('span', {class: 'pill tag prop' + (BOUNDARY_TAGS.includes(v.dimension_id) ? ' btag' : ''),
                           title: `${v.dimension_label || v.dimension_id}: ${v.label || v.value} · ${LEVEL_TEXT[v.level] || ''} · proposed by Jev${v.confidence != null ? ' at ' + Number(v.confidence).toFixed(2) : ''}, not accepted`}, tagText(v), levelWord(v)))];
}
const THREAD_TABLE_MAX = 40;
const rawLink = m => h('a', {class: 'ab ab-quiet', href: `/api/messages/${m.id}/raw`}, icon('raw'), h('span', null, isTeams(m) ? 'Original (.json)' : 'Original (.eml)'));
// A message's attachments: files in the vault open from Talos; Teams files are links to SharePoint/OneDrive.
function attachmentsSection(m) {
  const list = m.attachments || [];
  return list.length ? h('div', {class: 'dsec'}, h('h4', null, `Attachments · ${list.length}`),
    list.map(a => h('div', {class: 'att'},
      h('div', {style: 'min-width:0'},
        a.attrs && a.attrs.reference
          ? (safeLink(a.attrs.url) ? h('a', {href: safeLink(a.attrs.url), target: '_blank', rel: 'noopener noreferrer'}, a.filename || '(unnamed)') : h('span', null, a.filename || '(unnamed)'))
          : h('a', {href: `/api/attachments/${a.id}`, target: '_blank', rel: 'noopener'}, a.filename || '(unnamed)'),
        h('div', {class: 'small muted'}, `${a.content_type} · ${a.attrs && a.attrs.reference ? 'link in SharePoint/OneDrive' : bytes(a.size_bytes)}` + (a.attrs && a.attrs.pages ? ` · ${a.attrs.pages} pages` : '') + (a.attrs && a.attrs.model ? ` · ${a.attrs.model}` : '')),
        a.preview ? h('div', {class: 'prev'}, a.preview) : null)))) : null;
}

// ---------------------------------------------------------------- conversations
// A thread in the pane: an e-mail thread as stacked messages (the newest open, the older ones
// one line each), a Teams chat as a chat. Opened with the key 't:<thread id>'. A long chat loads
// its newest THREAD_PAGE lines; "Load earlier" pages back with ?before=<oldest id shown>.
const THREAD_PAGE = 100;
const firstName = n => String(n || '').trim().split(/[\s@]+/)[0] || String(n || '');
const personName = p => p.name || (p.address ? p.address.split('@')[0] : '') || '?';
const initials = n => { const w = String(n || '?').replace(/[^\p{L}\p{N}\s]/gu, ' ').trim().split(/\s+/).filter(Boolean); return ((w[0] || '?')[0] + (w.length > 1 ? w[w.length - 1][0] : '')).toUpperCase(); };
// A person's colour in a chat: one of the series colours, by a hash of who they are, so it never shifts.
const PERSON_SERIES = ['--series-1', '--series-2', '--series-3', '--series-4', '--series-5', '--series-6', '--series-8'];
const personColor = key => { let x = 0; for (const c of String(key || '')) x = (x * 31 + c.codePointAt(0)) >>> 0; return `var(${PERSON_SERIES[x % PERSON_SERIES.length]})`; };
const isMine = m => m.direction === 'out' || m.direction === 'self';
const senderKey = m => isMine(m) ? '' : String(m.from_address || m.from_name || '?').toLowerCase();
const threadTitle = t => t.subject || (isTeams(t) ? 'Teams chat' : '(no subject)');
const CHAT_KIND = {oneOnOne: 'One-to-one chat', group: 'Group chat', meeting: 'Meeting chat'};

// opts.around: a message id; the conversation opens with it in the middle, marked, and scrolled to.
function openThread(id, opts = {}) {
  const key = 't:' + id;
  const again = PANE.open && PANE.cur && PANE.cur.key === key && !opts.around;
  const around = opts.around;
  return paneLoad({key, title: 'Conversation', ...opts}, async () => {
    const t = await api(`/api/threads/${id}?limit=${THREAD_PAGE}` + (around ? `&around=${around}` : ''));
    return {node: threadDetail(t), label: threadTitle(t)};
  }).then(() => {
    if (!PANE.open || !PANE.cur || PANE.cur.key !== key) return;
    if (around) showFound(around); else if (!again) showNewest();
  });
}
// The found message: scrolled to the middle of the pane and washed in the accent colour for a moment.
function showFound(mid) {
  const el = PANE.body.querySelector(`[data-id="${CSS.escape(String(mid))}"]`);
  if (!el) { showNewest(); return; }
  el.classList.add('found');
  const b = PANE.body;
  b.scrollTop += el.getBoundingClientRect().top - b.getBoundingClientRect().top - b.clientHeight / 2 + el.offsetHeight / 2;
}
// Opened fresh, a conversation shows its newest message: a chat scrolls to the bottom, a mail
// thread to the open (newest) message when it is below the fold.
function showNewest() {
  const b = PANE.body;
  if (b.querySelector('.chat')) { b.scrollTop = b.scrollHeight; return; }
  const last = b.querySelector('.tm-card:last-of-type');
  if (!last) return;
  const off = last.getBoundingClientRect().top - b.getBoundingClientRect().top;
  if (off > b.clientHeight * 0.6) b.scrollTop += off - 12;
}
function threadDetail(t) {
  const teams = isTeams(t);
  const n = t.message_count;
  const people = t.people || [];
  const range = t.first_at && t.last_at && day(t.first_at) !== day(t.last_at) ? `${day(t.first_at)} – ${day(t.last_at)}` : day(t.last_at);
  return h('div', {class: 'thread' + (teams ? ' is-chat' : '')},
    h('div', {class: 'meta t-meta', style: 'margin:0 0 6px'}, h('span', null, acctDot(t.account_id), ' ', acctName(t.account_id)),
      teams ? h('span', null, t.medium === 'teams_channel' ? `${t.team || 'Team'} › ${t.channel || 'Channel'}` : CHAT_KIND[t.chat_type] || 'Chat') : null,
      h('span', null, `${fmt(n)} ${n === 1 ? 'message' : 'messages'}`), h('span', null, range)),
    h('h2', null, threadTitle(t)),
    h('div', {class: 'tpeople', 'aria-label': 'People'}, people.map(p => h('span', {class: 'tperson' + (p.me ? ' me' : ''), title: (p.address || '') + (p.n ? ` · ${fmt(p.n)} ${p.n === 1 ? 'message' : 'messages'}` : ' · has not written here')},
      h('span', {class: 'cav sm', style: `--pc:${p.me ? 'var(--accent)' : personColor(p.address || p.name)}`, 'aria-hidden': 'true'}, initials(p.me ? 'me' : personName(p))),
      p.me ? 'me' : personName(p), p.n ? h('span', {class: 'n'}, fmt(p.n)) : null))),
    teams ? chatView(t) : mailThreadView(t),
    teams ? teamsComposer({thread_id: t.id}, t.medium === 'teams_channel' ? `Reply in ${t.team || 'the team'} › ${t.channel || 'the channel'}` : 'Write in this chat') : null,
    h('div', {class: 'dactions'},
      h('button', {class: 'btn ghost', onclick: () => {
        if (teams) { S.fs.teams = {...teamsDefaults(), thread: t.id}; TEAMS.fromThread = true; LIST_MODE = {...LIST_MODE, teams: 'messages'}; go('teams'); return; }
        S.f = {...blank(), thread: t.id}; go('messages'); }}, 'Show as a list of messages')));
}
// "Load earlier": the older page is put in front of what is shown, and the reader stays where they were.
function earlierButton(t, state, redraw) {
  if (!state.more) return null;
  const left = t.message_count - state.rows.length;
  const btn = h('button', {class: 'btn sm', onclick: async () => {
    btn.disabled = true;
    fill(btn, 'Loading…');
    let page;
    try { page = await api(`/api/threads/${t.id}?limit=${THREAD_PAGE}&before=${state.before}`); }
    catch (e) { fill(btn, 'Load earlier'); btn.disabled = false; flash(errText(e)); return; }
    const b = PANE.body, height = b.scrollHeight, top = b.scrollTop;
    state.rows = page.messages.concat(state.rows);
    state.more = page.has_more;
    state.before = page.before;
    redraw();
    b.scrollTop = top + (b.scrollHeight - height);
  }}, `Load earlier messages (${fmt(left)} more)`);
  return h('div', {class: 'tmore'}, btn);
}

// ---- an e-mail thread: one card per message, oldest first. The newest is open; the others are one
// line (who, the start of the text, when) that opens on a click. An open message is fetched in full
// and shows the new part of its text (quoted history and a signature folded away), its attachments
// and the message pane's own actions.
function mailThreadView(t) {
  const state = {rows: t.messages, more: t.has_more, before: t.before, open: new Set([t.messages.length ? t.messages[t.messages.length - 1].id : null])};
  const box = h('div', {class: 'tmail'});
  const cards = new Map();  // message id → card, kept across "Load earlier"
  const redraw = () => {
    fill(box, earlierButton(t, state, redraw), state.rows.map(m => {
      if (!cards.has(m.id)) cards.set(m.id, mailCard(m, state.open.has(m.id)));
      return cards.get(m.id);
    }));
  };
  redraw();
  return box;
}
function mailCard(m, startOpen) {
  const card = h('article', {class: 'tm-card' + (m.seen === false ? ' unread' : '') + (isMine(m) ? ' mine' : ''), 'data-id': m.id});
  const body = h('div', {class: 'tm-body'});
  let loaded = false;
  const who = isMine(m) ? 'me' : (m.from_name || m.from_address || '?');
  const head = h('button', {class: 'tm-head', 'aria-expanded': 'false', title: m.from_address || ''},
    h('span', {class: 'cav', style: `--pc:${isMine(m) ? 'var(--accent)' : personColor(m.from_address || m.from_name)}`, 'aria-hidden': 'true'}, initials(isMine(m) ? 'me' : who)),
    h('span', {class: 'tm-who'}, who),
    h('span', {class: 'tm-snip'}, m.snippet || ''),
    h('span', {class: 'tm-marks'},
      m.importance && m.importance.level === 'high' ? h('span', {class: 'pill ac', title: `Important (score ${m.importance.score})`}, 'important') : null,
      m.has_attachments ? h('span', {class: 'pill', title: 'Has attachments'}, 'att') : null),
    h('span', {class: 'tm-date', title: new Date(m.received_at || 0).toLocaleString('sv-SE')}, when(m.received_at)));
  const load = async () => {
    fill(body, h('div', {class: 'small muted'}, 'Loading…'));
    let full;
    try { full = await api(`/api/messages/${m.id}`); } catch (e) { fill(body, h('div', {class: 'err'}, errText(e))); return; }
    loaded = true;
    // After a change (a work item made, an object added) the cache is cleared; the message is fetched again, in place.
    const reopen = async () => { loaded = false; await load(); };
    const to = (full.participants || []).filter(p => p.role === 'to' || p.role === 'cc').map(p => p.name || p.address);
    fill(body,
      to.length ? h('div', {class: 'small muted tm-to'}, 'To ', to.join(', ')) : null,
      mailBody(full, () => mailText(full), {compact: true}),
      attachmentsSection(full),
      workSection(full, reopen),
      importanceSection(full),
      objectsSection(full, reopen),
      replyBar(full),
      agroup('More', [abtn('quiet', 'convo', 'Open as a message', () => openMessage(m.id)), rawLink(full)]));
  };
  const toggle = open => {
    card.classList.toggle('open', open);
    head.setAttribute('aria-expanded', String(open));
    body.hidden = !open;
    if (open && !loaded) load();
  };
  head.addEventListener('click', () => toggle(!card.classList.contains('open')));
  add(card, [head, body]);
  toggle(!!startOpen);
  return card;
}
// The text of a mail in a thread: the new part (the server's quote_stripped), without a signature
// after a "-- " line; the rest (quoted history, signature) behind a toggle. When there is nothing
// to fold, the text as it is.
function mailText(m) {
  const full = (m.body_text || '').trim();
  let fresh = (m.quote_stripped || '').trim();
  if (!fresh || fresh.length >= full.length) fresh = full;
  const sig = /\n-- ?\n/.exec('\n' + fresh + '\n');
  if (sig && sig.index > 0) fresh = fresh.slice(0, sig.index - 1).trim();
  const text = h('div', {class: 'body-text tm-text'}, fresh || '(no text)');
  if (fresh === full || !fresh) return text;
  const hidden = full.split('\n').length - fresh.split('\n').length;
  const btn = h('button', {class: 'linkbtn tm-quote', 'aria-expanded': 'false', onclick: () => {
    const open = btn.getAttribute('aria-expanded') !== 'true';
    btn.setAttribute('aria-expanded', String(open));
    fill(text, open ? full : fresh);
    fill(btn, open ? 'Hide the quoted text' : `Show the quoted text${hidden > 0 ? ` · ${fmt(hidden)} lines` : ''}`);
  }}, `Show the quoted text${hidden > 0 ? ` · ${fmt(hidden)} lines` : ''}`);
  return h('div', null, text, btn);
}

// ---- a mail's body as HTML or as text. The HTML is never put into this page: it is cleaned on
// the server (talos.mailhtml) and shown in an <iframe sandbox> without allow-scripts and without
// allow-same-origin, so nothing in it runs and it can reach nothing of Talos; allow-popups (and
// -to-escape-sandbox) only let its links open in a new tab. The frame's response carries a CSP
// with no script and, unless asked, no remote images (they are tracking pixels as often as not).
// The frame cannot tell the page its height without a script inside it, so it has a fixed height,
// scrolls inside, and can be made taller by its corner. HTML or Text, and the senders whose
// images always load, are remembered in this browser. Teams messages keep their chat view.
const MAIL_SANDBOX = 'allow-popups allow-popups-to-escape-sandbox';
const BODY_MODE_KEY = 'talos-body-mode', IMAGE_SENDERS_KEY = 'talos-image-senders';
let bodyMode = 'html';
try { if (localStorage.getItem(BODY_MODE_KEY) === 'text') bodyMode = 'text'; } catch (e) {}
function imageSenders() {
  try { const x = JSON.parse(localStorage.getItem(IMAGE_SENDERS_KEY) || '[]'); return Array.isArray(x) ? x.filter(a => typeof a === 'string') : []; }
  catch (e) { return []; }
}
function setImageSender(addr, on) {
  const list = imageSenders().filter(a => a !== addr);
  if (on) list.push(addr);
  try { localStorage.setItem(IMAGE_SENDERS_KEY, JSON.stringify(list)); } catch (e) {}
}
function setBodyMode(mode) {
  bodyMode = mode;
  try { localStorage.setItem(BODY_MODE_KEY, mode); } catch (e) {}
  document.dispatchEvent(new CustomEvent('talos-body-mode'));
}
function mailFrame(m, images, compact) {
  return h('div', {class: 'mframe-wrap' + (compact ? ' compact' : '')},
    h('iframe', {sandbox: MAIL_SANDBOX, class: 'mframe', referrerpolicy: 'no-referrer', loading: 'lazy',
      title: 'The message as HTML' + (images ? '' : ', remote images blocked'), src: `/api/messages/${m.id}/html${images ? '?images=1' : ''}`}));
}
function imagesBar(m, st, redraw) {
  const n = m.html.remote_images;
  if (!n) return null;
  const sender = String(m.from_address || '').toLowerCase();
  const always = sender && imageSenders().includes(sender);
  const alwaysBtn = sender ? h('button', {class: 'linkbtn', onclick: () => { setImageSender(sender, !always); st.images = !always; redraw(); }},
    always ? `Stop loading images from ${sender}` : `Always load images from ${sender}`) : null;
  if (st.images) return h('div', {class: 'mimg on'}, h('span', null, always ? 'Images from this sender load automatically.' : 'Remote images are shown.'), alwaysBtn);
  const trackers = m.html.trackers;
  return h('div', {class: 'mimg'},
    h('span', null, `${fmt(n)} remote image${n === 1 ? '' : 's'} blocked`, trackers ? ` (${fmt(trackers)} ${trackers === 1 ? 'looks' : 'look'} like a tracking pixel)` : '', '.'),
    h('button', {class: 'btn sm', onclick: () => { st.images = true; redraw(); }}, 'Load images'), alwaysBtn);
}
function mailBody(m, textView, opts = {}) {
  if (!m.html || isTeams(m)) {
    const text = textView();
    return opts.heading ? h('div', {class: 'dsec'}, h('h4', null, m.body_kind === 'html' ? 'Text (from HTML)' : 'Text'), text) : text;
  }
  const sender = String(m.from_address || '').toLowerCase();
  const st = {images: !!sender && imageSenders().includes(sender), mode: null};
  const box = h('div', {class: 'mbody' + (opts.heading ? ' dsec' : '')});
  const redraw = () => {
    st.mode = bodyMode;
    const html = bodyMode === 'html';
    fill(box,
      h('div', {class: 'mbody-h'}, opts.heading ? h('h4', null, html ? 'Message' : 'Text (from the message)') : h('span'),
        h('div', {class: 'seg sm', role: 'group', 'aria-label': 'Show the message as'}, [['html', 'HTML'], ['text', 'Text']].map(([v, l]) =>
          h('button', {'aria-pressed': String(v === bodyMode), title: v === 'html' ? 'As the sender laid it out, in a sandbox' : 'Plain text', onclick: () => setBodyMode(v)}, l)))),
      html ? [imagesBar(m, st, redraw), mailFrame(m, st.images, opts.compact)] : textView());
  };
  // Every open body follows a switch; one that has left the page stops listening.
  const onMode = () => { if (!box.isConnected && box.dataset.mounted) { document.removeEventListener('talos-body-mode', onMode); return; } if (st.mode !== bodyMode) redraw(); };
  document.addEventListener('talos-body-mode', onMode);
  requestAnimationFrame(() => { box.dataset.mounted = '1'; });
  redraw();
  return box;
}

// ---- a Teams chat: bubbles, the owner's own on the right in the accent colour, the others with a name and
// initials; a line per day; consecutive lines from one person within a few minutes grouped, with
// the time at the group's head and on each line when hovered.
const CHAT_GROUP_MS = 8 * 60e3;
function dayLabel(iso) {
  const d = new Date(iso), today = new Date(), y = new Date(); y.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return 'Today';
  if (d.toDateString() === y.toDateString()) return 'Yesterday';
  return d.toLocaleDateString('sv-SE', {weekday: 'long', day: 'numeric', month: 'long', year: d.getFullYear() === today.getFullYear() ? undefined : 'numeric'});
}
const clock = iso => iso ? new Date(iso).toLocaleTimeString('sv-SE', {hour: '2-digit', minute: '2-digit'}) : '';
function chatView(t) {
  const state = {rows: t.messages, more: t.has_more, before: t.before, later: t.has_later, after: t.after};
  const box = h('div', {class: 'chat', role: 'log', 'aria-label': 'Chat'});
  const redraw = () => fill(box, earlierButton(t, state, redraw), chatLines(state.rows, t.chat_type !== 'oneOnOne'), laterButton(t, state, redraw));
  redraw();
  return box;
}
// "Load later": a conversation opened at a found message has newer ones below it.
function laterButton(t, state, redraw) {
  if (!state.later) return null;
  const btn = h('button', {class: 'btn sm', onclick: async () => {
    btn.disabled = true; fill(btn, 'Loading…');
    let page;
    try { page = await api(`/api/threads/${t.id}?limit=${THREAD_PAGE}&after=${state.after}`); }
    catch (e) { fill(btn, 'Load later messages'); btn.disabled = false; flash(errText(e)); return; }
    const b = PANE.body, top = b.scrollTop;
    state.rows = state.rows.concat(page.messages);
    state.later = page.has_later;
    state.after = page.after;
    redraw();
    b.scrollTop = top;
  }}, 'Load later messages');
  return h('div', {class: 'tmore'}, btn);
}
// opts.blind (the answer key): no way to open a line as a message, whose pane shows Talos's values.
function chatLines(rows, named, opts) {
  const out = [];
  let group = null, lastDay = null;
  for (const m of rows) {
    const at = new Date(m.received_at || 0), d = at.toDateString(), key = senderKey(m);
    if (d !== lastDay) { out.push(h('div', {class: 'chat-day', role: 'separator'}, h('span', null, dayLabel(m.received_at)))); lastDay = d; group = null; }
    if (!group || group.key !== key || at - group.last > CHAT_GROUP_MS) {
      const mine = isMine(m), name = m.from_name || m.from_address || 'Unknown';
      const lines = h('div', {class: 'cg-lines'});
      out.push(h('div', {class: 'cgroup' + (mine ? ' me' : '')},
        mine ? null : h('span', {class: 'cav', style: `--pc:${personColor(m.from_address || m.from_name)}`, title: name, 'aria-hidden': 'true'}, initials(name)),
        h('div', {class: 'cg-body'},
          h('div', {class: 'cg-h'}, mine ? null : (named ? h('b', null, name) : h('b', {class: 'sr-only'}, name)), mine ? h('span', {class: 'sr-only'}, 'You') : null,
            h('time', {datetime: m.received_at, title: at.toLocaleString('sv-SE')}, clock(m.received_at))),
          lines)));
      group = {key, last: at, lines};
    }
    group.last = at;
    group.lines.append(bubble(m, opts));
  }
  return out;
}
function bubble(m, opts) {
  const files = (m.attachments || []).map(a => {
    const url = a.attrs && a.attrs.reference ? safeLink(a.attrs.url) : `/api/attachments/${a.id}`;
    return url ? h('a', {class: 'cfile', href: url, target: '_blank', rel: 'noopener noreferrer'}, '📎 ', a.filename || '(file)') : h('span', {class: 'cfile'}, '📎 ', a.filename || '(file)');
  });
  const important = m.teams_importance === 'high' || m.teams_importance === 'urgent';
  return h('div', {class: 'bubble' + (m.deleted ? ' deleted' : '') + (important ? ' urgent' : '') + (m.importance && m.importance.level === 'high' ? ' imp' : ''), 'data-id': m.id},
    important ? h('span', {class: 'pill hi b-imp'}, m.teams_importance) : null,
    m.deleted ? h('span', {class: 'b-text'}, 'This message was deleted', m.text ? h('span', {class: 'b-was'}, ' · it said: ', m.text) : null)
      : m.text ? h('span', {class: 'b-text'}, m.text) : (files.length ? null : h('span', {class: 'b-text muted'}, '(no text)')),
    files.length ? h('span', {class: 'b-files'}, files) : null,
    h('span', {class: 'b-meta'}, m.edited ? h('span', {class: 'b-ed', title: 'Edited ' + new Date(m.edited).toLocaleString('sv-SE')}, 'edited') : null,
      h('time', {datetime: m.received_at, title: new Date(m.received_at || 0).toLocaleString('sv-SE')}, clock(m.received_at))),
    opts && opts.blind ? null : h('button', {class: 'b-open', 'aria-label': 'Open this message: importance, work item, original', title: 'Open this message', onclick: () => openMessage(m.id)}, '⋯'));
}

// The message pane's "Importance" section: the level, every signal behind it, and the owner's own verdict,
// which beats the score. Pressing the active verdict again takes it back.
const LEVEL_PILL = {high: 'pill hi', medium: 'pill md', low: 'pill', noise: 'pill mono'};
function importanceSection(m) {
  const box = h('fieldset', {class: 'agrp'});
  const draw = imp => {
    const mark = async value => {
      try { draw(await post(`/api/messages/${m.id}/importance`, {value})); }
      catch (e) { box.append(h('div', {class: 'err'}, String(e.message || e))); }
    };
    // Important: a red exclamation mark; not important: a grey line; a question to the owner not answered: amber.
    const verdict = (value, sym, label, color, key) => {
      const on = !!imp && imp.marked === value;
      return abtn('act', sym, label, () => mark(on ? null : value), {color, key, on, pressed: on});
    };
    fill(box, h('legend', null, withHelp('Importance', 'importance')),
      imp ? h('div', {class: 'agrp-b'},
        h('span', {class: (LEVEL_PILL[imp.level] || 'pill') + ' ipill'}, imp.level === 'high' ? icon('important') : null, imp.level),
        h('span', {class: 'small muted'}, `score ${imp.score}`),
        imp.waiting ? h('span', {class: 'pill md ipill'}, icon('question'), 'waiting for your answer') : null)
        : h('div', {class: 'small muted'}, 'Not scored yet.'),
      imp && imp.reasons.length ? h('ul', {class: 'small', style: 'margin:6px 0 0;padding-left:18px'},
        imp.reasons.map(r => h('li', null, r.text, r.points ? h('span', {class: 'muted'}, ` ${r.points > 0 ? '+' : ''}${r.points}`) : null))) : null,
      h('div', {class: 'agrp-b', style: 'margin-top:8px'}, verdict('important', 'important', 'Important', 'var(--critical)', MESSAGE_KEYS.important),
        verdict('not_important', 'notimp', 'Not important', 'var(--text-secondary)', MESSAGE_KEYS.not_important)));
  };
  draw(m.importance);
  return box;
}

// The message pane's "Objects" section: where the message belongs, and a control to add it.
function objectsSection(m, reopen) {
  const objs = m.objects || [];
  const box = h('div', {class: 'objadd'});
  const closed = () => fill(box, abtn('act', 'binderadd', 'Add to a binder', () => opened(), {color: 'var(--accent)', key: MESSAGE_KEYS.binder}));
  const opened = async () => {
    box.replaceChildren(h('span', {class: 'small muted'}, 'Loading objects…'));
    let list;
    try { list = await api('/api/objects'); } catch (e) { box.replaceChildren(h('span', {class: 'err'}, String(e.message || e))); return; }
    const mine = new Set(objs.map(o => o.id));
    const sel = h('select', {class: 'sel', 'aria-label': 'Object'},
      list.filter(o => !mine.has(o.id)).map(o => h('option', {value: o.id}, `${o.name} · ${KIND_LABEL[o.kind] || o.kind}`)),
      h('option', {value: 'new'}, 'New collection…'));
    const msg = h('span', {class: 'small'});
    const addTo = async ids => {
      let oid = sel.value;
      try {
        if (oid === 'new') {
          const name = (prompt('Name of the new collection') || '').trim();
          if (!name) return;
          oid = (await post('/api/objects', {kind: 'collection', name})).id;
        }
        await post(`/api/objects/${oid}/members`, {action: 'add', ids});
      } catch (e) { msg.replaceChildren(h('span', {class: 'err'}, String(e.message || e))); return; }
      if (S.view === 'objects') render();
      (reopen || (() => openMessage(m.id)))();
    };
    fill(box, sel,
      h('button', {class: 'btn sm primary', onclick: () => addTo([m.id])}, 'Add message'),
      m.thread_id && m.thread.length > 1 ? h('button', {class: 'btn sm', onclick: () => addTo([m.thread_id])}, 'Add the thread') : null,
      h('button', {class: 'btn ghost sm', onclick: closed}, 'Cancel'), msg);
    sel.focus();
  };
  closed();
  return agroup(withHelp(objs.length ? `Binders · ${objs.length}` : 'Binders', 'binder-members'), [
    objs.length ? h('div', {class: 'chips'}, objs.map(o => h('button', {class: 'chipbtn', title: sourceText(o.sources, o.thread_sources),
      onclick: () => go('objects', o.id)}, o.name, h('span', {class: 'n'}, o.sources.length ? sourceText(o.sources) : 'via thread')))) : null,
    box]);
}

// ---------------------------------------------------------------- objects
const KIND_LABEL = {project: 'Project', personal_project: 'Personal project', area: 'Area', topic: 'Topic', system: 'System', case: 'Case', collection: 'Collection', saved_search: 'Saved search'};
// Object kinds have colours, as in the Obsidian vault (--kind-* in style.css). They take another
// form than the account colours, which are filled dots and tints: a left stripe on cards and
// rows, and an outlined badge (text in the kind's colour, a thin border, no fill).
const kindCls = k => 'kind-' + (KIND_LABEL[k] ? k : 'other');
const kindBadge = (k, text, attrs) => h('span', {class: 'kbadge ' + kindCls(k), title: (KIND_LABEL[k] || k) + (text ? ': ' + text : ''), ...attrs}, text || KIND_LABEL[k] || k);
const kindsIn = list => KIND_ORDER.filter(k => list.includes(k));
// The legend takes the form of what it explains: stripes for rows and cards, outlined badges for pills.
function kindLegend(kinds, form, extra) {
  if (!kinds.length) return null;
  return h('div', {class: 'legend kind-legend', role: 'note', 'aria-label': 'Colours of the object kinds'},
    h('span', {class: 'lg-h'}, 'Kinds'),
    kinds.map(k => form === 'badge' ? h('span', null, kindBadge(k)) : h('span', {class: 'kl ' + kindCls(k)}, h('span', {class: 'kswatch', 'aria-hidden': 'true'}), KIND_LABEL[k] || k)),
    extra ? h('span', {class: 'lg-x'}, extra) : null);
}

// ---- a small, safe Markdown renderer. It builds elements with h() and never parses HTML:
// headings, paragraphs, bullet, numbered and task lists, quotes, code, bold, italic, links
// (http and https only, rel=noopener) and Obsidian wikilinks, shown as text.
const MD_INLINE = /(`+)([\s\S]*?[^`])\1(?!`)|(!?)\[\[([^\]]+)\]\]|(!?)\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)|\*\*(?=\S)([\s\S]*?\S)\*\*|__(?=\S)([\s\S]*?\S)__|\*(?=[^\s*])([\s\S]*?[^\s*])\*|(?<![\w])_(?=\S)([\s\S]*?\S)_(?![\w])|~~(?=\S)([\s\S]*?\S)~~|(https?:\/\/[^\s<>()]*[^\s<>().,;:!?'"])/g;
const safeHref = u => { try { const x = new URL(u); return x.protocol === 'http:' || x.protocol === 'https:' ? x.href : null; } catch (e) { return null; } };
const mdLink = (href, kids) => h('a', {href, target: '_blank', rel: 'noopener noreferrer'}, kids);
function mdInline(text) {
  const out = [], re = new RegExp(MD_INLINE.source, 'g');
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    last = re.lastIndex;
    if (m[1]) out.push(h('code', null, m[2]));
    else if (m[4] != null) {
      const [target, alias] = m[4].split('|');
      const name = (alias || target.split('#')[0].split('/').pop() || target).trim();
      out.push(h('span', {class: m[3] ? 'wikilink embed' : 'wikilink', title: m[3] ? 'Embedded in Obsidian: ' + target.trim() : target.trim()}, name));
    } else if (m[7] != null) {
      const href = safeHref(m[7]), label = m[6] || (m[5] ? 'image' : m[7]);
      out.push(href ? mdLink(href, m[5] ? label : mdInline(label)) : mdInline(label));
    } else if (m[8] || m[9]) out.push(h('strong', null, mdInline(m[8] || m[9])));
    else if (m[10] || m[11]) out.push(h('em', null, mdInline(m[10] || m[11])));
    else if (m[12]) out.push(h('del', null, mdInline(m[12])));
    else if (m[13]) { const href = safeHref(m[13]); out.push(href ? mdLink(href, m[13]) : m[13]); }
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}
const MD = {head: /^(#{1,6})\s+(.*?)\s*#*\s*$/, fence: /^\s*(```|~~~)/, hr: /^\s*([-*_])(\s*\1){2,}\s*$/, quote: /^\s*>\s?(.*)$/,
            item: /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/};
const mdIndent = s => s.replace(/\t/g, '    ').length;
function markdown(src) {
  const lines = String(src || '').replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  const lineBreaks = ls => ls.flatMap((l, k) => k ? [h('br'), ...mdInline(l.trim())] : mdInline(l.trim()));
  const startsBlock = l => MD.head.test(l) || MD.fence.test(l) || MD.quote.test(l) || MD.item.test(l) || MD.hr.test(l);
  const listItem = text => {
    const t = text.match(/^\[([ xX])\]\s+(.*)$/);
    if (!t) return h('li', null, mdInline(text));
    const done = t[1] !== ' ';
    return h('li', {class: 'md-task' + (done ? ' done' : '')}, h('span', {class: 'md-box', 'aria-hidden': 'true'}, done ? '☑' : '☐'),
      h('span', {class: 'sr'}, done ? 'Done: ' : 'To do: '), h('span', null, mdInline(t[2])));
  };
  const list = start => {
    const first = lines[start].match(MD.item), indent = mdIndent(first[1]), ordered = /\d/.test(first[2]);
    const el = h(ordered ? 'ol' : 'ul', {class: 'md-list', start: ordered && parseInt(first[2], 10) !== 1 ? parseInt(first[2], 10) : null});
    let i = start, li = null;
    while (i < lines.length) {
      const l = lines[i];
      if (!l.trim()) {
        let j = i + 1;
        while (j < lines.length && !lines[j].trim()) j++;
        const nm = j < lines.length && lines[j].match(MD.item);
        if (nm && mdIndent(nm[1]) >= indent) { i = j; continue; }
        break;
      }
      const m = l.match(MD.item);
      if (m && mdIndent(m[1]) < indent) break;
      if (m && mdIndent(m[1]) > indent && li) { const [sub, j] = list(i); li.append(sub); i = j; continue; }
      if (m && mdIndent(m[1]) === indent) {
        if (/\d/.test(m[2]) !== ordered) break;
        li = listItem(m[3]); el.append(li); i++; continue;
      }
      if (li && !m && mdIndent(l.match(/^\s*/)[0]) > indent) { add(li, [h('br'), mdInline(l.trim())]); i++; continue; }
      break;
    }
    return [el, i];
  };
  let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    let m;
    if (!l.trim()) { i++; continue; }
    if (MD.fence.test(l)) {
      const fence = l.trim().slice(0, 3), buf = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence)) buf.push(lines[i++]);
      i++;
      out.push(h('pre', {class: 'md-pre'}, h('code', null, buf.join('\n'))));
    } else if ((m = l.match(MD.head))) {
      out.push(h('h' + Math.min(6, m[1].length + 2), {class: 'md-h md-h' + m[1].length}, mdInline(m[2])));
      i++;
    } else if (MD.hr.test(l)) { out.push(h('hr', {class: 'md-hr'})); i++; }
    else if (MD.quote.test(l)) {
      const buf = [];
      while (i < lines.length && (m = lines[i].match(MD.quote))) { buf.push(m[1]); i++; }
      // An Obsidian callout (> [!note] Title) keeps its title as the quote's first line.
      const c = (buf[0] || '').match(/^\[!(\w+)\][+-]?\s*(.*)$/);
      if (c) buf[0] = '**' + (c[2] || c[1].charAt(0).toUpperCase() + c[1].slice(1)) + '**';
      out.push(h('blockquote', {class: 'md-quote'}, markdown(buf.join('\n'))));
    } else if (MD.item.test(l)) { const [el, j] = list(i); out.push(el); i = j; }
    else {
      const buf = [];
      while (i < lines.length && lines[i].trim() && !(buf.length && startsBlock(lines[i]))) buf.push(lines[i++]);
      out.push(h('p', null, lineBreaks(buf)));
    }
  }
  return out;
}
const MEMBER_LABEL = {message: ['message', 'messages'], thread: ['thread', 'threads'], attachment: ['attachment', 'attachments'], person: ['person', 'people'],
                      org: ['organisation', 'organisations'], event: ['event', 'events'], object: ['object', 'objects']};
const plural = (n, kind) => `${fmt(n)} ${(MEMBER_LABEL[kind] || [kind, kind])[n === 1 ? 0 : 1]}`;
const countsText = counts => Object.keys(MEMBER_LABEL).filter(k => counts[k]).map(k => plural(counts[k], k)).join(' · ');
const sourceName = src => src === 'human' ? 'manual' : src === 'query' ? 'query' : src.startsWith('rule:') ? 'rule ' + src.slice(5).split('@')[0] : src;
function sourceText(sources, threadSources) {
  const own = (sources || []).map(sourceName);
  const th = (threadSources || []).map(x => 'thread: ' + sourceName(x));
  return own.concat(th).join(', ');
}
function sourcePill(src) {
  const cls = src === 'human' ? 'pill' : src === 'query' ? 'pill ok' : 'pill ac';
  return h('span', {class: cls, title: src}, sourceName(src));
}
function conditionChips(conds) {
  return h('div', {class: 'conds'}, (conds || []).map((c, i) => [i ? h('span', {class: 'muted small'}, 'and') : null,
    h('span', {class: 'fld'}, c.field), h('span', {class: 'muted small'}, c.op.replace('_', ' ')), c.value != null ? h('span', {class: 'fld v'}, JSON.stringify(c.value)) : null]));
}

// ---------------------------------------------------------------- binders (Binders › All binders)
// Every binder with its kind, members and open work, and the form for a new one; with a binder chosen
// (S.obj) its own page (viewObject).
async function viewObjects() {
  if (S.obj) return viewObject(S.obj);
  const rows = await api('/api/objects' + (S.objArchived ? '?archived=1' : ''));
  const name = h('input', {class: 'field', id: 'obj-name', placeholder: 'For example: Insurance', 'aria-label': 'Name', maxlength: '200'});
  const kind = h('select', {class: 'sel', 'aria-label': 'Kind'}, KIND_ORDER.map(k => h('option', {value: k, selected: k === 'collection'}, KIND_LABEL[k] || k)));
  const query = h('textarea', {class: 'json', 'aria-label': 'Stored query as JSON', placeholder: '[{"field": "from_domain", "op": "is", "value": "example.se"}]'});
  const msg = h('div', {class: 'small'});
  const create = async () => {
    let q = null;
    try { if (query.value.trim()) q = JSON.parse(query.value); } catch (e) { msg.replaceChildren(h('span', {class: 'err'}, 'The stored query is not valid JSON.')); return; }
    try {
      const o = await post('/api/objects', {kind: kind.value, name: name.value, query: q});
      go('objects', o.id);
    } catch (e) { msg.replaceChildren(h('span', {class: 'err'}, String(e.message || e))); }
  };
  return h('div', null,
    header(withHelp('All binders', 'binders'), `${fmt(rows.length)} ${S.objArchived ? 'binders and other objects, archived included' : 'binders and other objects'}`,
      seg([['', 'Active'], ['1', 'With archived']], S.objArchived, v => { S.objArchived = v; render(); })),
    h('p', {class: 'lead'}, 'Projects, areas, topics and systems first, then cases, collections and saved searches. Members come from rules, from your own additions and exclusions, and from a stored query that is evaluated live; a binder\'s work items are on its Board.'),
    h('div', {class: 'grid g-hero', style: 'margin-top:16px'},
      card('All objects', 'Click one to see its members and what they have in common', null,
        kindLegend(kindsIn(rows.map(o => o.kind)), 'stripe'),
        rows.length ? h('div', {class: 'tw'}, h('table', {class: 't objlist'},
          h('thead', null, h('tr', null, h('th', null, 'Name'), h('th', null, 'Members'), h('th', {class: 'r'}, 'Open work'), h('th', {class: 'r'}, 'Total'))),
          // Grouped by kind: the binders (projects, areas, topics, systems) first. Each group and
          // each row carries its kind's stripe.
          h('tbody', null, [...KIND_ORDER, ...new Set(rows.map(o => o.kind).filter(k => !KIND_ORDER.includes(k)))].map(k => {
            const group = rows.filter(o => o.kind === k);
            return group.length ? [h('tr', {class: 'grp ' + kindCls(k)}, h('th', {colspan: '4', scope: 'colgroup'},
                h('span', {class: 'kl'}, h('span', {class: 'kswatch', 'aria-hidden': 'true'}), KIND_PLURAL[k] || KIND_LABEL[k] || k), h('span', {class: 'n'}, fmt(group.length)))),
              group.map(o => h('tr', {class: 'click ' + kindCls(k), tabindex: '0', onclick: () => go('objects', o.id), onkeydown: e => { if (e.key === 'Enter') go('objects', o.id); }},
                h('td', {class: 'nm kstripe'}, o.name, o.query ? h('span', {class: 'pill ok', style: 'margin-left:6px'}, 'query') : null,
                  o.archived ? h('span', {class: 'pill md', style: 'margin-left:4px'}, 'archived') : null,
                  h('small', null, o.description ? mdInline(o.description.split('\n')[0]) : o.query ? 'Live query' : '')),
                h('td', {class: 'small muted'}, countsText(o.counts) || '—'),
                h('td', {class: 'r'}, o.work_open ? fmt(o.work_open) : h('span', {class: 'muted'}, '—')),
                h('td', {class: 'r'}, fmt(o.members))))] : null;
          })))) :
          emptyPage('No objects yet', 'Create one here, add a message to one from an open message, or promote an organisation from an object\'s "In common" panel.')),
      card('New object', 'A stored query is optional; it uses the same conditions as rules', h('button', {class: 'btn sm primary', onclick: create}, 'Create'),
        h('div', {class: 'objnew'},
          h('label', {class: 'flabel', for: 'obj-name'}, 'Name'), name,
          h('label', {class: 'flabel'}, 'Kind'), kind,
          h('details', null, h('summary', {class: 'small'}, 'Stored query (optional)'), query),
          msg))));
}

let objPage = {id: null, rows: [], total: 0};

async function viewObject(id) {
  const params = k => { const p = new URLSearchParams({limit: 100, offset: k}); if (S.objNested) p.set('recursive', '1'); if (S.objKind) p.set('kind', S.objKind); return p; };
  const o = await api(`/api/objects/${id}?` + params(0));
  objPage = {id, rows: o.members.rows, total: o.members.total};
  const counts = o.members.counts, all = Object.values(counts).reduce((a, b) => a + b, 0);
  const change = async (action, ids) => {
    try { await post(`/api/objects/${id}/members`, {action, ids}); render(); } catch (e) { alert(String(e.message || e)); }
  };
  const update = async body => { try { await post(`/api/objects/${id}`, body); render(); } catch (e) { alert(String(e.message || e)); } };
  const openMember = r => {
    if (r.kind === 'message') openMessage(r.entity_id);
    else if ((r.kind === 'attachment' || r.kind === 'event') && r.message_id) openMessage(r.message_id);
    else if (r.kind === 'thread') { S.f = {...blank(), thread: r.entity_id}; go('messages'); }
    else if (r.kind === 'object') go('objects', r.entity_id);
    else if (r.kind === 'org') { S.f = {...blank(), domain: r.detail}; go('messages'); }
    else if (r.kind === 'person') { S.f = {...blank(), q: r.detail}; go('messages'); }
  };
  const tbody = h('tbody');
  const more = h('div');
  const draw = () => {
    const memberKey = r => r.kind === 'message' ? 'm:' + r.entity_id : (r.kind === 'attachment' || r.kind === 'event') && r.message_id ? 'm:' + r.message_id : null;
    tbody.replaceChildren(...objPage.rows.map(r => h('tr', {class: 'click', tabindex: '0', 'data-pane-key': memberKey(r), onclick: () => openMember(r), onkeydown: e => { if (e.key === 'Enter') openMember(r); }},
      h('td', null, h('span', {class: 'pill mono'}, r.kind)),
      h('td', {class: 'nm'}, r.label, h('small', null, r.detail || '')),
      h('td', null, h('div', {class: 'srcs'}, r.sources.map(sourcePill), r.via_names.map(n => h('span', {class: 'pill', title: 'Through a nested object'}, 'via ' + n)))),
      h('td', {class: 'r muted'}, when(r.happened_at)),
      h('td', {class: 'r act'},
        r.sources.includes('human') && !r.via.length ? h('button', {class: 'btn ghost sm', title: 'Undo the manual addition', onclick: e => { e.stopPropagation(); change('remove', [r.entity_id]); }}, 'Remove') : null,
        h('button', {class: 'btn ghost sm', title: 'Keep it out, whatever rules or the query say', onclick: e => { e.stopPropagation(); change('exclude', [r.entity_id]); }}, 'Exclude')))));
    fill(more, objPage.rows.length < objPage.total ? h('div', {class: 'more'}, h('button', {class: 'btn', onclick: async () => {
      const next = await api(`/api/objects/${id}?common=0&` + params(objPage.rows.length));
      objPage.rows = objPage.rows.concat(next.members.rows); draw();
    }}, `Load more (${fmt(objPage.total - objPage.rows.length)} left)`)) : null);
  };
  draw();
  const kinds = Object.keys(MEMBER_LABEL).filter(k => counts[k]);
  const about = BINDER_KINDS.includes(o.kind) || o.body || (o.notes || []).length;
  const notes = (o.notes || []).length ? notesCard(o) : null;
  return h('div', null,
    h('div', {class: 'ohead ' + kindCls(o.kind)},
      header(withHelp(o.name, 'binder-page'), [kindBadge(o.kind), h('span', null, `${fmt(all)} ${all === 1 ? 'member' : 'members'}`), o.archived ? h('span', {class: 'pill md'}, 'archived') : null], [
        h('button', {class: 'btn ghost sm', onclick: () => go('objects')}, '‹ All objects'),
        seg([['', 'Direct'], ['1', 'Include nested']], S.objNested, v => { S.objNested = v; render(); }),
        h('button', {class: 'btn sm', onclick: () => { const n = prompt('New name', o.name); if (n && n.trim() && n !== o.name) update({name: n}); }}, 'Rename'),
        h('button', {class: 'btn sm', onclick: () => { const d = prompt('Description', o.description || ''); if (d !== null) update({description: d}); }}, 'Describe'),
        h('button', {class: 'btn sm', onclick: () => update({archived: !o.archived})}, o.archived ? 'Unarchive' : 'Archive')])),
    (o.parents || []).length ? h('div', {class: 'meta', style: 'margin-top:10px'}, 'Part of',
      o.parents.map(pa => h('button', {class: 'kbadge kbtn ' + kindCls(pa.kind), title: `${KIND_LABEL[pa.kind] || pa.kind}: ${pa.name}`, onclick: () => go('objects', pa.id)}, pa.name))) : null,
    o.query ? h('div', {class: 'qline'}, h('span', {class: 'small muted'}, 'Live query:'), conditionChips(o.query)) : null,
    about ? aboutCard(o) : o.description ? h('p', {class: 'lead'}, o.description) : null,
    binderPanels(o),
    binderSpace(o),
    h('div', {class: 'grid ' + (notes ? 'g-hero' : ''), style: 'margin-top:16px'}, activityCard(o), notes),
    h('div', {class: 'grid g-hero'},
      h('section', {class: 'card'},
        h('div', {class: 'card-h'}, h('div', null, h('h2', null, withHelp('Members', 'binder-members')), h('p', null, 'Manual additions, rules and the stored query; exclusions win over all of them')),
          kinds.length > 1 ? seg([['', 'All'], ...kinds.map(k => [k, plural(counts[k], k)])], S.objKind, v => { S.objKind = v; render(); }) : null),
        objPage.rows.length ? h('div', {class: 'tw'}, h('table', {class: 't'},
          h('thead', null, h('tr', null, h('th', null, 'Kind'), h('th', null, 'Member'), h('th', null, 'From'), h('th', {class: 'r'}, 'Date'), h('th', null))), tbody)) :
          empty('No members yet', 'Add messages or threads from an open message, point a rule at this object, or give it a stored query.'),
        more,
        (o.excluded || []).length ? h('div', {class: 'dsec'}, h('h4', null, `Excluded · ${o.excluded.length}`),
          h('table', {class: 't'}, h('tbody', null, o.excluded.map(x => h('tr', null,
            h('td', null, h('span', {class: 'pill mono'}, x.kind)), h('td', {class: 'nm'}, x.label, h('small', null, x.detail || '')),
            h('td', {class: 'r act'}, h('button', {class: 'btn ghost sm', onclick: () => change('unexclude', [x.entity_id])}, 'Let back in'))))))) : null),
      commonCard(o.common)));
}

// A binder (project, area, topic, system, personal project) shows its Board.
function binderPanels(o) {
  const items = o.work || [];
  if (!BINDER_KINDS.includes(o.kind) && !items.length) return null;
  const open = items.filter(w => w.status !== 'done').length;
  return h('section', {class: 'card', style: 'margin-top:16px'},
    h('div', {class: 'card-h'}, h('div', null, h('h2', null, 'Board'), h('p', null, `${fmt(open)} open, ${fmt(items.length - open)} done · drag a card, or focus it and press m to move it`)),
      h('button', {class: 'btn sm primary', onclick: () => newWork({home_id: o.id})}, 'New work item')),
    items.length ? [workLegend(items, false), workBoard(items, {showHome: false, homeId: o.id})] : empty('No work items yet', 'Make one here, or from a message with "Make a work item".'));
}

// ---- About: the binder's purpose, its own text in tidy sections, and a facts panel.
// Sections the vault's binder template uses; a leading "Purpose: …" paragraph counts as one too.
const ABOUT_SECTIONS = ['purpose', 'outcome', 'desired outcome', 'current context', 'context', 'what belongs here', 'what does not belong here',
                        'scope', 'state', 'next steps', 'goal', 'goals', 'background', 'notes'];
function bodySections(body) {
  const lines = String(body || '').replace(/\r\n?/g, '\n').split('\n');
  let fence = false, top = 7;
  for (const l of lines) { if (MD.fence.test(l)) fence = !fence; else if (!fence) { const m = l.match(MD.head); if (m) top = Math.min(top, m[1].length); } }
  const secs = [{title: null, lines: []}];
  fence = false;
  for (const l of lines) {
    if (MD.fence.test(l)) fence = !fence;
    const m = !fence && l.match(MD.head);
    if (m && m[1].length === top) secs.push({title: m[2], lines: []}); else secs[secs.length - 1].lines.push(l);
  }
  const out = [];
  for (const sec of secs) {
    const text = sec.lines.join('\n').trim();
    if (sec.title) { out.push({title: sec.title, text}); continue; }
    if (!text) continue;
    let cur = {title: null, parts: []};
    for (const para of text.split(/\n\s*\n/)) {
      const m = para.match(/^([A-Za-z][A-Za-z ]{1,40}?):\s+([\s\S]+)$/);
      if (m && ABOUT_SECTIONS.includes(m[1].toLowerCase())) {
        if (cur.title || cur.parts.length) out.push({title: cur.title, text: cur.parts.join('\n\n')});
        cur = {title: m[1], parts: [m[2]]};
      } else cur.parts.push(para);
    }
    if (cur.title || cur.parts.length) out.push({title: cur.title, text: cur.parts.join('\n\n')});
  }
  return out;
}
const HEALTH_PILL = {ok: 'pill ok', healthy: 'pill ok', green: 'pill ok', degraded: 'pill md', warning: 'pill md', yellow: 'pill md',
                     down: 'pill hi', failed: 'pill hi', critical: 'pill hi', red: 'pill hi'};
const factValue = v => v === true ? 'yes' : v === false ? 'no' : v == null || v === '' ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v);
const asDate = v => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(v) ? new Date(v).toLocaleString('sv-SE', {dateStyle: 'short', timeStyle: 'short'}) : factValue(v);
function factsPanel(o) {
  const a = o.attrs || {}, st = a.state || {}, f = o.facts || {}, origin = o.origin || {};
  const row = (label, value) => value == null || value === '' ? null : [h('dt', null, label), h('dd', null, value)];
  const count = (n, label, sub) => h('div', {class: 'fcount'}, h('b', null, fmt(n)), h('span', null, label), sub ? h('small', null, sub) : null);
  const extra = Object.entries(a.frontmatter || {}).filter(([, v]) => v != null && typeof v !== 'object').slice(0, 8);
  const link = safeHref(st.source_link || '');
  return h('aside', {class: 'facts', 'aria-label': 'Facts'},
    h('div', {class: 'fcounts'}, count(f.work_open, 'open', 'work items'), count(f.work_done, 'done', 'work items'),
      count(f.messages, f.messages === 1 ? 'message' : 'messages', 'linked'), count(f.notes, f.notes === 1 ? 'note' : 'notes', 'attached')),
    h('dl', {class: 'kv'},
      row('Lifecycle', a.lifecycle ? h('span', {class: 'pill'}, a.lifecycle) : null),
      row('Status', a.status ? discStatusPill(a.status) : null),
      row('Vendor', a.vendor), row('Category', a.category),
      row('Seen', a.first_seen || a.last_seen ? `${a.first_seen || '?'} – ${a.last_seen || '?'}` : null),
      row('Created', a.created ? asDate(a.created) : day(o.created_at)),
      row('Updated', day(o.updated_at)),
      origin.source === 'obsidian' ? row('Imported', [day(origin.imported_at), origin.path ? h('small', {class: 'fpath'}, origin.path) : null]) : null,
      origin.source === 'discovery' ? row('From discovery', [day(origin.imported_at), origin.file ? h('small', {class: 'fpath'}, origin.file) : null]) : null,
      extra.map(([k, v]) => { const l = k.replace(/[-_]/g, ' '); return row(l.charAt(0).toUpperCase() + l.slice(1), factValue(v)); })),
    Object.keys(st).length ? h('div', {class: 'fstate'}, h('h4', null, 'State'),
      h('dl', {class: 'kv'},
        row('Health', st.health ? h('span', {class: HEALTH_PILL[String(st.health).toLowerCase()] || 'pill'}, st.health) : null),
        row('Version', st.version != null ? h('span', {class: 'num'}, factValue(st.version)) : null),
        row('Update available', st.update_available != null ? (st.update_available ? h('span', {class: 'pill md'}, 'yes') : 'no') : null),
        row('Security findings', st.security_findings != null ? (Number(st.security_findings) ? h('span', {class: 'pill hi'}, factValue(st.security_findings)) : factValue(st.security_findings)) : null),
        row('Observed', st.observed_at ? asDate(st.observed_at) : null),
        row('Source', st.source_link ? (link ? mdLink(link, new URL(link).host) : factValue(st.source_link)) : null)),
      st.note ? h('div', {class: 'md fnote'}, markdown(st.note)) : null) : null);
}
function aboutCard(o) {
  const secs = bodySections(o.body);
  const pi = secs.findIndex(x => x.title && x.title.toLowerCase() === 'purpose');
  const purpose = pi >= 0 ? secs.splice(pi, 1)[0].text : o.description;
  const box = h('div', {class: 'about-main'});
  const editBtn = h('button', {class: 'btn sm', onclick: () => edit()}, o.body ? 'Edit' : 'Write');
  const read = () => {
    editBtn.style.display = '';
    fill(box,
      purpose ? h('div', {class: 'about-lead md'}, markdown(purpose)) : null,
      secs.length ? h('div', {class: 'about-secs'}, secs.map(x => h('section', {class: 'asec' + (x.title ? '' : ' intro')},
        x.title ? h('h3', {class: 'asec-h'}, x.title) : null, h('div', {class: 'md'}, markdown(x.text))))) :
        !purpose ? h('div', {class: 'small muted about-empty'}, 'No text yet. Write the purpose, what belongs here and the outcome; Markdown works.') : null);
  };
  const edit = () => {
    editBtn.style.display = 'none';
    const ta = h('textarea', {class: 'field wbody about-edit', 'aria-label': 'The binder\'s text, in Markdown', rows: '16'}, o.body || '');
    const err = h('div', {class: 'err', role: 'alert'});
    const saveBtn = h('button', {class: 'btn sm primary', onclick: () => save()}, 'Save');
    const save = async () => {
      if (ta.value === (o.body || '')) { read(); return; }
      saveBtn.disabled = true;
      try { await post(`/api/objects/${o.id}`, {body: ta.value}); } catch (e) { saveBtn.disabled = false; err.replaceChildren(errText(e)); return; }
      await render();
      flash('Saved.');
    };
    fill(box, h('div', {class: 'about-editor', onkeydown: e => {
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); save(); }
        else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); read(); editBtn.focus(); }
      }}, ta,
      h('div', {class: 'rrow'}, saveBtn, h('button', {class: 'btn sm ghost', onclick: () => { read(); editBtn.focus(); }}, 'Cancel'),
        h('span', {class: 'small muted'}, 'Markdown: ## headings, lists, **bold**, *italic*, `code`, [links](https://…) · ⌘/Ctrl + Enter saves · Esc cancels')), err));
    ta.focus();
  };
  read();
  return h('section', {class: 'card about ' + kindCls(o.kind)},
    h('div', {class: 'about-top'}, kindBadge(o.kind), (o.attrs || {}).lifecycle ? h('span', {class: 'pill'}, o.attrs.lifecycle) : null,
      h('h2', {class: 'about-name'}, 'About ', o.name), h('span', {class: 'sp'}), editBtn),
    h('div', {class: 'about-grid'}, box, factsPanel(o)));
}

// ---- Notes: what came over from the vault (receipts, state, sources, decisions, research …).
const NOTES_UI = {obj: null, kind: '', all: false};
const NOTES_SHOWN = 30;
function notesCard(o) {
  if (NOTES_UI.obj !== o.id) Object.assign(NOTES_UI, {obj: o.id, kind: '', all: false});
  const notes = o.notes || [];
  NOTE_INDEX.clear();
  notes.forEach(n => NOTE_INDEX.set(n.id, n));
  const byKind = {};
  notes.forEach(n => { byKind[n.kind] = (byKind[n.kind] || 0) + 1; });
  const list = h('div', {class: 'notes'});
  const bar = h('div');
  const draw = () => {
    const shown = notes.filter(n => !NOTES_UI.kind || n.kind === NOTES_UI.kind);
    const cut = NOTES_UI.all ? shown : shown.slice(0, NOTES_SHOWN);
    fill(bar, Object.keys(byKind).length > 1 ? h('div', {class: 'chips nfilter', role: 'group', 'aria-label': 'Note kinds'},
      [['', 'All', notes.length], ...Object.entries(byKind).sort((x, y) => y[1] - x[1]).map(([k, n]) => [k, k, n])].map(([k, label, n]) =>
        h('button', {class: 'chipbtn', 'aria-pressed': String(NOTES_UI.kind === k), onclick: () => { NOTES_UI.kind = k; NOTES_UI.all = false; draw(); }}, label, h('span', {class: 'n'}, fmt(n))))) : null);
    fill(list, cut.map(n => h('div', {class: 'note-i', id: 'note-' + n.id, tabindex: '0', role: 'button', 'data-pane-key': 'n:' + n.id,
        onclick: () => openNote(n.id), onkeydown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openNote(n.id); } }},
      h('span', {class: 'pill mono'}, n.kind), h('span', {class: 'note-t'}, n.title), h('span', {class: 'note-d', title: new Date(n.created_at).toLocaleString('sv-SE')}, relTime(n.created_at)))),
      cut.length < shown.length ? h('button', {class: 'linkbtn', onclick: () => { NOTES_UI.all = true; draw(); }}, `Show all ${fmt(shown.length)}`) : null);
  };
  draw();
  return card(`Notes · ${fmt(notes.length)}`, 'Decisions, research, state, sources and receipts; open one to read it', null, bar, list);
}
// A binder's notes come with the binder; the pane shows one from there.
const NOTE_INDEX = new Map();
function openNote(id) {
  const n = NOTE_INDEX.get(id);
  openPane(n ? h('div', null,
      h('div', {class: 'meta', style: 'margin:0 0 6px'}, h('span', {class: 'pill mono'}, n.kind), '·',
        h('span', {title: new Date(n.created_at).toLocaleString('sv-SE')}, relTime(n.created_at))),
      h('h2', null, n.title),
      h('div', {class: 'md note-b'}, n.body.trim() ? markdown(n.body) : h('span', {class: 'muted'}, 'No text.')),
      n.path ? h('div', {class: 'small muted fpath'}, 'From the vault: ', n.path) : null)
    : h('div', {class: 'muted'}, 'This note is not among the ones loaded with the page.'),
  {key: 'n:' + id, title: 'Note', label: n ? n.title : 'Note'});
}

// ---- Activity: the binder's timeline, newest first.
function relTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso), now = new Date(), secs = (now - d) / 1000;
  if (secs < -60) return day(iso);
  if (secs < 90) return 'just now';
  if (secs < 3600) return `${Math.round(secs / 60)} min ago`;
  const days = Math.round((new Date(now.toDateString()) - new Date(d.toDateString())) / 864e5);
  if (days === 0) return `${Math.round(secs / 3600)} h ago`;
  if (days === 1) return 'yesterday';
  if (days < 7) return `${days} days ago`;
  if (days < 30) { const w = Math.round(days / 7); return w === 1 ? 'a week ago' : `${w} weeks ago`; }
  if (days < 365) { const m = Math.max(1, Math.round(days / 30.4)); return m === 1 ? 'a month ago' : `${m} months ago`; }
  const y = Math.round(days / 365); return y === 1 ? 'a year ago' : `${y} years ago`;
}
function periodOf(iso) {
  const d = new Date(iso), now = new Date();
  const days = Math.round((new Date(now.toDateString()) - new Date(d.toDateString())) / 864e5);
  if (days <= 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days < 7) return 'This week';
  return d.toLocaleDateString('en-GB', {month: 'long', year: 'numeric'});
}
const ACT = {obj: null, show: 'all'};
const byText = by => !by ? null : by === OWNER.id ? 'by you' : by === 'import:vault' ? 'from the vault import' : 'by ' + by;
function activityItem(x) {
  let dot, line, meta = [], open = null, key = null;
  if (x.type === 'work') {
    open = () => openWork(x.work_item_id);
    key = 'w:' + x.work_item_id;
    const t = h('b', null, x.title);
    if (x.event === 'created') {
      dot = STATUS_DOT[x.created_status || 'inbox'];
      line = ['Work item ', t, ' made in ', statusPill(x.created_status || 'inbox')];
    } else if (x.event === 'status') {
      dot = STATUS_DOT[x.new_value] || 'var(--accent)';
      line = [t, ' ', statusPill(x.old_value), h('span', {class: 'arrow', 'aria-label': 'to'}, '→'), statusPill(x.new_value)];
    } else {
      dot = 'var(--axis)';
      const homes = [{id: x.old_value, name: x.old_name || '#' + x.old_value}, {id: x.new_value, name: x.new_name || '#' + x.new_value}];
      line = [t, h('span', {class: 'muted'}, ' · '), historyText({field: x.event, old_value: x.old_value, new_value: x.new_value}, homes)];
    }
    meta.push(byText(x.by));
  } else if (x.type === 'message') {
    open = () => openMessage(x.message_id);
    key = 'm:' + x.message_id;
    dot = acctColor(x.account_id);
    line = [h('b', null, x.title)];
    meta.push(x.direction === 'out' ? 'sent' : x.from ? 'from ' + x.from : null, acctName(x.account_id) + (isTeams(x) ? ' · Teams' : ''),
      (x.via_work || []).length ? 'via ' + x.via_work.join(', ') : 'filed here');
  } else if (x.type === 'note') {
    open = () => openNote(x.note_id);
    key = 'n:' + x.note_id;
    dot = null;
    line = [h('span', {class: 'pill mono'}, x.kind), ' ', h('b', null, x.title), h('span', {class: 'muted'}, x.event === 'updated' ? ' updated' : ' added')];
  } else {
    dot = null;
    line = x.event === 'imported' ? ['Imported from the Obsidian vault'] : [KIND_LABEL[x.kind] || 'Object', ' created'];
    if (x.path) meta.push(x.path);
  }
  const full = new Date(x.at).toLocaleString('sv-SE', {dateStyle: 'medium', timeStyle: 'short'});
  return h('li', {class: 'tl-i tl-' + x.type + (open ? ' click' : ''), tabindex: open ? '0' : null, role: open ? 'button' : null, 'data-pane-key': key,
      onclick: open, onkeydown: open ? e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } } : null},
    h('span', {class: 'tl-dot' + (dot ? '' : ' hollow'), style: dot ? `background:${dot}` : null, 'aria-hidden': 'true'}),
    h('div', {class: 'tl-b'}, h('div', {class: 'tl-t'}, line), meta.filter(Boolean).length ? h('div', {class: 'tl-m'}, meta.filter(Boolean).join(' · ')) : null),
    h('time', {class: 'tl-when', datetime: x.at, title: full}, relTime(x.at)));
}
function activityCard(o) {
  if (ACT.obj !== o.id) Object.assign(ACT, {obj: o.id, show: 'all'});
  const list = h('div', {class: 'tl-wrap', 'aria-live': 'polite'}, h('div', {class: 'small muted'}, 'Loading…'));
  const filters = h('span');
  let items = [], next = null, token = 0;
  const draw = () => {
    const groups = [];
    for (const x of items) {
      const p = periodOf(x.at);
      if (!groups.length || groups[groups.length - 1][0] !== p) groups.push([p, []]);
      groups[groups.length - 1][1].push(x);
    }
    fill(list, items.length ? groups.map(([p, xs]) => h('div', {class: 'tl-grp'}, h('h4', {class: 'tl-h'}, p), h('ol', {class: 'tl'}, xs.map(activityItem)))) :
      empty('Nothing yet', ACT.show === 'all' ? 'Work, mail and notes show up here as they happen.' : 'Nothing of this kind yet.'),
      next ? h('div', {class: 'more'}, h('button', {class: 'btn sm', onclick: () => load(true)}, 'Show older')) : null);
    if (list.isConnected) paneMark();  // the timeline arrives after the page: mark its open item
  };
  const load = async append => {
    const my = ++token;
    const p = new URLSearchParams({show: ACT.show, limit: '60'});
    if (append && next) p.set('before', next);
    let res;
    try { res = await api(`/api/objects/${o.id}/activity?` + p); } catch (e) { if (my === token) fill(list, h('div', {class: 'err'}, errText(e))); return; }
    if (my !== token) return;
    items = append ? items.concat(res.items) : res.items;
    next = res.next;
    draw();
  };
  const drawFilters = () => fill(filters, seg([['all', 'All'], ['work', 'Work'], ['messages', 'Messages'], ['notes', 'Notes']], ACT.show,
    v => { ACT.show = v; drawFilters(); load(false); }));
  drawFilters();
  load(false);
  return card('Activity', 'What has happened here, newest first', filters, list);
}

function commonCard(c) {
  if (!c || !c.messages) return card('In common', 'What the member mail shares', null, empty('Nothing to compare', 'The panel fills in once the object holds messages or threads.'));
  const maxP = Math.max(1, ...c.people.map(p => p.threads));
  const promote = async org => {
    try { const o = await post('/api/objects', {promote_org: org.id}); go('objects', o.id); } catch (e) { alert(String(e.message || e)); }
  };
  return card('In common', `${plural(c.messages, 'message')} in ${plural(c.threads, 'thread')} · ${day(c.first_at)} – ${day(c.last_at)}`, null,
    c.orgs.length ? h('div', {class: 'dsec', style: 'margin-top:0'}, h('h4', null, 'Organisations'),
      h('table', {class: 't'}, h('tbody', null, c.orgs.map(g => h('tr', null,
        h('td', {class: 'nm'}, g.name, h('small', null, `${g.domain} · ${plural(g.people, 'person')}`)),
        h('td', {class: 'r'}, `${fmt(g.threads)} / ${fmt(c.threads)}`, h('div', {class: 'small muted'}, 'threads')),
        h('td', {class: 'r act'}, h('button', {class: 'btn ghost sm', title: 'A project of all mail from ' + g.domain, onclick: () => promote(g)}, 'Make project'))))))) : null,
    c.people.length ? h('div', {class: 'dsec'}, h('h4', null, 'People'),
      h('div', {class: 'hbars'}, c.people.map(p => h('button', {class: 'hbar', title: `${p.address} · ${(p.roles || []).join(', ')}`, onclick: () => { S.f = {...blank(), q: p.address}; go('messages'); }},
        h('span', {class: 'lab'}, p.name), h('span', {class: 'track'}, h('span', {class: 'fill', style: `display:block;width:${100 * p.threads / maxP}%;background:var(--accent)`})),
        h('span', {class: 'val'}, fmt(p.threads)))))) : null,
    c.labels.length ? h('div', {class: 'dsec'}, h('h4', null, 'Labels'),
      h('div', {class: 'chips'}, c.labels.map(l => h('button', {class: 'chipbtn', onclick: () => { S.f = {...blank(), label: l.label}; go('messages'); }}, l.label, h('span', {class: 'n'}, fmt(l.messages)))))) : null,
    c.folders.length ? h('div', {class: 'dsec'}, h('h4', null, 'Folders'),
      h('div', {class: 'chips'}, c.folders.map(f => h('span', {class: 'chipbtn', title: f.account_id}, f.folder, h('span', {class: 'n'}, fmt(f.messages)))))) : null,
    c.attachment_types.length ? h('div', {class: 'dsec'}, h('h4', null, 'Attachments'),
      h('table', {class: 't'}, h('tbody', null, c.attachment_types.map(a => h('tr', null, h('td', null, a.content_type), h('td', {class: 'r'}, fmt(a.n)), h('td', {class: 'r muted'}, bytes(a.bytes))))))) : null);
}

// ---------------------------------------------------------------- work items
// The place the owner acts: work items on a board (by status), in a list, in the reading pane, and from a
// message. The status vocabulary is Obsidian Talos's, kept exactly.
const STATUSES = ['inbox', 'next', 'doing', 'blocked', 'someday', 'done'];
const STATUS_LABEL = {inbox: 'Inbox', next: 'Next', doing: 'Doing', blocked: 'Blocked', someday: 'Someday', done: 'Done'};
const STATUS_PILL = {inbox: 'pill', next: 'pill ac', doing: 'pill ac', blocked: 'pill se', someday: 'pill', done: 'pill ok'};
const STATUS_DOT = {inbox: 'var(--series-other)', next: 'var(--series-1)', doing: 'var(--series-7)', blocked: 'var(--serious)', someday: 'var(--axis)', done: 'var(--good)'};
const REASON_PILL = {overdue: 'pill hi', blocked: 'pill se', doing: 'pill ac', review: 'pill md', focus: 'pill ok', inbox: 'pill'};
const REASON_TIP = {overdue: 'The due date has passed', blocked: 'Status: blocked', doing: 'Status: doing', review: 'The review date has come',
                    focus: 'Marked as focus', inbox: 'Not filed yet: status inbox'};
// The binder kinds of Obsidian Talos come first wherever objects are grouped.
const BINDER_KINDS = ['project', 'area', 'topic', 'system', 'personal_project'];
const KIND_ORDER = ['project', 'area', 'topic', 'system', 'personal_project', 'case', 'collection', 'saved_search'];
const KIND_PLURAL = {project: 'Projects', area: 'Areas', topic: 'Topics', system: 'Systems', personal_project: 'Personal projects',
                     case: 'Cases', collection: 'Collections', saved_search: 'Saved searches'};
const DONE_SHOWN = 20;
const todayISO = () => new Date().toLocaleDateString('sv-SE');  // YYYY-MM-DD in local time, as the server's date.today()

const WORK = {mode: 'board', status: '', home: '', focus: false, overdue: false, q: '', sort: 'due', dir: 1, showDone: false, col: ''};
try { const m = localStorage.getItem('talos-work-mode'); if (m === 'board' || m === 'list') WORK.mode = m; } catch (e) {}
let workSearchTimer = null;

let toastEl = null, toastTimer = null;
function flash(text) {
  if (toastEl) toastEl.remove();
  toastEl = h('div', {class: 'toast', role: 'status'}, text);
  document.body.append(toastEl);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { if (toastEl) { toastEl.remove(); toastEl = null; } }, 4000);
}

// A home picker: "No home" (or the given first options), then the objects grouped by kind.
function homeSelect(homes, cur, attrs, first, curName) {
  const groups = KIND_ORDER.map(k => [k, homes.filter(o => o.kind === k)]).filter(([, l]) => l.length);
  const known = cur == null || cur === '' || cur === 'none' || homes.some(o => String(o.id) === String(cur));
  const sel = h('select', {class: 'sel', ...attrs},
    first || h('option', {value: ''}, 'No home'),
    known ? null : h('option', {value: cur}, curName || `#${cur}`),
    groups.map(([k, l]) => h('optgroup', {label: KIND_PLURAL[k] || k}, l.map(o => h('option', {value: o.id}, o.name)))));
  sel.value = cur == null ? '' : String(cur);
  return sel;
}
const statusSelect = (cur, attrs) => {
  const sel = h('select', {class: 'sel', ...attrs}, STATUSES.map(s => h('option', {value: s}, STATUS_LABEL[s])));
  sel.value = cur || 'inbox';
  return sel;
};
const statusPill = st => h('span', {class: STATUS_PILL[st] || 'pill'}, STATUS_LABEL[st] || st);
function workLegend(rows, showHome) {
  const kinds = kindsIn([...(showHome ? rows.map(r => r.home_kind) : []), ...rows.flatMap(r => (r.related || []).map(o => o.kind))]);
  return kindLegend(kinds, 'badge', kinds.length ? (showHome ? 'Solid: the home; dashed: related. The card\'s stripe is its home\'s kind.' : 'Dashed: related binders') : null);
}
function dueChip(r) {
  if (!r.due) return null;
  const late = r.overdue != null ? r.overdue : r.status !== 'done' && r.due < todayISO();
  const soon = !late && r.status !== 'done' && (new Date(r.due) - new Date(todayISO())) / 864e5 <= 3;
  return h('span', {class: late ? 'pill hi' : soon ? 'pill md' : 'pill', title: (late ? 'Overdue: due ' : 'Due ') + r.due}, (late ? 'overdue ' : 'due ') + shortDay(r.due));
}
function reviewChip(r) {
  if (!r.review_after || r.status === 'done') return null;
  const now = r.review_due != null ? r.review_due : r.review_after <= todayISO();
  return now ? h('span', {class: 'pill md', title: 'Review after ' + r.review_after}, 'review due') : null;
}
const focusChip = r => r.focus ? h('span', {class: 'pill ok', title: 'Focus'}, 'focus') : null;
const msgCount = r => r.message_count ? h('span', {class: 'small muted', title: `${r.message_count} linked message${r.message_count === 1 ? '' : 's'}`}, `✉ ${r.message_count}`) : null;

// A position between two neighbours of a column; either may be missing.
function posBetween(prev, next) {
  if (prev && next) return (prev.position + next.position) / 2;
  if (prev) return prev.position + 1;
  if (next) return next.position - 1;
  return 1;
}
async function moveWork(id, body, refocus) {
  try { await post(`/api/work/${id}`, body); } catch (e) { flash(errText(e)); return; }
  await render();
  if (refocus) { const el = document.getElementById('wcard-' + id); if (el) el.focus(); }
}

// ---- the move menu: the keyboard's way to do what drag and drop does.
let menuEl = null, menuReturn = null;
function closeMenu(restore) {
  if (!menuEl) return;
  menuEl.remove(); menuEl = null;
  if (menuReturn) menuReturn.removeAttribute('aria-expanded');
  if (restore && menuReturn && document.contains(menuReturn)) menuReturn.focus();
}
// A menu's keys: arrows, Home and End move; Esc and Tab close it.
function menuKeys(e) {
  const btns = [...menuEl.querySelectorAll('.menu-item:not(:disabled)')];
  const at = btns.indexOf(document.activeElement);
  if (e.key === 'ArrowDown') { e.preventDefault(); btns[(at + 1) % btns.length].focus(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); btns[(at - 1 + btns.length) % btns.length].focus(); }
  else if (e.key === 'Home') { e.preventDefault(); btns[0].focus(); }
  else if (e.key === 'End') { e.preventDefault(); btns[btns.length - 1].focus(); }
  else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeMenu(true); }
  else if (e.key === 'Tab') { e.preventDefault(); closeMenu(true); }
}
// Below the anchor, or above it when there is no room.
function placeMenu(anchor) {
  const rc = anchor.getBoundingClientRect(), mh = menuEl.offsetHeight, mw = menuEl.offsetWidth;
  menuEl.style.left = Math.max(8, Math.min(rc.left, innerWidth - mw - 8)) + 'px';
  menuEl.style.top = (rc.bottom + 4 + mh > innerHeight ? Math.max(8, rc.top - mh - 4) : rc.bottom + 4) + 'px';
}
document.addEventListener('mousedown', e => { if (menuEl && !menuEl.contains(e.target)) closeMenu(); });
function moveMenu(r, col, anchor, returnTo) {
  closeMenu();
  menuReturn = returnTo || anchor;
  const i = col.findIndex(x => x.id === r.id);
  const act = (label, fn, disabled) => h('button', {class: 'menu-item', role: 'menuitem', disabled, tabindex: '-1',
    onclick: () => { closeMenu(); fn(); }}, label);
  const items = [
    ...STATUSES.filter(st => st !== r.status).map(st => act(`Move to ${STATUS_LABEL[st]}`, () => moveWork(r.id, {status: st}, true))),
    h('div', {class: 'menu-sep', role: 'separator'}),
    act('Move up', () => moveWork(r.id, {position: posBetween(col[i - 2], col[i - 1])}, true), i <= 0),
    act('Move down', () => moveWork(r.id, {position: posBetween(col[i + 1], col[i + 2])}, true), i < 0 || i >= col.length - 1),
    act('Move to the top', () => moveWork(r.id, {position: posBetween(null, col[0])}, true), i <= 0),
    h('div', {class: 'menu-sep', role: 'separator'}),
    act('Open…', () => openWork(r.id)),
  ];
  menuEl = h('div', {class: 'menu', role: 'menu', 'aria-label': 'Move ' + r.title, onkeydown: menuKeys}, h('div', {class: 'menu-h'}, r.title), items);
  document.body.append(menuEl);
  placeMenu(anchor);
  const first = menuEl.querySelector('.menu-item:not(:disabled)');
  if (first) first.focus();
}

// ---- the board: one column per status; cards drag between and within columns.
let drag = null;  // {id, status} while a card is dragged
function workCard(r, col, opts) {
  // The move menu hides behind ⋯ until the card is hovered or focused: the card itself stays clean.
  const moveBtn = h('button', {class: 'k-more', 'aria-haspopup': 'menu', 'aria-label': 'Move ' + r.title, title: 'Move to another column or up and down (keyboard: m)',
    onmousedown: e => { if (menuEl) e.stopPropagation(); }, onclick: e => { e.stopPropagation(); moveMenu(r, col, moveBtn, card); }, onkeydown: e => e.stopPropagation()}, icon('more'));
  const i = col.findIndex(x => x.id === r.id);
  const card = h('article', {class: 'kcard ' + (r.home_kind ? kindCls(r.home_kind) : 'kind-none') + (r.status === 'done' ? ' k-done' : ''), id: 'wcard-' + r.id, 'data-id': r.id, 'data-pane-key': 'w:' + r.id, draggable: 'true', tabindex: '0',
      'aria-label': `${r.title}. ${STATUS_LABEL[r.status]}${r.home_name ? ', ' + r.home_name : ''}${r.due ? ', due ' + r.due : ''}. Enter opens it; m moves it.`,
      onclick: () => openWork(r.id),
      onkeydown: e => {
        if (e.target !== e.currentTarget) return;
        const si = STATUSES.indexOf(r.status);
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openWork(r.id); }
        else if (e.key === 'm' || e.key === 'ContextMenu' || (e.key === 'F10' && e.shiftKey)) { e.preventDefault(); moveMenu(r, col, moveBtn, card); }
        else if (e.altKey && e.key === 'ArrowRight' && si < STATUSES.length - 1) { e.preventDefault(); moveWork(r.id, {status: STATUSES[si + 1]}, true); }
        else if (e.altKey && e.key === 'ArrowLeft' && si > 0) { e.preventDefault(); moveWork(r.id, {status: STATUSES[si - 1]}, true); }
        else if (e.altKey && e.key === 'ArrowUp' && i > 0) { e.preventDefault(); moveWork(r.id, {position: posBetween(col[i - 2], col[i - 1])}, true); }
        else if (e.altKey && e.key === 'ArrowDown' && i < col.length - 1) { e.preventDefault(); moveWork(r.id, {position: posBetween(col[i + 1], col[i + 2])}, true); }
      },
      ondragstart: e => {
        drag = {id: r.id, status: r.status};
        e.dataTransfer.effectAllowed = 'move';
        try { e.dataTransfer.setData('text/plain', String(r.id)); } catch (x) {}
        card.classList.add('dragging');
      },
      ondragend: () => { card.classList.remove('dragging'); drag = null; document.querySelectorAll('.col.over').forEach(c => c.classList.remove('over')); }},
    h('div', {class: 'k-top'}, h('div', {class: 'k-t'}, r.title), moveBtn),
    h('div', {class: 'k-f'},
      opts.showHome && r.home_name ? h('span', {class: 'k-home', title: `Home · ${KIND_LABEL[r.home_kind] || r.home_kind}: ${r.home_name}`}, kindDot(r.home_kind), r.home_name) : null,
      (r.related || []).length ? h('span', {class: 'k-rel', title: 'Related: ' + r.related.map(o => o.name).join(', ')}, `+${r.related.length}`) : null,
      h('span', {class: 'sp'}), focusChip(r), dueChip(r), reviewChip(r), msgCount(r)));
  return card;
}
async function dropInto(st, list, col, y) {
  if (!drag) return;
  const d = drag; drag = null;
  const all = [...list.querySelectorAll('.kcard')];
  const orig = all.findIndex(c => c.dataset.id === String(d.id));
  const cards = all.filter(c => c.dataset.id !== String(d.id));
  let at = cards.findIndex(c => { const b = c.getBoundingClientRect(); return y < b.top + b.height / 2; });
  if (at < 0) at = cards.length;
  if (d.status === st && orig === at) return;  // dropped where it was
  const item = el => col.find(x => String(x.id) === el.dataset.id);
  const prev = at > 0 ? item(cards[at - 1]) : null, next = at < cards.length ? item(cards[at]) : null;
  await moveWork(d.id, {status: st, position: posBetween(prev, next)});
}
const WORK_FOLD_DEFAULT = ['someday', 'done'];
let WORK_FOLD = new Set(WORK_FOLD_DEFAULT);
try { const x = JSON.parse(localStorage.getItem('talos-work-fold') || 'null'); if (Array.isArray(x)) WORK_FOLD = new Set(x); } catch (e) {}
function foldColumn(st, on) {
  on ? WORK_FOLD.add(st) : WORK_FOLD.delete(st);
  try { localStorage.setItem('talos-work-fold', JSON.stringify([...WORK_FOLD])); } catch (e) {}
  render(true);
}
function boardColumn(st, items, opts) {
  if (opts.fold && WORK_FOLD.has(st) && !(innerWidth < 820)) {
    const col = h('button', {class: 'col fold', style: `--sc:${STATUS_DOT[st]}`, 'aria-label': `${STATUS_LABEL[st]}: ${items.length} items. Show the column`, title: `Show ${STATUS_LABEL[st]}`,
        ondragover: e => { if (!drag) return; e.preventDefault(); col.classList.add('over'); }, ondragleave: () => col.classList.remove('over'),
        ondrop: e => { e.preventDefault(); col.classList.remove('over'); if (drag) { const d = drag; drag = null; moveWork(d.id, {status: st, position: posBetween(null, items[0])}); } },
        onclick: () => foldColumn(st, false)},
      h('span', {class: 'n num'}, fmt(items.length)), h('span', {class: 'fold-l'}, STATUS_LABEL[st]));
    return col;
  }
  let shown = items;
  if (st === 'done' && !WORK.showDone && items.length > DONE_SHOWN) {
    const recent = new Set([...items].sort((a, b) => String(b.done_at || '').localeCompare(String(a.done_at || ''))).slice(0, DONE_SHOWN).map(x => x.id));
    shown = items.filter(x => recent.has(x.id));
  }
  const list = h('div', {class: 'col-list'}, shown.map(r => workCard(r, items, opts)));
  const col = h('section', {class: 'col' + (opts.cur === st ? ' cur' : ''), 'data-st': st, style: `--sc:${STATUS_DOT[st]}`, 'aria-label': `${STATUS_LABEL[st]}: ${items.length} item${items.length === 1 ? '' : 's'}`,
      ondragover: e => { if (!drag) return; e.preventDefault(); e.dataTransfer.dropEffect = 'move'; col.classList.add('over'); },
      ondragleave: e => { if (!col.contains(e.relatedTarget)) col.classList.remove('over'); },
      ondrop: e => { e.preventDefault(); col.classList.remove('over'); dropInto(st, list, items, e.clientY); }},
    h('div', {class: 'col-h'}, h('span', {class: 'dot', style: `background:${STATUS_DOT[st]}`}), STATUS_LABEL[st], h('span', {class: 'n'}, fmt(items.length)), h('span', {class: 'sp'}),
      st === 'inbox' || opts.homeId ? h('button', {class: 'btn ghost sm', title: `New work item in ${STATUS_LABEL[st]}`, 'aria-label': `New work item in ${STATUS_LABEL[st]}`,
        onclick: () => newWork({status: st, home_id: opts.homeId || null})}, '+') : null,
      opts.fold && WORK_FOLD_DEFAULT.includes(st) ? h('button', {class: 'btn ghost sm col-fold', title: `Fold ${STATUS_LABEL[st]} away`, 'aria-label': `Fold ${STATUS_LABEL[st]} away`, onclick: () => foldColumn(st, true)}, '‹') : null),
    list,
    !items.length ? h('div', {class: 'col-empty small muted'}, 'Drop a card here') : null,
    shown.length < items.length || (st === 'done' && WORK.showDone && items.length > DONE_SHOWN)
      ? h('button', {class: 'linkbtn', onclick: () => { WORK.showDone = !WORK.showDone; render(); }}, WORK.showDone ? `Show the latest ${DONE_SHOWN}` : `Show all ${fmt(items.length)}`) : null);
  return col;
}
function workBoard(rows, opts) {
  const statuses = opts.statuses || STATUSES;
  const by = Object.fromEntries(statuses.map(st => [st, []]));
  rows.forEach(r => { if (by[r.status]) by[r.status].push(r); });  // the API returns them in position order
  const cur = statuses.includes(WORK.col) ? WORK.col : statuses.includes('next') ? 'next' : statuses[0];
  const board = h('div', {class: 'board work', style: `--cols:${statuses.length}`}, statuses.map(st => boardColumn(st, by[st], {...opts, cur})));
  if (statuses.length < 2) return board;
  // On a phone: one column at a time, picked here.
  const pick = h('div', {class: 'colpick only-narrow', role: 'tablist', 'aria-label': 'Column'}, statuses.map(st => h('button', {class: 'fc' + (st === cur ? ' on' : ''), role: 'tab', 'aria-selected': String(st === cur),
    onclick: () => { WORK.col = st; render(true); }}, STATUS_LABEL[st], h('span', {class: 'num'}, ' ' + fmt(by[st].length)))));
  return [pick, board];
}

// ---- the list: a table, sortable by due (and by the other columns).
const SORTS = {
  title: r => r.title.toLowerCase(), status: r => STATUSES.indexOf(r.status), home: r => r.home_name ? r.home_name.toLowerCase() : null,
  due: r => r.due, review: r => r.review_after, messages: r => r.message_count,
};
function workTable(rows) {
  const val = SORTS[WORK.sort] || SORTS.due;
  const sorted = [...rows].sort((a, b) => {
    const x = val(a), y = val(b);
    if (x == null || y == null) return x == null && y == null ? a.position - b.position : x == null ? 1 : -1;  // empty last, either way
    return ((x < y ? -1 : x > y ? 1 : 0) * WORK.dir) || STATUSES.indexOf(a.status) - STATUSES.indexOf(b.status) || a.position - b.position;
  });
  const th = (key, label, cls) => h('th', {class: cls || null, 'aria-sort': WORK.sort === key ? (WORK.dir > 0 ? 'ascending' : 'descending') : null},
    h('button', {class: 'thbtn', onclick: () => { if (WORK.sort === key) WORK.dir = -WORK.dir; else { WORK.sort = key; WORK.dir = 1; } render(); }},
      label, WORK.sort === key ? h('span', {'aria-hidden': 'true'}, WORK.dir > 0 ? '▲' : '▼') : null));
  return h('section', {class: 'card', style: 'margin-top:4px'}, rows.length ? h('div', {class: 'tw'}, h('table', {class: 't wtable'},
    h('thead', null, h('tr', null, th('title', 'Work item'), th('status', 'Status'), th('home', 'Home'), th('due', 'Due'), th('review', 'Review'), th('messages', 'Messages', 'r'))),
    h('tbody', null, sorted.map(r => h('tr', {class: 'click' + (r.status === 'done' ? ' wdone' : ''), tabindex: '0', 'data-pane-key': 'w:' + r.id, onclick: () => openWork(r.id), onkeydown: e => { if (e.key === 'Enter') openWork(r.id); }},
      h('td', {class: 'nm'}, r.title, r.focus ? h('span', {class: 'pill ok', style: 'margin-left:6px'}, 'focus') : null),
      h('td', null, statusPill(r.status)),
      h('td', null, r.home_name ? [r.home_name, h('small', {class: 'muted', style: 'display:block'}, KIND_LABEL[r.home_kind] || r.home_kind)] : h('span', {class: 'muted'}, '—')),
      h('td', {class: 'num'}, r.due ? (r.overdue ? h('span', {class: 'pill hi'}, r.due) : r.due) : h('span', {class: 'muted'}, '—')),
      h('td', {class: 'num'}, r.review_after ? (r.review_due ? h('span', {class: 'pill md'}, r.review_after) : r.review_after) : h('span', {class: 'muted'}, '—')),
      h('td', {class: 'r'}, r.message_count ? fmt(r.message_count) : h('span', {class: 'muted'}, '—'))))))) :
    empty('Nothing matches', 'Clear a filter, or make a work item from a message.'));
}

async function viewWork() {
  const p = new URLSearchParams();
  if (WORK.status) p.set('status', WORK.status);
  if (WORK.home) p.set('home', WORK.home);
  if (WORK.focus) p.set('focus', '1');
  if (WORK.overdue) p.set('overdue', '1');
  if (WORK.q) p.set('q', WORK.q);
  const [rows, homes, inbox] = await Promise.all([api('/api/work?' + p), api('/api/work/homes'), api('/api/work?status=inbox')]);
  const open = rows.filter(r => r.status !== 'done').length;
  const quick = WORK.status === 'inbox' && !WORK.focus && !WORK.overdue ? 'inbox' : WORK.focus && !WORK.overdue ? 'focus' : WORK.overdue && !WORK.focus ? 'overdue' : !WORK.status && !WORK.focus && !WORK.overdue ? '' : null;
  const setQuick = v => { WORK.status = v === 'inbox' ? 'inbox' : ''; WORK.focus = v === 'focus'; WORK.overdue = v === 'overdue'; render(); };
  const status = h('select', {class: 'sel fc-sel' + (WORK.status ? ' on' : ''), 'aria-label': 'Status', onchange: e => { WORK.status = e.target.value; render(); }},
    h('option', {value: ''}, 'Any status'), STATUSES.map(st => h('option', {value: st}, STATUS_LABEL[st])));
  status.value = WORK.status;
  const home = homeSelect(homes, WORK.home, {'aria-label': 'Home', class: 'sel fc-sel' + (WORK.home ? ' on' : ''), onchange: e => { WORK.home = e.target.value; render(); }},
    [h('option', {value: ''}, 'Any binder'), h('option', {value: 'none'}, 'No home')]);
  const filtered = WORK.status || WORK.home || WORK.focus || WORK.overdue || WORK.q;
  const toggle = (on, label, fn, title) => h('button', {class: 'fc' + (on ? ' on' : ''), 'aria-pressed': String(on), title, onclick: fn}, label);
  return h('div', null,
    header(withHelp('Work', 'work'), `${fmt(open)} open${rows.length > open ? ` · ${fmt(rows.length - open)} done` : ''}${filtered ? ' in this selection' : ''} · ${fmt(inbox.length)} in the inbox`,
      h('button', {class: 'btn primary', onclick: () => newWork({home_id: WORK.home && WORK.home !== 'none' ? Number(WORK.home) : null, status: WORK.status || 'inbox'})}, 'New work item')),
    h('div', {class: 'fchips wfilters'},
      h('label', {class: 'search-l wsearch'}, icon('search'), h('input', {class: 'search', type: 'search', id: 'wq', placeholder: 'Find a work item', value: WORK.q, 'aria-label': 'Search work items',
        oninput: e => { clearTimeout(workSearchTimer); const v = e.target.value; workSearchTimer = setTimeout(() => { WORK.q = v; render(true); }, 250); }})),
      toggle(quick === 'inbox', `Inbox · ${fmt(inbox.length)}`, () => setQuick(quick === 'inbox' ? '' : 'inbox'), 'Only what is not filed yet'),
      toggle(WORK.focus, 'Focus', () => { WORK.focus = !WORK.focus; render(); }, 'Only work marked as focus'),
      toggle(WORK.overdue, 'Overdue', () => { WORK.overdue = !WORK.overdue; render(); }, 'Only work past its due date'),
      status, home,
      filtered ? h('button', {class: 'btn ghost sm', onclick: () => { Object.assign(WORK, {status: '', home: '', focus: false, overdue: false, q: ''}); render(); }}, 'Reset') : null),
    WORK.mode === 'board'
      ? [workBoard(rows, {showHome: true, fold: !WORK.status, statuses: WORK.status ? [WORK.status] : null}),
         h('p', {class: 'small muted board-hint'}, 'Drag a card, or focus it and press m (or Alt + arrow keys) to move it.')]
      : workTable(rows));
}

// ---- the work item in the pane: every field, the linked messages and the history.
const cleanSubject = subj => { let t = (subj || '').trim(), was; do { was = t; t = t.replace(/^(re|sv|vs|fw|fwd|vb|aw|wg)\s*:\s*/i, ''); } while (t !== was); return t; };
function openWork(id, saved) {
  closeMenu();
  return paneLoad({key: 'w:' + id, title: 'Work item'}, async () => {
    const [w, homes] = await Promise.all([api(`/api/work/${id}`), api('/api/work/homes')]);
    return {node: workForm(w, homes, null, saved), label: w.title};
  });
}
async function newWork(pre) {
  closeMenu();
  let homes;
  try { homes = await api('/api/work/homes'); } catch (e) { flash(errText(e)); return; }
  openPane(workForm(null, homes, pre || {}), {key: 'w:new', title: 'New work item'});
  const t = document.getElementById('w-title');
  if (t) t.focus();
}
function historyText(e, homes) {
  const v = x => x == null || x === '' ? '—' : String(x);
  const home = id => id == null ? 'no home' : (homes.find(o => o.id === id) || {name: '#' + id}).name;
  switch (e.field) {
    case 'created': return `Created in ${STATUS_LABEL[(e.new_value || {}).status] || 'Inbox'}`;
    case 'status': return `${STATUS_LABEL[e.old_value] || v(e.old_value)} → ${STATUS_LABEL[e.new_value] || v(e.new_value)}`;
    case 'home_id': return `Home: ${home(e.old_value)} → ${home(e.new_value)}`;
    case 'focus': return e.new_value ? 'Focus on' : 'Focus off';
    case 'start_on': return `Start: ${v(e.old_value)} → ${v(e.new_value)}`;
    case 'due': return `Due: ${v(e.old_value)} → ${v(e.new_value)}`;
    case 'review_after': return `Review after: ${v(e.old_value)} → ${v(e.new_value)}`;
    case 'title': return `Title: “${v(e.old_value)}” → “${v(e.new_value)}”`;
    case 'body': return 'Text edited';
    case 'position': return 'Reordered';
    case 'messages': {
      const was = new Set(e.old_value || []), now = new Set(e.new_value || []);
      const added = [...now].filter(x => !was.has(x)).length, removed = [...was].filter(x => !now.has(x)).length;
      return [added ? `${added} message${added === 1 ? '' : 's'} linked` : null, removed ? `${removed} unlinked` : null].filter(Boolean).join(', ') || 'Messages';
    }
    default: return `${e.field}: ${v(JSON.stringify(e.old_value))} → ${v(JSON.stringify(e.new_value))}`;
  }
}
// Newest first. A move writes its status and its new position together; the position is
// left out then, since "Reordered" would only repeat the move.
function historyShown(events) {
  const moves = new Set(events.filter(e => e.field === 'status').map(e => e.at));
  return events.filter(e => !(e.field === 'position' && moves.has(e.at))).reverse();
}
function workForm(w, homes, pre, saved) {
  const isNew = !w;
  const v = w || {title: '', status: 'inbox', home_id: null, focus: false, start_on: null, due: null, review_after: null, body: '', messages: [], history: [], related: [], calendar: [], ...pre};
  const title = h('input', {class: 'field wtitle', id: 'w-title', value: v.title, maxlength: '500', autocomplete: 'off'});
  const status = statusSelect(v.status, {id: 'w-status'});
  const home = homeSelect(homes, v.home_id, {id: 'w-home'}, null, v.home_name);
  const focus = h('input', {type: 'checkbox', class: 'switch', id: 'w-focus', checked: !!v.focus});
  const startOn = h('input', {class: 'field wdate', type: 'date', id: 'w-start', value: v.start_on || ''});
  const due = h('input', {class: 'field wdate', type: 'date', id: 'w-due', value: v.due || ''});
  const review = h('input', {class: 'field wdate', type: 'date', id: 'w-review', value: v.review_after || ''});
  const body = h('textarea', {class: 'field wbody', id: 'w-body', rows: '10', placeholder: 'Desired outcome, context, working notes. Markdown, kept as text.'}, v.body || '');
  const slot = {title: h('div', {class: 'ferr', id: 'w-title-err'}), status: h('div', {class: 'ferr'}), home_id: h('div', {class: 'ferr'}),
                start_on: h('div', {class: 'ferr', id: 'w-start-err'}), due: h('div', {class: 'ferr', id: 'w-due-err'}), review_after: h('div', {class: 'ferr', id: 'w-review-err'}), body: h('div', {class: 'ferr'})};
  const input = {title, status, home_id: home, start_on: startOn, due, review_after: review, body, focus};
  const err = h('div', {class: 'err', role: 'alert'});
  const clearErrors = () => { Object.values(slot).forEach(x => x.replaceChildren()); Object.values(input).forEach(x => x.removeAttribute('aria-invalid')); err.replaceChildren(); };
  const fieldOf = text => /^(review_after|review)/.test(text) ? 'review_after' : /^start_on\b|the start comes after/.test(text) ? 'start_on' : /^due\b/.test(text) ? 'due' : /status/.test(text) ? 'status'
    : /title/.test(text) ? 'title' : /home|object/.test(text) ? 'home_id' : /body|text/.test(text) ? 'body' : null;
  const showErr = (field, text) => {
    if (field && slot[field]) { slot[field].replaceChildren(text); input[field].setAttribute('aria-invalid', 'true'); input[field].focus(); }
    else err.replaceChildren(text);
  };
  // The body is Markdown kept as text; Read shows it through the safe renderer (markdown()), never as HTML.
  let bodyMode = isNew || !v.body ? 'write' : 'read';
  const bodyWrap = h('div');
  const drawBody = () => fill(bodyWrap, bodyMode === 'read'
    ? h('div', {class: 'body-text md wread', tabindex: '0', title: 'Double-click to edit', ondblclick: () => { bodyMode = 'write'; drawBody(); body.focus(); }}, body.value.trim() ? markdown(body.value) : '(no text yet)')
    : body);
  const bodySeg = () => {
    const g = seg([['read', 'Read'], ['write', 'Edit']], bodyMode, m => { bodyMode = m; drawBody(); fill(bodyTabs, bodySeg()); if (m === 'write') body.focus(); });
    g.classList.add('sm');
    return g;
  };
  const bodyTabs = h('span');
  fill(bodyTabs, bodySeg());
  drawBody();
  const saveBtn = h('button', {class: 'btn primary', onclick: () => save()}, isNew ? 'Create' : 'Save');
  const save = async () => {
    clearErrors();
    const vals = {title: title.value.trim(), status: status.value, home_id: home.value ? Number(home.value) : null, focus: focus.checked,
                  start_on: startOn.value || null, due: due.value || null, review_after: review.value || null, body: body.value};
    if (!vals.title) { showErr('title', 'A work item needs a title.'); return; }
    for (const [k, el] of [['start_on', startOn], ['due', due], ['review_after', review]]) if (el.validity && el.validity.badInput) { showErr(k, 'That is not a date.'); return; }
    let payload = {};
    if (isNew) payload = {...vals, message_ids: v.message_ids || []};
    else for (const k of Object.keys(vals)) if ((vals[k] == null ? null : vals[k]) !== (v[k] == null ? null : v[k])) payload[k] = vals[k];
    if (!isNew && !Object.keys(payload).length) { err.replaceChildren(h('span', {class: 'muted'}, 'Nothing has changed.')); return; }
    saveBtn.disabled = true;
    let res;
    try { res = await post(isNew ? '/api/work' : `/api/work/${v.id}`, payload); }
    catch (e) { saveBtn.disabled = false; const text = errText(e); showErr(fieldOf(text), text); return; }
    render();  // the page behind follows; the pane stays on the saved item
    openPane(workForm(res, homes, null, isNew ? 'Created.' : 'Saved.'), {key: 'w:' + res.id, title: 'Work item', label: res.title, replace: true});
  };
  const unlink = async (e, mid) => {
    e.stopPropagation();
    try { await post(`/api/work/${v.id}/messages`, {remove: [mid]}); } catch (x) { flash(errText(x)); return; }
    render();
    openWork(v.id, 'Message unlinked.');
  };
  const row = (label, forId, el, extra) => [h('label', {class: 'flabel', for: forId}, label), h('div', {class: 'wfield'}, el, extra || null)];
  const messages = v.messages || [];
  const prov = v.origin && Object.keys(v.origin).length ? v.origin : null;
  return h('div', {class: 'wdrawer', onkeydown: e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); save(); } }},
    h('div', {class: 'meta', style: 'margin:0 0 6px'}, isNew ? 'New work item' : ['Work item', statusPill(v.status), v.home_name ? h('button', {class: 'chipbtn', title: 'Open ' + v.home_name,
      onclick: () => go('objects', v.home_id)}, v.home_name) : null, focusChip(v), dueChip(v), reviewChip(v)]),
    h('h2', null, isNew ? (v.title || 'New work item') : v.title),
    saved ? h('div', {class: 'small', role: 'status', style: 'color:var(--good-ink)'}, saved) : null,
    h('div', {class: 'wform'},
      row('Title', 'w-title', title, slot.title),
      row('Status', 'w-status', h('span', {class: 'rrow'}, status,
        !isNew && v.status !== 'done' ? h('button', {class: 'btn sm danger', title: 'Mark it done and save, in one click',
          onclick: () => { status.value = 'done'; save(); }}, '✓ Done') : null, helpBtn('work')), slot.status),
      row('Home', 'w-home', home, slot.home_id),
      row('Focus', 'w-focus', h('span', {class: 'rrow'}, focus, h('span', {class: 'small muted'}, 'Keep it in Needs attention'))),
      row('Start', 'w-start', startOn, slot.start_on),
      row('Due', 'w-due', due, slot.due),
      row('Review after', 'w-review', review, slot.review_after)),
    h('div', {class: 'dsec'}, h('div', {class: 'rrow', style: 'justify-content:space-between;margin-bottom:8px'}, h('h4', {style: 'margin:0'}, 'Text'), bodyTabs), bodyWrap, slot.body),
    err,
    h('div', {class: 'dactions', style: 'margin-top:12px'}, saveBtn, h('button', {class: 'btn ghost', onclick: () => closePane()}, isNew ? 'Cancel' : 'Close'),
      h('span', {class: 'small muted', style: 'align-self:center'}, '⌘/Ctrl + Enter saves')),
    isNew ? ((v.messages || []).length ? h('div', {class: 'dsec'}, h('h4', null, 'Will be linked to'), v.messages.map(m => h('div', {class: 'small'}, m.subject || '(no subject)'))) : null) :
      h('div', {class: 'dsec'}, h('h4', null, messages.length ? `Messages · ${messages.length}` : 'Messages'),
        messages.length ? messages.map(m => h('div', {class: 'imp-row', tabindex: '0', role: 'button', onclick: () => openMessage(m.id), onkeydown: e => { if (e.key === 'Enter' && e.target === e.currentTarget) openMessage(m.id); }},
          h('span', {class: 'imp-l'}, acctDot(m.account_id)),
          h('span', {class: 'imp-m'}, h('b', null, m.subject || '(no subject)'), h('span', {class: 'small muted'}, (m.from_name || m.from_address || '') + (m.medium && m.medium.startsWith('teams') ? ' · Teams' : ''))),
          h('span', {class: 'imp-r small muted'}, when(m.received_at), ' ', h('button', {class: 'btn ghost sm', title: 'Unlink this message', onclick: e => unlink(e, m.id), onkeydown: e => e.stopPropagation()}, 'Unlink')))) :
          h('div', {class: 'small muted'}, 'No messages yet. In a message, use “Make a work item” or “Link to a work item”.')),
    !isNew && (v.related || []).length ? h('div', {class: 'dsec'}, h('h4', null, 'Related'),
      h('div', {class: 'chips'}, v.related.map(o => h('button', {class: 'chipbtn', onclick: () => go('objects', o.id)}, o.name, h('span', {class: 'n'}, KIND_LABEL[o.kind] || o.kind))))) : null,
    !isNew ? workCalendar(v) : null,
    !isNew ? h('div', {class: 'dsec'}, h('h4', null, 'History'),
      h('table', {class: 't hist'}, h('tbody', null, historyShown(v.history || []).map(e => h('tr', null,
        h('td', {class: 'muted', style: 'white-space:nowrap'}, new Date(e.at).toLocaleString('sv-SE', {dateStyle: 'short', timeStyle: 'short'})),
        h('td', null, historyText(e, homes)), h('td', {class: 'r muted'}, e.by)))))) : null,
    prov ? h('div', {class: 'small muted', style: 'margin-top:12px;overflow-wrap:anywhere'},
      'From ', [prov.path ? `the vault: ${prov.path}` : null, prov['routed-by'] || prov.routed_by ? `filed by ${prov['routed-by'] || prov.routed_by}` : null].filter(Boolean).join(' · ') || 'an import') : null);
}

// ---- the message pane's Work section: the items it is part of, and making or linking one.
function workSection(m, reopen) {
  const items = m.work || [];
  const box = h('div', {class: 'objadd'});
  const closed = () => fill(box, abtn('act', 'workitem', 'Make a work item', () => make(), {color: 'var(--accent)', key: MESSAGE_KEYS.work}),
    abtn('quiet', 'link', 'Link to a work item', () => link(), {key: MESSAGE_KEYS.link}));
  const done = async text => {
    if (['work', 'objects', 'overview'].includes(S.view)) render();
    await (reopen || (() => openMessage(m.id)))();
    flash(text);
  };
  const make = async () => {
    box.replaceChildren(h('span', {class: 'small muted'}, 'Loading…'));
    let homes;
    try { homes = await api('/api/work/homes'); } catch (e) { box.replaceChildren(h('span', {class: 'err'}, errText(e))); return; }
    const title = h('input', {class: 'field', id: 'mw-title', value: cleanSubject(m.subject) || '(no subject)', maxlength: '500'});
    const home = homeSelect(homes, null, {id: 'mw-home'});
    const status = statusSelect('inbox', {id: 'mw-status'});
    const msg = h('div', {class: 'err', role: 'alert'});
    const create = async () => {
      msg.replaceChildren();
      if (!title.value.trim()) { msg.replaceChildren('A work item needs a title.'); title.focus(); return; }
      try {
        await post('/api/work', {title: title.value.trim(), home_id: home.value ? Number(home.value) : null, status: status.value, message_ids: [m.id]});
      } catch (e) { msg.replaceChildren(errText(e)); return; }
      done('Work item made and linked to this message.');
    };
    fill(box, h('div', {class: 'mwform', onkeydown: e => { if (e.key === 'Enter' && e.target === title) create(); }},
      h('label', {class: 'flabel', for: 'mw-title'}, 'Title'), title,
      h('div', {class: 'rrow'}, h('label', {class: 'fsel', for: 'mw-home'}, h('span', {class: 'flabel'}, 'Home'), home),
        h('label', {class: 'fsel', for: 'mw-status'}, h('span', {class: 'flabel'}, 'Status'), status)),
      h('div', {class: 'rrow'}, h('button', {class: 'btn sm primary', onclick: create}, 'Create and link'), h('button', {class: 'btn ghost sm', onclick: closed}, 'Cancel')),
      msg));
    title.focus();
    title.select();
  };
  const link = async () => {
    box.replaceChildren(h('span', {class: 'small muted'}, 'Loading…'));
    let open;
    try { open = await api('/api/work?status=inbox,next,doing,blocked,someday'); } catch (e) { box.replaceChildren(h('span', {class: 'err'}, errText(e))); return; }
    const mine = new Set(items.map(w => w.id));
    const choices = open.filter(w => !mine.has(w.id));
    if (!choices.length) { fill(box, h('span', {class: 'small muted'}, 'No other open work item to link to.'), h('button', {class: 'btn ghost sm', onclick: closed}, 'Back')); return; }
    const sel = h('select', {class: 'sel', 'aria-label': 'Work item'}, STATUSES.filter(st => st !== 'done').map(st => {
      const l = choices.filter(w => w.status === st);
      return l.length ? h('optgroup', {label: STATUS_LABEL[st]}, l.map(w => h('option', {value: w.id}, w.title + (w.home_name ? ` · ${w.home_name}` : '')))) : null;
    }));
    const msg = h('span', {class: 'err', role: 'alert'});
    fill(box, sel, h('button', {class: 'btn sm primary', onclick: async () => {
      try { await post(`/api/work/${sel.value}/messages`, {add: [m.id]}); } catch (e) { msg.replaceChildren(errText(e)); return; }
      done('Linked.');
    }}, 'Link'), h('button', {class: 'btn ghost sm', onclick: closed}, 'Cancel'), msg);
    sel.focus();
  };
  closed();
  return agroup(withHelp(items.length ? `Work · ${items.length}` : 'Work', 'work'), [
    items.length ? h('div', {class: 'wlinks'}, items.map(w => h('button', {class: 'wlink', onclick: () => openWork(w.id)},
      statusPill(w.status), h('span', {class: 'wl-t'}, w.title), w.home_name ? h('span', {class: 'small muted'}, w.home_name) : null))) : null,
    box]);
}

// ---- the Overview's "Needs attention" rows: each says why.
function attentionRow(r) {
  return h('div', {class: 'imp-row', tabindex: '0', role: 'button', 'data-pane-key': 'w:' + r.id, onclick: () => openWork(r.id), onkeydown: e => { if (e.key === 'Enter') openWork(r.id); }},
    h('span', {class: 'imp-l att-l'}, r.reasons.map((k, i) => h('span', {class: REASON_PILL[k] || 'pill', title: REASON_TIP[k]}, r.reason_text[i]))),
    h('span', {class: 'imp-m'}, h('b', null, r.title),
      h('span', {class: 'small muted'}, [r.home_name, r.due ? `due ${r.due}` : null, r.review_due ? `review after ${r.review_after}` : null].filter(Boolean).join(' · ') || 'No home')),
    h('span', {class: 'imp-r small muted'}, r.message_count ? `✉ ${r.message_count}` : ''));
}

// ---------------------------------------------------------------- charts for Insights
// A month-by-account bar chart and the top-people card, drawn as SVG and rows (no chart library).
function barChart(months, accounts, rows) {
  const W = 760, H = 200, L = 40, R = 8, T = 10, B = 24;
  const by = {}; rows.forEach(r => { (by[r.month] = by[r.month] || {})[r.account_id] = r.n; });
  const totals = months.map(m => accounts.reduce((a, acc) => a + ((by[m] || {})[acc] || 0), 0));
  const max = Math.max(1, ...totals);
  const step = Math.pow(10, Math.floor(Math.log10(max))); const top = Math.ceil(max / step) * step;
  const bw = (W - L - R) / months.length;
  const y = v => T + (H - T - B) * (1 - v / top);
  const g = s('svg', {viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Messages per month'});
  for (let k = 0; k <= 4; k++) { const v = top * k / 4; g.append(s('line', {x1: L, x2: W - R, y1: y(v), y2: y(v), style: 'stroke:var(--grid)'}), s('text', {x: L - 6, y: y(v) + 3.5, 'text-anchor': 'end'}, fmt(v))); }
  months.forEach((m, i) => {
    let base = 0;
    accounts.forEach((acc, ai) => {
      const v = (by[m] || {})[acc] || 0; if (!v) return;
      const r = s('rect', {x: L + i * bw + 1, width: Math.max(1, bw - 2), y: y(base + v), height: y(base) - y(base + v), style: `fill:${acctColor(acc)}`}, s('title', null, `${m} · ${acc}: ${fmt(v)}`));
      g.append(r); base += v;
    });
    if (i % 3 === 0 || i === months.length - 1) g.append(s('text', {x: L + i * bw + bw / 2, y: H - 6, 'text-anchor': 'middle'}, m));
  });
  return h('div', {class: 'chart'}, g);
}

// "Top people": individual senders, the owner's own addresses and automated mail left out. The window
// is remembered while the page is open.
let topDays = '';
function topPeopleCard(people) {
  const range = seg([['', 'All time'], ['365', 'Year'], ['90', '90 days']], topDays, v => { topDays = v; render(); });
  range.classList.add('sm');
  if (!people) return card('Top people', 'Could not load the senders', range);
  const max = Math.max(1, ...people.map(p => p.n));
  return card('Top people', 'Who writes to you most, without automated mail — click to list', range,
    people.length ? h('div', {class: 'hbars'}, people.map(p => h('button', {class: 'hbar', title: `${p.address} · ${fmt(p.n)} messages`,
        onclick: () => { S.f = {...blank(), sender: p.address}; openSenders(); go('messages'); }},
      h('span', {class: 'lab'}, p.name || p.address), h('span', {class: 'track'}, h('span', {class: 'fill', style: `display:block;width:${100 * p.n / max}%;background:var(--accent)`})), h('span', {class: 'val'}, fmt(p.n))))) :
      empty('Nobody yet', topDays ? 'No person wrote to you in this window.' : 'No mail from people has been synced yet.'));
}

// ---------------------------------------------------------------- Discover › Insights (the old Overview)
// Insights is a set of widgets the owner switches on and off (Customize). The choice is kept in this
// browser only. The widgets Today uses (attention, waiting, today, argus) stay in WIDGET_DATA and
// WIDGET_VIEW, so both pages draw them the same way.
// Needs attention, Waiting for your answer, Today's most important and Argus moved to Today; Low-hanging
// fruit and Improvement jobs to Tune › Jobs & fruit (2026-09-28). What stays is the look across the archive.
const WIDGETS = [
  {id: 'check', title: 'Check this', on: true},
  {id: 'projects', title: 'Projects', on: true},
  {id: 'people', title: 'Top people', on: true},
  {id: 'domains', title: 'Top domains', on: true},
  {id: 'events', title: 'Events', on: true},
  {id: 'archive', title: 'The archive', on: false, wide: true},
  {id: 'accounts', title: 'Accounts', on: false},
  {id: 'labels', title: 'Labels and categories', on: false},
  {id: 'attachments', title: 'Attachments by type', on: false},
];
// A saved choice may still name a widget that is gone ('daily', Important conversations per day,
// removed 2026-09-26): only the widgets that exist are read from it, so it is simply left out.
function widgetChoice() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem('talos-widgets') || 'null'); } catch (e) {}
  if (!saved || typeof saved !== 'object' || Array.isArray(saved)) saved = null;
  return Object.fromEntries(WIDGETS.map(w => [w.id, saved && w.id in saved ? !!saved[w.id] : w.on]));
}
function saveWidgetChoice(choice) {
  try { localStorage.setItem('talos-widgets', JSON.stringify(choice)); } catch (e) {}
}
let customizing = false;

// Importance is a signal, not an alarm: red stays for overdue, down and failed (docs/design.md).
const levelPill = lv => h('span', {class: 'pill ' + (lv === 'high' ? 'ac' : ''), title: `Importance: ${lv}`}, lv);
function importantRow(m, right) {
  return h('div', {class: 'imp-row acct-tint', tabindex: '0', role: 'button', style: `--acct:${acctColor(m.account_id)}`, 'data-pane-key': 'm:' + m.id,
      onclick: () => openMessage(m.id), onkeydown: e => { if (e.key === 'Enter') openMessage(m.id); }},
    h('span', {class: 'imp-l'}, acctDot(m.account_id), levelPill(m.level)),
    h('span', {class: 'imp-m'}, h('b', null, m.subject || '(no subject)'),
      h('span', {class: 'small muted'}, (m.from_name || m.from_address || '') + (m.medium && m.medium.startsWith('teams') ? ' · Teams' : ''))),
    h('span', {class: 'imp-r small muted'}, right));
}
const ago = iso => { const d = (Date.now() - new Date(iso)) / 3600e3; return d < 48 ? `${Math.max(1, Math.round(d))} h` : `${Math.round(d / 24)} d`; };

const WIDGET_DATA = {
  attention: () => api('/api/work/attention?limit=100'),
  today: () => api('/api/importance/today?limit=5&hours=24'),
  waiting: () => api('/api/importance/waiting?limit=6'),
  check: () => api('/api/importance/check?limit=5'),
  projects: () => api('/api/objects'),
  people: () => { const q = new URLSearchParams({people_only: 1, limit: 12}); if (topDays) q.set('days', topDays); return api('/api/top-senders?' + q); },
};
const ATTENTION_SHOWN = 8;
const WIDGET_VIEW = {
  attention: rows => card(withHelp('Needs attention', 'needs-you'), 'Open work in progress, blocked, overdue, due for review, in focus or in the inbox', h('button', {class: 'btn sm', onclick: () => go('work')}, 'Work ›'),
    rows.length ? [rows.slice(0, ATTENTION_SHOWN).map(attentionRow),
      rows.length > ATTENTION_SHOWN ? h('button', {class: 'linkbtn', onclick: () => go('work')}, `${fmt(rows.length - ATTENTION_SHOWN)} more in Work ›`) : null] :
      empty('Nothing needs you', 'No work item is in progress, blocked, overdue, due for review, in focus or in the inbox.')),
  today: rows => card(withHelp("Today's most important", 'importance'), 'The last 24 hours, one per conversation', null,
    rows.length ? [rows.map(m => importantRow(m, when(m.received_at))), rowsLegend(rows)] : empty('Nothing important yet today', 'Messages that need you appear here as they arrive.')),
  waiting: rows => card(withHelp('Waiting for your answer', 'waiting'), 'Questions to you from people you know, oldest first', null,
    rows.length ? [rows.map(m => importantRow(m, `${Math.round(m.waited_days)} d`)), rowsLegend(rows)] : empty('Nothing is waiting', 'No unanswered questions from people you know in the last 30 days.')),
  check: rows => card('Check this', 'Things that look like they need a look', null,
    rows.length ? [rows.map(m => h('div', {class: 'imp-wrap acct-tint', style: `--acct:${acctColor(m.account_id)}`}, importantRow(m, ago(m.received_at)), h('div', {class: 'imp-reason small muted'}, m.reason))), rowsLegend(rows)] : empty('Nothing to check', 'No stalled important threads right now.')),
  projects: rows => {
    const projects = rows.filter(o => ['project', 'case'].includes(o.kind));
    return card('Projects', 'Your projects and cases', h('button', {class: 'btn sm', onclick: () => go('objects')}, 'Objects ›'),
      projects.length ? h('table', {class: 't'}, h('tbody', null, projects.map(o => h('tr', {class: 'click', onclick: () => go('objects', o.id)},
        h('td', {class: 'nm'}, o.name, h('small', null, o.kind)), h('td', {class: 'r'}, `${fmt(o.members)} members`))))) :
        empty('No projects yet', 'Create one under Objects; it shows up here.'));
  },
  people: rows => topPeopleCard(rows),
};

async function viewOverview() {
  const choice = widgetChoice();
  const want = WIDGETS.filter(w => choice[w.id]);
  const needO = want.some(w => ['domains', 'events', 'archive', 'accounts', 'labels', 'attachments'].includes(w.id));
  const loads = await Promise.all([needO || !want.length ? api('/api/overview') : Promise.resolve(null),
    ...want.map(w => WIDGET_DATA[w.id] ? WIDGET_DATA[w.id]().catch(e => ({__error: String(e.message || e)})) : Promise.resolve(null))]);
  const o = loads[0], data = Object.fromEntries(want.map((w, i) => [w.id, loads[i + 1]]));
  if (o) pollWhileRefreshing(o, '/api/overview');
  if (o && !o.messages) {
    return h('div', null, header('Insights', 'Nothing synced yet'),
      h('div', {class: 'hero-empty'}, heroMark('lg'),
        h('p', {class: 'lead'}, 'The archive is empty. Once the Gmail app password and the Microsoft sign-in are in place, the first sync fills it.')));
  }
  const view = {...WIDGET_VIEW,
    domains: () => { const maxDom = Math.max(1, ...o.top_domains.map(d => d.n));
      return card('Top domains', 'Incoming mail by sender domain — click for its most active senders', null,
        h('div', {class: 'hbars'}, o.top_domains.map(d => h('button', {class: 'hbar', title: `${d.domain}: ${fmt(d.n)} messages — show its senders`, onclick: () => { S.f = {...blank(), domain: d.domain}; openSenders(); go('messages'); }},
          h('span', {class: 'lab'}, d.domain || '(none)'), h('span', {class: 'track'}, h('span', {class: 'fill', style: `display:block;width:${100 * d.n / maxDom}%;background:${d.automated ? 'var(--series-other)' : 'var(--accent)'}`})), h('span', {class: 'val'}, fmt(d.n))))),
        peopleLegend()); },
    events: () => { const byKind = {}; o.event_breakdown.forEach(e => { (byKind[e.kind] = byKind[e.kind] || {})[e.status] = e.n; });
      return card('Events', 'Machine mail, read as status', h('button', {class: 'btn sm', onclick: () => go('events')}, 'Events ›'),
        Object.keys(byKind).length ? h('table', {class: 't'}, h('tbody', null, Object.entries(byKind).map(([k, st]) => h('tr', null, h('td', {class: 'nm'}, k),
          h('td', {class: 'r'}, ['failed', 'warning', 'ok', 'info'].filter(x => st[x]).map(x => h('span', {class: 'pill ' + (x === 'failed' ? 'hi' : x === 'warning' ? 'md' : x === 'ok' ? 'ok' : ''), style: 'margin-left:4px'}, `${x} ${fmt(st[x])}`))))))) :
          empty('No events yet', 'Run  talos events run  after a sync.')); },
    archive: () => { const accounts = o.accounts.filter(a => a.messages > 0).map(a => a.id); const months = [...new Set(o.monthly.map(r => r.month))].sort();
      const autoShare = o.messages ? Math.round(100 * o.automated / o.messages) : 0;
      return card('The archive', 'Everything ingested, per month and account', null,
        h('div', {class: 'kpis', style: 'border:0;margin:0 0 14px;padding:0'},
          h('div', {class: 'kpi'}, h('div', {class: 'l'}, 'Messages'), h('div', {class: 'v'}, fmt(o.messages)), h('div', {class: 'n'}, `${fmt(o.threads)} threads`)),
          h('div', {class: 'kpi'}, h('div', {class: 'l'}, 'Sent by you'), h('div', {class: 'v'}, fmt(o.sent)), h('div', {class: 'n'}, `${Math.round(100 * o.sent / o.messages)}% of all`)),
          h('div', {class: 'kpi'}, h('div', {class: 'l'}, 'Automated'), h('div', {class: 'v'}, autoShare + '%'), h('div', {class: 'n'}, `${fmt(o.events)} events extracted`)),
          h('div', {class: 'kpi'}, h('div', {class: 'l'}, 'Vault on disk'), h('div', {class: 'v'}, bytes(o.vault_bytes)), h('div', {class: 'n'}, `${fmt(o.orgs)} organisations`))),
        months.length ? barChart(months, accounts, o.monthly) : null, acctLegend(accounts)); },
    accounts: () => card('Accounts', 'Last successful sync', h('div', {class: 'rrow'}, syncButton('btn sm'), h('button', {class: 'btn sm', onclick: () => go('sources')}, 'Sources ›')),
      h('table', {class: 't'}, h('tbody', null, o.accounts.filter(a => a.enabled || a.messages).map(a => h('tr', {class: 'click', onclick: () => { S.f = {...blank(), exclude_accounts: o.accounts.map(x => x.id).filter(x => x !== a.id)}; go('messages'); }},
        h('td', {class: 'nm'}, acctDot(a.id), ' ', a.display_name || a.id, h('small', null, a.provider)), h('td', {class: 'r'}, fmt(a.messages)),
        h('td', {class: 'r muted'}, a.last_sync ? when(a.last_sync) : 'never')))))),
    labels: () => card('Labels and categories', 'As the servers have them — click to list', null,
      o.labels.length ? h('div', {class: 'chips'}, o.labels.map(l => h('button', {class: 'chipbtn', onclick: () => { S.f = {...blank(), label: l.label}; go('messages'); }}, l.label, h('span', {class: 'n'}, fmt(l.n))))) : empty('No labels', 'None of the synced mail carries a label or category.')),
    attachments: () => card('Attachments by type', null, null, h('table', {class: 't'}, h('tbody', null, o.attachment_types.map(t => h('tr', null, h('td', null, t.content_type), h('td', {class: 'r'}, fmt(t.n)), h('td', {class: 'r muted'}, bytes(t.bytes))))))),
  };
  const today = data.today && !data.today.__error ? data.today.length : null;
  const waitingN = data.waiting && !data.waiting.__error ? data.waiting.length : null;
  const attentionN = data.attention && !data.attention.__error ? data.attention.length : null;
  const lead = [attentionN ? `${attentionN} work item${attentionN === 1 ? ' needs' : 's need'} attention` : null,
                today != null ? `${today} important conversation${today === 1 ? '' : 's'} in the last 24 hours` : null,
                waitingN != null ? `${waitingN}${waitingN >= 6 ? '+' : ''} question${waitingN === 1 ? '' : 's'} waiting for your answer` : null].filter(Boolean).join('; ');
  const panel = customizing ? h('section', {class: 'card customize'}, h('div', {class: 'card-h'}, h('div', null, h('h2', null, 'Widgets'), h('p', null, 'Choose what Insights shows. The choice is kept in this browser.')),
      h('button', {class: 'btn sm primary', onclick: () => { customizing = false; render(); }}, 'Done')),
    h('div', {class: 'widget-choices'}, WIDGETS.map(w => h('label', {class: 'widget-choice'},
      h('input', {type: 'checkbox', checked: choice[w.id], onchange: e => { choice[w.id] = e.target.checked; saveWidgetChoice(choice); render(); }}), ' ', w.title)))) : null;
  return h('div', null,
    header(withHelp('Insights', 'insights'), lead || 'The archive at a glance: people, domains, events and labels', h('button', {class: 'btn sm', 'aria-pressed': String(customizing), onclick: () => { customizing = !customizing; render(); }}, 'Customize')),
    panel,
    want.length ? h('div', {class: 'dash'}, want.map(w => {
      const d = data[w.id];
      const body = d && d.__error ? card(w.title, null, null, h('div', {class: 'err'}, d.__error)) : view[w.id](d);
      return h('div', {class: 'dash-cell' + (w.wide ? ' wide' : '')}, body);
    })) : emptyPage('No widgets chosen', 'Press Customize to choose what Insights shows.')
  );
}

// ---------------------------------------------------------------- Today
// Where the day starts (docs/design.md): what needs the owner, in one list (overdue, blocked, due for
// review, in focus, doing, the inbox, and watchers with news), the next meetings, who is waiting for
// their answer, today's most important mail, the services' health in one line, and the areas.
const TODAY_GROUPS = [['overdue', 'Overdue'], ['blocked', 'Blocked'], ['review', 'Review due'], ['focus', 'In focus'], ['doing', 'Doing'], ['inbox', 'In the inbox']];
const TODAY_SHOWN = {inbox: 3, doing: 4, focus: 4};
const shortDay = iso => iso ? new Date(iso + (iso.length === 10 ? 'T12:00:00' : '')).toLocaleDateString('sv-SE', {day: 'numeric', month: 'short'}) : '';
const hhmmOf = iso => new Date(iso).toLocaleTimeString('sv-SE', {hour: '2-digit', minute: '2-digit'});
const kindDot = k => h('span', {class: 'kdot ' + (k ? kindCls(k) : 'kind-none'), 'aria-hidden': 'true'});
// The colour of each reason, as the dot and as a faint tint over the row.
const NEED_COLOR = {overdue: 'var(--critical)', blocked: 'var(--warning)', review: 'var(--accent)', focus: 'var(--accent)', doing: 'var(--accent)', inbox: 'var(--series-other)'};
function todayWorkRow(r, g) {
  const right = g === 'overdue' && r.due ? h('span', {class: 'late'}, 'due ' + shortDay(r.due))
    : g === 'blocked' ? h('span', null, 'blocked')
    : g === 'review' ? h('span', null, 'review ' + shortDay(r.review_after))
    : r.due ? h('span', null, 'due ' + shortDay(r.due)) : r.message_count ? h('span', null, `✉ ${r.message_count}`) : null;
  return h('div', {class: 'nrow', style: `--sc:${NEED_COLOR[g] || 'transparent'}`, tabindex: '0', role: 'button', 'data-pane-key': 'w:' + r.id, onclick: () => openWork(r.id), onkeydown: e => { if (e.key === 'Enter') openWork(r.id); }},
    h('span', {class: 'sdot sd-' + g, 'aria-hidden': 'true'}),
    h('span', {class: 'nrow-m'}, h('b', null, r.title), h('span', {class: 'nrow-s'}, r.home_name ? [kindDot(r.home_kind), r.home_name] : 'No home')),
    h('span', {class: 'nrow-r'}, right));
}
function todayWatcherRow(w) {
  const open = () => { post(`/api/watchers/${w.id}`, {seen: true}).catch(() => {}); openQuery(w.query); };
  return h('div', {class: 'nrow', style: '--sc:var(--accent)', tabindex: '0', role: 'button', onclick: open, onkeydown: e => { if (e.key === 'Enter') open(); }},
    h('span', {class: 'sdot sd-watch', 'aria-hidden': 'true'}),
    h('span', {class: 'nrow-m'}, h('b', null, w.name), h('span', {class: 'nrow-s'}, w.object_name ? [kindDot(w.object_kind), w.object_name] : queryWords(w.query))),
    h('span', {class: 'nrow-r'}, h('span', {class: 'pill ac'}, `${fmt(w.fresh)} new`)));
}
function needsYouCard(att, watchers) {
  const groups = TODAY_GROUPS.map(([g, label]) => [g, label, att.filter(r => (TODAY_GROUPS.find(([x]) => r.reasons.includes(x)) || [])[0] === g)]).filter(([, , l]) => l.length);
  const news = watchers.filter(w => w.fresh && !w.paused);
  const n = att.filter(r => !r.reasons.every(x => x === 'inbox')).length + news.length;
  return h('section', {class: 'card needs'},
    h('div', {class: 'card-h'}, h('div', null, h('h2', null, withHelp('Needs you', 'needs-you')), h('p', null, 'Overdue, blocked, due for review, in focus, and watchers with news')),
      h('button', {class: 'linkbtn', onclick: () => go('work')}, 'Open in Work')),
    groups.length || news.length ? [
      groups.map(([g, label, rows]) => {
        const shown = TODAY_SHOWN[g] ? rows.slice(0, TODAY_SHOWN[g]) : rows;
        return h('div', {class: 'ngroup'}, h('div', {class: 'ngroup-h'}, label, h('span', {class: 'n'}, fmt(rows.length))),
          shown.map(r => todayWorkRow(r, g)),
          rows.length > shown.length ? h('button', {class: 'linkbtn nmore', onclick: () => { if (g === 'inbox') { Object.assign(WORK, {status: 'inbox', focus: false, overdue: false}); } go('work'); }}, `${fmt(rows.length - shown.length)} more in Work`) : null);
      }),
      news.length ? h('div', {class: 'ngroup'}, h('div', {class: 'ngroup-h'}, 'Watchers with news', h('span', {class: 'n'}, fmt(news.length))), news.map(todayWatcherRow)) : null]
    : empty('Nothing needs you', n ? '' : 'No work is overdue, blocked or due for review, and no watcher has news.'));
}
function nextMeetings(cal) {
  if (!cal) return null;
  const vis = Object.fromEntries(cal.calendars.map(c => [c.id, c]));
  const now = Date.now();
  const timed = cal.entries.filter(e => !e.all_day && !e.cancelled && (vis[e.calendar_id] || {}).visible && new Date(e.end) > now);
  const allDay = cal.entries.filter(e => e.all_day && !e.cancelled && (vis[e.calendar_id] || {}).visible);
  return card('Next on the calendar', allDay.length ? `${fmt(allDay.length)} all-day ${allDay.length === 1 ? 'entry' : 'entries'} today` : null,
    h('button', {class: 'linkbtn', onclick: () => go('calendar')}, 'Calendar'),
    timed.length ? h('div', {class: 'evlist'}, timed.slice(0, 4).map(e => h('button', {class: 'evrow', style: `--cc:${calColor((vis[e.calendar_id] || {}).color)}`, onclick: () => go('calendar')},
      h('span', {class: 'ev-t num'}, hhmmOf(e.start)), h('span', {class: 'ev-bar', 'aria-hidden': 'true'}),
      h('span', {class: 'ev-m'}, h('b', null, e.title || '(no title)'), h('span', {class: 'small muted'}, `${hhmmOf(e.start)}–${hhmmOf(e.end)}${e.location ? ' · ' + e.location : ''}`)))))
      : h('div', {class: 'small muted'}, 'Nothing more today.'));
}
function servicesLine(st) {
  if (!st || !st.services) return null;
  const bad = st.services.filter(v => ['late', 'down', 'failing'].includes(v.status));
  const up = st.services.filter(v => v.status === 'up').length;
  return h('button', {class: 'health' + (bad.length ? ' bad' : ''), title: 'Argus, the service monitor', onclick: () => go('argus')},
    h('span', {class: 'sdot ' + (bad.length ? 'sd-overdue' : 'sd-good'), 'aria-hidden': 'true'}),
    bad.length ? `${bad.map(v => v.name).join(', ')} ${bad.length === 1 ? 'needs' : 'need'} a look` : `All ${up} services up`);
}
function areasStrip(space) {
  if (!space || !space.areas || !space.areas.length) return null;
  return h('section', {class: 'areas'},
    h('div', {class: 'card-h'}, h('div', null, h('h2', null, 'Areas'), h('p', null, 'Open work and this month’s mail')), h('button', {class: 'linkbtn', onclick: () => go('space')}, 'All binders')),
    h('div', {class: 'area-strip'}, space.areas.map(a => h('button', {class: 'area-tile kind-area', onclick: () => go('objects', a.id)},
      h('b', null, a.name),
      h('span', {class: 'small'}, h('span', null, `${fmt(a.open || 0)} open`), a.overdue ? h('span', {class: 'late'}, `${fmt(a.overdue)} overdue`) : a.blocked ? h('span', null, `${fmt(a.blocked)} blocked`) : null),
      h('span', {class: 'small muted'}, `${fmt(a.recent || 0)} mails this month`)))));
}
async function viewToday() {
  const day = todayISO();
  const [att, space, waiting, important, cal, argus] = await Promise.all([
    api('/api/work/attention?limit=100').catch(() => []), api('/api/space').catch(() => null),
    api('/api/importance/waiting?limit=6').catch(() => []), api('/api/importance/today?limit=5&hours=24').catch(() => []),
    api(`/api/calendar?start=${day}&end=${day}`).catch(() => null), api('/argus/status').catch(() => null)]);
  if (space) pollWhileRefreshing(space, '/api/space');
  const d = new Date();
  const needs = needsCount(att, space && space.watchers);
  const beaconChanged = setBeacon(beaconLevel(att, important, argus));
  if (RAIL_BADGE.today !== needs || beaconChanged) { RAIL_BADGE.today = needs; renderRail(); }
  const title = d.toLocaleDateString('en-GB', {weekday: 'long', day: 'numeric', month: 'long'});
  return h('div', {class: 'today'},
    header('Today', `${title} · week ${isoWeek(d)}${needs ? ` · ${fmt(needs)} ${needs === 1 ? 'thing needs' : 'things need'} you` : ''}`,
      h('div', {class: 'rrow'}, servicesLine(argus), h('button', {class: 'btn primary', onclick: () => newWork({status: 'inbox'})}, 'New work item'))),
    h('div', {class: 'today-grid'},
      needsYouCard(att, (space && space.watchers) || []),
      h('div', {class: 'vstack'},
        nextMeetings(cal),
        card(withHelp('Waiting for your answer', 'waiting'), 'Questions to you from people you know, oldest first', null,
          waiting.length ? waiting.map(m => importantRow(m, `${Math.round(m.waited_days)} d`)) : empty('Nothing is waiting', 'No unanswered questions from people you know in the last 30 days.')),
        card(withHelp('Most important today', 'importance'), 'The last 24 hours, one per conversation', null,
          important.length ? important.map(m => importantRow(m, when(m.received_at))) : empty('Nothing important yet', 'Messages that need you appear here as they arrive.')))),
    areasStrip(space));
}

// blank(): no filter at all, for drill-downs (a thread, a person, a label) that must show everything.
// defaults(): what Messages opens with and Reset returns to: received mail in every account but
// DEFAULT_EXCLUDED_ACCOUNTS, since that is what the owner reads. The list filters (exclude_…) are arrays,
// never changed in place but replaced, so a copy of S.f never shares a changing list.
function blank() {
  return {q: '', account: '', exclude_accounts: [], direction: '', teams_direction: '', automated: '', attachments: '', label: '', domain: '', sender: '',
          exclude_senders: [], exclude_domains: [], dims: [], type: '', kind: '', topic: '', origin: '', sphere: '', keep: '', sure: '',
          medium: '', chat: '', team: '', channel: ''};
}
// Mail opens on received e-mail; Teams on every chat and channel, both directions (a chat is both sides).
function defaults() { return {...blank(), exclude_accounts: [...DEFAULT_EXCLUDED_ACCOUNTS], direction: 'in', medium: 'email'}; }
function teamsDefaults() { return {...blank(), medium: 'teams'}; }
let listOffset = 0, listRows = [], listTotal = 0, searchTimer = null;

// The filters as a query string. Kind, type, topic, origin, work/personal (sphere) and worth keeping
// (keep) are dimension filters: dim=type:newsletter.
// omit leaves some out, as the sender sidebar does with its own filter.
const DIM_FILTERS = ['kind', 'type', 'topic', 'origin', 'sphere', 'keep'];
function filterParams(f, omit) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(f)) {
    if (!v || (omit || []).includes(k)) continue;
    if (Array.isArray(v)) { v.forEach(x => p.append(k === 'dims' ? 'dim' : k, x)); continue; }
    if (DIM_FILTERS.includes(k)) p.append('dim', `${k}:${v}`);
    else p.set(k, v);
  }
  return p;
}

// The list is messages or conversations (listMode()); both take the same filters and page the same way.
let listKind = 'messages';
async function loadMessages(append) {
  const p = filterParams(S.f);
  if (!append) listKind = listMode();
  p.set('offset', append ? listOffset : 0);
  p.set('limit', 100);
  const res = await api((listKind === 'threads' ? '/api/threads?' : '/api/messages?') + p);
  listRows = append ? listRows.concat(res.rows) : res.rows;
  listTotal = res.total;
  listOffset = listRows.length;
}

// ---- Messages ⇄ Conversations. The choice is remembered in this browser, apart for mail and for
// Teams on its own: mail opens as messages, Teams alone (only its chip on) as conversations, until
// the owner picks otherwise for either.
const LIST_MODE_DEFAULT = {mail: 'messages', teams: 'threads'};
let LIST_MODE = {};
try { const x = JSON.parse(localStorage.getItem('talos-list-mode') || '{}'); if (x && typeof x === 'object') LIST_MODE = x; } catch (e) {}
const listScope = () => fScope();
function listMode() {
  const v = LIST_MODE[listScope()];
  return v === 'messages' || v === 'threads' ? v : LIST_MODE_DEFAULT[listScope()];
}
function setListMode(v) {
  LIST_MODE = {...LIST_MODE, [listScope()]: v};
  try { localStorage.setItem('talos-list-mode', JSON.stringify(LIST_MODE)); } catch (e) {}
  render();
}

// The sender sidebar: open or closed, and senders or domains, are remembered per browser; a
// domain click opens it.
const SIDE = {all: false, open: true, mode: 'senders'};
const SENDERS_TOP = 8;  // the senders shown before "Show all", so the filter panels under them are in view
try { if (localStorage.getItem('talos-senders') === '0') SIDE.open = false; } catch (e) {}
try { if (localStorage.getItem('talos-senders-mode') === 'domains') SIDE.mode = 'domains'; } catch (e) {}
function openSenders(open = true) {
  SIDE.open = open;
  if (innerWidth < 820) SIDE.phoneAsked = true;
  if (open) SIDE.all = false;
  try { localStorage.setItem('talos-senders', open ? '1' : '0'); } catch (e) {}
}
function sideMode(mode) {
  SIDE.mode = mode; SIDE.all = false;
  try { localStorage.setItem('talos-senders-mode', mode); } catch (e) {}
}
// A server error ("400 {"error": "…"}") as the sentence the server wrote.
function errText(e) {
  const m = /^\d+ ([\s\S]*)$/.exec(String((e && e.message) || e));
  if (m) { try { const j = JSON.parse(m[1]); if (j && j.error) return j.error; } catch (x) {} }
  return String((e && e.message) || e);
}
let msgToken = 0;
const LIST_SEEN = {sel: null, ids: null};

// ---- filter panels in the sidebar: the filters with long lists (kind, origin, type, topic, label),
// under the senders, each value with its count in the current selection and a bar, most first.
// A click narrows the list to the value; a second click widens it again. Each panel folds away,
// and shows its top values until "Show all"; both are remembered in this browser. The counts of a
// panel ignore its own choice (as the dropdowns' did), so another value can be picked.
const FACET_PANELS = [['kind', 'Kind'], ['origin', 'Origin'], ['type', 'Type'], ['topic', 'Topic'], ['label', 'Label']];
const FACET_TOP = 6;
const PANELS = {closed: new Set(['type', 'label']), all: new Set()};
try { const v = JSON.parse(localStorage.getItem('talos-facet-panels') || 'null'); if (v) { PANELS.closed = new Set(v.closed || []); PANELS.all = new Set(v.all || []); } } catch (e) {}
const savePanels = () => { try { localStorage.setItem('talos-facet-panels', JSON.stringify({closed: [...PANELS.closed], all: [...PANELS.all]})); } catch (e) {} };
function facetPanel(key, title, rows) {
  const cur = S.f[key];
  const open = !PANELS.closed.has(key) || !!cur;  // a panel with a choice in it stays open
  const toggle = h('button', {class: 'fp-h', 'aria-expanded': String(open), onclick: () => { PANELS.closed.has(key) ? PANELS.closed.delete(key) : PANELS.closed.add(key); savePanels(); render(true); }},
    h('span', {class: 'fp-caret', 'aria-hidden': 'true'}, open ? '▾' : '▸'), h('b', null, title),
    rows ? h('span', {class: 'small muted'}, `${fmt(rows.length)} ${rows.length === 1 ? 'value' : 'values'}`) : null,
    cur ? h('span', {class: 'pill ac'}, (rows || []).find(r => r.value === cur)?.label || cur) : null);
  if (!open) return h('div', {class: 'fpanel'}, toggle);
  if (!rows) return h('div', {class: 'fpanel'}, toggle, h('div', {class: 'small muted', style: 'padding:4px 2px'}, 'Counting…'));
  const sorted = [...rows].sort((a, b) => b.n - a.n);
  const all = PANELS.all.has(key);
  const shown = all ? sorted : sorted.slice(0, FACET_TOP);
  if (cur && !shown.some(r => r.value === cur)) shown.unshift(sorted.find(r => r.value === cur) || {value: cur, n: 0});
  const max = Math.max(1, ...sorted.map(r => r.n));
  const pick = v => { S.f = {...S.f, [key]: S.f[key] === v ? '' : v}; render(true); };
  return h('div', {class: 'fpanel'}, toggle,
    sorted.length ? h('div', {class: 'srows'}, shown.map(r => h('button', {class: 'srow', 'aria-pressed': String(cur === r.value),
        title: `${r.label || r.value}${r.family ? ' · ' + r.family : ''} · ${fmt(r.n)} messages${cur === r.value ? ' · click again to show all' : ''}`, onclick: () => pick(r.value)},
      h('span', {class: 'who'}, h('span', {class: 'nm'}, r.label || r.value), r.family ? h('span', {class: 'ad'}, r.family) : null),
      h('span', {class: 'n'}, fmt(r.n)),
      h('span', {class: 'bar'}, h('span', {style: `width:${100 * r.n / max}%;background:var(--accent)`}))))) :
      h('div', {class: 'small muted', style: 'padding:4px 2px'}, 'None in this selection.'),
    sorted.length > FACET_TOP ? h('button', {class: 'linkbtn', onclick: () => { all ? PANELS.all.delete(key) : PANELS.all.add(key); savePanels(); render(true); }},
      all ? '▴ Show the top ' + FACET_TOP : `Show all ${fmt(sorted.length)}`) : null);
}
// ---- hidden senders and domains: a real filter (exclude_senders, exclude_domains), so they leave
// the list and every count. The names seen when hiding are kept for the chips.
const HIDDEN_NAMES = new Map();
const hiddenName = a => HIDDEN_NAMES.get(a) || a;
function hideSender(r) {
  HIDDEN_NAMES.set(r.address, r.name || r.address);
  S.f = {...S.f, exclude_senders: [...S.f.exclude_senders.filter(x => x !== r.address), r.address], sender: S.f.sender === r.address ? '' : S.f.sender};
  render(true);
}
function hideDomain(d) {
  S.f = {...S.f, exclude_domains: [...S.f.exclude_domains.filter(x => x !== d), d], domain: S.f.domain === d ? '' : S.f.domain};
  render(true);
}
const unhide = (key, v) => { S.f = {...S.f, [key]: S.f[key].filter(x => x !== v)}; render(true); };
// One chip per hidden sender or domain; its × lets it back in.
function hiddenChips(prefix) {
  const chip = (key, v, label) => h('span', {class: 'chip hid'}, (prefix || '') + label,
    h('button', {'aria-label': `Show ${label} again`, title: 'Show again', onclick: () => unhide(key, v)}, '×'));
  return [...S.f.exclude_senders.map(a => chip('exclude_senders', a, hiddenName(a))),
          ...S.f.exclude_domains.map(d => chip('exclude_domains', d, d))];
}
const EYE_OFF = 'M3 3l18 18M10.6 10.6a2 2 0 0 0 2.8 2.8M9.9 5.1A9.8 9.8 0 0 1 12 5c5 0 9 5 10 7a13.2 13.2 0 0 1-2.2 3M6.6 6.6C4.4 8 2.9 10.1 2 12c1 2 5 7 10 7a9.7 9.7 0 0 0 5.4-1.6';
const hideBtn = (label, on) => h('button', {class: 'shide', 'aria-label': `Hide ${label}`, title: `Hide ${label} from the list and the counts`, onclick: on},
  s('svg', {class: 'ico', viewBox: '0 0 24 24', 'aria-hidden': 'true'}, s('path', {d: EYE_OFF})));

// The sidebar: the top senders or domains of the selection (its own filter left out), each with
// a click that narrows the list to it and a control that hides it.
function senderPanel(res) {
  const domains = SIDE.mode === 'domains';
  const where = !domains && S.f.domain ? `at ${S.f.domain}` : 'in this selection';
  const close = h('button', {class: 'btn ghost sm', title: 'Close the sidebar', 'aria-label': 'Close the sidebar', onclick: () => { openSenders(false); render(true); }}, '×');
  const mode = seg([['senders', 'Senders'], ['domains', 'Domains']], SIDE.mode, v => { sideMode(v); render(true); });
  mode.classList.add('sm');
  const count = res ? (domains ? `${fmt(res.domains)} domains ${where} · ${fmt(res.total)} messages` : `${fmt(res.senders)} ${where} · ${fmt(res.total)} messages`) : 'Counting…';
  const hidden = hiddenChips();
  // With every sender listed the way back to the top few is in the header, which stays in view.
  const fewer = SIDE.all ? h('button', {class: 'linkbtn', style: 'margin-top:4px', onclick: () => { SIDE.all = false; render(true); }}, `▴ Show the top ${SENDERS_TOP} only`) : null;
  const head = [h('div', {class: 'sside-h'}, h('div', null, h('div', {class: 'rrow'}, mode, helpBtn('senders')), h('div', {class: 'small muted', style: 'margin-top:6px'}, count), fewer), close),
    hidden.length ? h('div', {class: 'hidden-row'}, h('span', {class: 'small muted'}, 'Hidden:'), hidden) : null];
  if (!res) return head;
  if (!res.rows.length) return [head, h('div', {class: 'small muted', style: 'padding:8px 2px'}, domains ? 'No domains match.' : 'No senders match.')];
  const max = Math.max(1, res.rows[0].n);
  const bar = r => h('span', {class: 'bar'}, h('span', {style: `width:${100 * r.n / max}%;background:${r.automated ? 'var(--series-other)' : 'var(--accent)'}`}));
  const rows = domains
    ? res.rows.map(r => h('div', {class: 'srow-w'},
        h('button', {class: 'srow', 'aria-pressed': String(S.f.domain === r.domain),
          title: `${r.domain} · ${fmt(r.n)} messages from ${fmt(r.addresses)} ${r.addresses === 1 ? 'address' : 'addresses'}, ${(100 * r.share).toFixed(1)}%` + (r.automated ? ' · automated' : ''),
          onclick: () => { S.f = {...S.f, domain: S.f.domain === r.domain ? '' : r.domain}; render(); }},
          h('span', {class: 'who'}, h('span', {class: 'nm'}, r.domain || '(none)'), h('span', {class: 'ad'}, `${fmt(r.addresses)} ${r.addresses === 1 ? 'address' : 'addresses'}`)),
          h('span', {class: 'n'}, fmt(r.n)), bar(r)),
        r.domain ? hideBtn(r.domain, () => hideDomain(r.domain)) : null))
    : res.rows.map(r => h('div', {class: 'srow-w'},
        h('button', {class: 'srow', 'aria-pressed': String(S.f.sender === r.address),
          title: `${r.address} · ${fmt(r.n)} messages, ${(100 * r.share).toFixed(1)}%` + (r.automated ? ' · automated' : ''),
          onclick: () => { S.f = {...S.f, sender: S.f.sender === r.address ? '' : r.address}; render(); }},
          h('span', {class: 'who'}, h('span', {class: 'nm'}, r.name || r.address), r.name ? h('span', {class: 'ad'}, r.address) : null),
          h('span', {class: 'n'}, fmt(r.n)), bar(r)),
        hideBtn(r.name || r.address, () => hideSender(r))));
  const total = domains ? res.domains : res.senders;
  return [head,
    h('div', {class: 'srows'}, rows),
    total > res.rows.length && !SIDE.all ? h('button', {class: 'linkbtn', onclick: () => { SIDE.all = true; render(true); }}, `Show all ${fmt(total)}`) : null,
    SIDE.all ? h('button', {class: 'linkbtn', onclick: () => { SIDE.all = false; render(true); }}, `▴ Show the top ${SENDERS_TOP} only`) : null,
    SIDE.all && total > res.rows.length ? h('div', {class: 'small muted'}, `The first ${fmt(res.rows.length)} are shown.`) : null,
    peopleLegend()];
}

// The legend of the sender bars: a person, or automated mail.
const peopleLegend = () => h('div', {class: 'legend'}, h('span', null, h('span', {class: 'dot', style: 'background:var(--accent)'}), 'people'),
  h('span', null, h('span', {class: 'dot', style: 'background:var(--series-other)'}), 'automated'));

// ---- the account chips: one per account the archive knows, which switch it in or out of the list.
// They are also the legend of the account colours on the rows below them.
let knownAccounts = null;  // the last answer, so a redraw shows the chips at once
let SOLO_PREV = null;  // the accounts hidden before a solo, restored when it ends
function accountChips() {
  const box = h('div', {class: 'achips', role: 'group', 'aria-label': 'Accounts'});
  const draw = (list, counted) => {
    list = list.filter(r => r.value !== 'teams');  // Teams is a place of its own
    const ids = list.map(r => r.value);
    const off = new Set(S.f.exclude_accounts);
    const set = ex => { S.f = {...S.f, exclude_accounts: ex}; render(true); };
    // Like a mixer channel: the chip switches its account in or out; S solos it (only this
    // account), and S on the soloed account brings back the mix from before the solo. Alt/Option- or
    // Cmd-click on the chip solos too.
    const soloed = ids.filter(x => !off.has(x)).length === 1 ? ids.find(x => !off.has(x)) : null;
    fill(box, list.map(r => {
      const on = !off.has(r.value), name = ACCOUNT_NAME[r.value] ? acctName(r.value) : (r.name || r.value);
      const onlyThis = soloed === r.value;
      const solo = () => {
        if (onlyThis) { set(SOLO_PREV ? SOLO_PREV.filter(x => x !== r.value) : []); SOLO_PREV = null; return; }
        if (!soloed) SOLO_PREV = [...S.f.exclude_accounts];  // the mix to come back to
        set(ids.filter(x => x !== r.value));
      };
      return h('span', {class: 'achip-w' + (on ? '' : ' off') + (onlyThis ? ' solo' : ''), style: `--acct:${acctColor(r.value)}`},
        h('button', {class: 'achip' + (on ? '' : ' off'), id: 'achip-' + r.value, 'aria-pressed': String(on),
            title: `${name}: ${on ? 'shown' : 'hidden'} · click to ${on ? 'hide' : 'show'} it · Alt- or Cmd-click (or S) to ${onlyThis ? 'end the solo' : 'show only this one'}`,
            onclick: e => {
              if (e.altKey || e.metaKey) solo();
              else set(on ? [...S.f.exclude_accounts, r.value] : S.f.exclude_accounts.filter(x => x !== r.value));
            }},
          acctMark(r.value), h('span', {class: 'an'}, name), h('span', {class: 'n'}, counted ? fmt(r.n) : '…')),
        h('button', {class: 'asolo', 'aria-pressed': String(onlyThis), 'aria-label': onlyThis ? 'End the solo' : `Solo ${name}: show only this account`,
          title: onlyThis ? 'Solo is on: click to go back to the accounts you had before' : `Solo: show only ${name}`, onclick: solo}, 'S'));
    }), helpBtn('accounts'));
  };
  if (knownAccounts) draw(knownAccounts, false);
  else fill(box, h('span', {class: 'small muted'}, 'Accounts…'));
  return {el: box, set: list => {
    if (!list) return;
    knownAccounts = [...list].sort((a, b) => byAccount(a.value, b.value));
    draw(knownAccounts, true);
  }};
}

// One message in a list: the account colour as a mark and as a light wash over the row.
function messageRow(m) {
  // In Teams a found message opens its conversation where it was said, not on its own.
  const open = () => isTeams(m) && m.thread_id ? openThread(m.thread_id, {origin: row, around: m.id}) : openMessage(m.id, {origin: row});
  const row = h('div', {class: 'mrow acct-tint' + (m.seen === false ? ' unread' : ''), tabindex: '0', role: 'button', style: `--acct:${acctColor(m.account_id)}`,
      'data-pane-key': 'm:' + m.id, 'data-id': m.id, onclick: open, onkeydown: e => { if (e.key === 'Enter') open(); }},
    h('span', {class: 'm-acct'}, acctDot(m.account_id), h('span', {class: 'dir dir-' + m.direction, title: m.direction})),
    h('span', {class: 'm-from'}, m.direction === 'out' ? (isTeams(m) ? 'me' : 'To: …') : (m.from_name || m.from_address || '')),
    h('span', {class: 'm-main'}, isTeams(m) ? teamsKindPill(m) : null, h('span', {class: 'm-subj'}, m.subject || '(no subject)'), h('span', {class: 'm-snip'}, ' — ' + (m.snippet || '')),
      h('span', {class: 'm-pills'}, removedChip(m), m.importance && m.importance.level === 'high' ? h('span', {class: 'pill ac', title: `Important (score ${m.importance.score})`}, 'important') : null,
        m.has_attachments ? h('span', {class: 'pill', title: 'Has attachments'}, 'att') : null, m.is_automated ? h('span', {class: 'pill md'}, 'auto') : null)),
    h('span', {class: 'm-date'}, when(m.received_at)));
  return row;
}

// One conversation in a list: the account colour as for a message; who is in it (the owner as "me"),
// the subject or the chat's name, the latest line and when; how many messages, how many unread
// and how many theirs.
function threadPeopleText(t) {
  const people = t.people || [];
  if (people.length && people.every(p => p.me) && (t.recipients || []).length) return 'To: ' + t.recipients.map(firstName).join(', ');
  const many = people.length > 1;
  const names = people.map(p => p.me ? 'me' : many ? firstName(personName(p)) : personName(p));
  const more = (t.people_count || 0) - people.length;
  return names.join(', ') + (more > 0 ? ` +${more}` : '');
}
function threadRow(t) {
  const open = () => openThread(t.id, {origin: row});
  const teams = isTeams(t);
  const latestBy = isMine(t) ? 'You: ' : teams && t.chat_type !== 'oneOnOne' && t.from_name ? firstName(t.from_name) + ': ' : '';
  const everyone = (t.people || []).map(p => p.me ? 'me' : personName(p)).join(', ');
  const row = h('div', {class: 'mrow trow acct-tint' + (t.unread ? ' unread' : ''), tabindex: '0', role: 'button', style: `--acct:${acctColor(t.account_id)}`,
      'data-pane-key': 't:' + t.id, 'data-tid': t.id, onclick: open, onkeydown: e => { if (e.key === 'Enter') open(); }},
    h('span', {class: 'm-acct'}, acctDot(t.account_id), h('span', {class: 'dir dir-' + t.direction, title: 'Latest message: ' + t.direction})),
    h('span', {class: 'm-from', title: everyone + (t.people_count > (t.people || []).length ? ` and ${t.people_count - t.people.length} more` : '')},
      h('span', {class: 't-who'}, threadPeopleText(t)),
      t.message_count > 1 ? h('span', {class: 't-n', title: `${fmt(t.message_count)} messages`}, fmt(t.message_count)) : null),
    h('span', {class: 'm-main'}, teams ? teamsKindPill(t) : null, h('span', {class: 'm-subj'}, threadTitle(t)), h('span', {class: 'm-snip'}, ' — ' + latestBy + (t.snippet || '')),
      h('span', {class: 'm-pills'},
        t.level === 'high' ? h('span', {class: 'pill ac', title: 'A message in it is important'}, 'important') : null,
        t.unread ? h('span', {class: 'pill ac', title: `${fmt(t.unread)} unread`}, `${fmt(t.unread)} new`) : null,
        t.mine ? h('span', {class: 'pill t-mine', title: `${fmt(t.mine)} ${t.mine === 1 ? 'message' : 'messages'} from you`}, '↩ ' + fmt(t.mine)) : null,
        t.attachments ? h('span', {class: 'pill', title: 'Has attachments'}, 'att') : null, t.automated ? h('span', {class: 'pill md'}, 'auto') : null)),
    h('span', {class: 'm-date', title: t.last_at ? new Date(t.last_at).toLocaleString('sv-SE') : ''}, when(t.last_at)));
  return row;
}

// ---- the Messages filters as chips (docs/design.md): a set filter is a filled chip that says what it
// keeps, with × to clear it; an unset one is a quiet chip with a menu. On a phone, one Filters button
// opens them all in a sheet.
const FACET_COUNTS = {};
const MSG_FILTERS = [
  // Direction narrows mail only; Teams has its own, both ways unless narrowed (a chat is both sides of it),
  // shown only while Teams is in the list.
  {key: 'direction', label: 'Direction', opts: [['', 'Both ways'], ['in', 'Received'], ['out', 'Sent']], when: () => fScope() === 'mail'},
  {key: 'teams_direction', label: 'Both directions', sheet: 'Direction', opts: [['', 'Both directions'], ['in', 'Received'], ['out', 'Sent by me']], when: () => fScope() === 'teams',
   tip: 'A chat is both sides of it, so Teams shows both unless you narrow it here.'},
  {key: 'automated', label: 'Sender', opts: [['', 'Anyone'], ['false', 'People'], ['true', 'Automated']],
   tip: 'By sender (people or automated, the boundary Jev decides), else by origin: people, or people through a system; automated is every machine origin.'},
  {key: 'attachments', label: 'Attachments', opts: [['', 'With or without'], ['true', 'With attachments']]},
  {key: 'sphere', label: 'Work or personal', opts: [['', 'Both'], ['work', 'Work'], ['personal', 'Personal']], counted: true},
  {key: 'keep', label: 'Keep', opts: [['', 'Either'], ['keep', 'Worth keeping'], ['short_lived', 'Short-lived']], counted: true},
  // How sure the value filters must be (talos.sureness); below Likely, Jev's guesses Talos has not accepted come in.
  {key: 'sure', label: 'Sureness', opts: [['', 'As decided'], ['fact', 'Fact: you decided'], ['certain', 'Certain or surer'], ['confident', 'Confident or surer'],
    ['likely', 'Likely or surer'], ['maybe', 'Maybe or surer'], ['doubtful', 'Doubtful or surer']],
   tip: 'How sure the kind, type, topic, work or personal, and keep you filter on must be. From Likely down, Jev’s guesses that Talos has not accepted come in too. Only with such a filter set.'},
];
const shownFilters = () => MSG_FILTERS.filter(f => !f.when || f.when());
const optLabel = (f, v) => { const o = f.opts.find(x => x[0] === v); const n = f.counted && v && FACET_COUNTS[f.key] ? FACET_COUNTS[f.key][v] : null; return (o ? o[1] : v) + (n != null ? ` · ${fmt(n)}` : ''); };
function setFilter(key, v) { S.f = {...S.f, [key]: v}; render(true); }
function filterMenu(f, anchor) {
  if (menuEl) { closeMenu(true); if (menuReturn === anchor) return; }
  menuReturn = anchor;
  const cur = S.f[f.key] || '';
  menuEl = h('div', {class: 'menu', role: 'menu', 'aria-label': f.label, onkeydown: menuKeys}, h('div', {class: 'menu-h'}, f.label),
    f.opts.map(([v]) => h('button', {class: 'menu-item', role: 'menuitemradio', 'aria-checked': String(cur === v), tabindex: '-1', onclick: () => { closeMenu(); setFilter(f.key, v); }},
      h('span', {class: 'mi-l'}, h('span', {class: 'mi-c', 'aria-hidden': 'true'}, cur === v ? '✓' : ''), optLabel(f, v)))));
  document.body.append(menuEl);
  anchor.setAttribute('aria-expanded', 'true');
  placeMenu(anchor);
  (menuEl.querySelector('[aria-checked="true"]') || menuEl.querySelector('.menu-item')).focus();
}
function filterChip(f) {
  const v = S.f[f.key] || '';
  const b = h('button', {class: 'fc' + (v ? ' on' : ''), 'aria-haspopup': 'menu', title: f.tip || null, onmousedown: e => { if (menuEl) e.stopPropagation(); }, onclick: () => filterMenu(f, b)},
    v ? optLabel(f, v) : f.label, v ? null : h('span', {class: 'cv', 'aria-hidden': 'true'}, '▾'));
  if (!v) return b;
  return h('span', {class: 'fc-w'}, b, h('button', {class: 'fc-x', 'aria-label': `Clear ${f.label}`, title: 'Clear', onclick: () => setFilter(f.key, '')}, '×'));
}
const setFilterCount = () => shownFilters().filter(f => S.f[f.key]).length;
// The phone's sheet: every filter as a row of buttons, and the number the list will show.
function filterSheet() {
  const box = h('div', {class: 'fsheet'},
    shownFilters().map(f => h('div', {class: 'lk-f'}, h('div', {class: 'lk-l'}, f.sheet || f.label),
      (x => { x.classList.add('seg-fill'); return x; })(seg(f.opts.map(([v]) => [v, optLabel(f, v).replace(/^(Mail|Teams) both ways$/, 'Both ways').replace(/^(Anyone|With or without|Both|Either)$/, 'Any')]), S.f[f.key] || '',
        v => { S.f = {...S.f, [f.key]: v}; render(true); openPane(filterSheet(), {key: 'filters', title: 'Filters', replace: true}); })))),
    h('div', {class: 'rrow', style: 'margin-top:8px'},
      h('button', {class: 'btn primary', onclick: () => closePane()}, `Show ${fmt(listTotal)} ${listKind === 'threads' ? 'conversations' : 'mails'}`),
      h('button', {class: 'btn ghost', onclick: () => { S.f = defaults(); render(); closePane(); }}, 'Reset')));
  return box;
}

async function viewMessages() {
  const token = ++msgToken;
  // The list comes first; the facet counts, the account chips and the senders fill in when they arrive.
  const facetsP = api('/api/facets?' + filterParams(S.f)).catch(() => null);
  if (innerWidth < 820 && !SIDE.phoneAsked) SIDE.open = false;
  const sideP = !SIDE.open ? null : SIDE.mode === 'domains'
    ? api('/api/domains?' + filterParams(S.f, ['domain', 'sender']) + (SIDE.all ? '&all=1' : '&limit=' + SENDERS_TOP)).catch(e => ({error: errText(e)}))
    : api('/api/senders?' + filterParams(S.f, ['sender']) + (SIDE.all ? '&all=1' : '&limit=' + SENDERS_TOP)).catch(e => ({error: errText(e)}));
  await loadMessages(false);
  const chips = [];
  for (const k of ['account', 'domain', 'sender', 'thread']) if (S.f[k]) chips.push(h('span', {class: 'chip'}, `${k}: ${S.f[k]}`, h('button', {'aria-label': 'Remove', onclick: () => { S.f[k] = ''; render(); }}, '×')));
  // dimensions the filter bar has no menu for (ask, …), from a watcher or an aggregation
  for (const d of S.f.dims || []) chips.push(h('span', {class: 'chip'}, d.replace(':', ': ').replace(/_/g, ' '), h('button', {'aria-label': 'Remove', onclick: () => { S.f = {...S.f, dims: S.f.dims.filter(x => x !== d)}; render(); }}, '×')));
  // With the sidebar closed, the hidden senders and domains are listed with the other filters.
  if (!SIDE.open) chips.push(...hiddenChips('hidden: '));
  // Up and down (or j and k) move between the rows; with the pane open beside the list, the
  // message under the focus shows in it while the focus stays in the list.
  const list = h('div', {class: 'group mlist', onkeydown: e => {
    const row = e.target;
    if (e.altKey || e.metaKey || e.ctrlKey || !row.classList || !row.classList.contains('mrow')) return;
    const dir = e.key === 'ArrowDown' || e.key === 'j' ? 1 : e.key === 'ArrowUp' || e.key === 'k' ? -1 : 0;
    if (!dir) return;
    e.preventDefault();
    const next = dir > 0 ? row.nextElementSibling : row.previousElementSibling;
    if (!next || !next.classList.contains('mrow')) return;
    next.focus();
    if (paneSplit() && !PANE.max) next.dataset.tid ? openThread(Number(next.dataset.tid), {origin: next, focus: false}) : openMessage(Number(next.dataset.id), {origin: next, focus: false});
  }});
  // Rows that were not there the last time this same selection was drawn (a sync brought them) show a
  // brief wash in the accent colour that fades.
  const sel = listKind + '|' + filterParams(S.f).toString();
  const before = LIST_SEEN.sel === sel ? LIST_SEEN.ids : null;
  LIST_SEEN.sel = sel; LIST_SEEN.ids = new Set(listRows.map(m => m.id));
  const draw = () => {
    list.replaceChildren();
    if (!listRows.length) { list.append(empty('Nothing matches', 'Clear a filter or change the search.')); return; }
    listRows.forEach(m => { const row = listKind === 'threads' ? threadRow(m) : messageRow(m); if (before && !before.has(m.id)) row.classList.add('arrived'); list.append(row); });
    if (listRows.length < listTotal) list.append(h('div', {class: 'more'}, h('button', {class: 'btn', onclick: async () => { await loadMessages(true); draw(); }}, `Load more (${fmt(listTotal - listRows.length)} left)`)));
  };
  draw();
  const accts = accountChips();
  // The sidebar: the senders on top, then a panel per filter with a long list.
  const side = h('aside', {class: 'card sside', 'aria-label': 'Senders and filters'});
  const sendersBox = h('div'), panelsBox = h('div', {class: 'fpanels'}, FACET_PANELS.map(([k, t]) => facetPanel(k, t, null)));
  if (sideP) {
    fill(sendersBox, senderPanel(null));
    sideP.then(res => { if (token === msgToken) fill(sendersBox, res && res.error ? [senderPanel(null), h('div', {class: 'err'}, res.error)] : senderPanel(res)); });
  }
  fill(side, sendersBox, panelsBox);
  facetsP.then(f => { if (token === msgToken && f) { for (const k of ['sphere', 'keep']) FACET_COUNTS[k] = Object.fromEntries((f[k] || []).map(r => [r.value, r.n])); accts.set(f.account);
    fill(panelsBox, FACET_PANELS.map(([k, t]) => facetPanel(k, t, f[k] || []))); } });
  // A value chosen in a panel shows as a chip here too, so it is seen with the sidebar folded or closed.
  for (const [k, t] of FACET_PANELS) if (S.f[k]) chips.push(h('span', {class: 'chip'}, `${t.toLowerCase()}: ${String(S.f[k]).replace(/_/g, ' ')}`, h('button', {'aria-label': `Remove the ${t.toLowerCase()} filter`, onclick: () => { S.f = {...S.f, [k]: ''}; render(true); }}, '×')));
  const column = h('div', {class: 'mcol'}, accts.el, list);
  const nSet = setFilterCount() + chips.length;
  const isDefault = JSON.stringify(S.f) === JSON.stringify(defaults());
  return h('div', null,
    header(withHelp('Mail', 'mail'), listKind === 'threads' ? `${fmt(listTotal)} ${listTotal === 1 ? 'conversation matches' : 'conversations match'}` : `${fmt(listTotal)} match`,
      h('div', {class: 'rrow'}, h('div', {class: 'lmode', title: 'One row per message, or one per conversation (a mail thread). Remembered in this browser.'},
        seg([['messages', 'Messages'], ['threads', 'Conversations']], listKind, v => { if (v !== listKind) setListMode(v); })), composeButtons())),
    h('div', {class: 'msearch'},
      h('label', {class: 'search-l'}, icon('search'),
        h('input', {class: 'search', type: 'search', id: 'q', placeholder: 'Search in Swedish or English, or an address  (⌘K)', value: S.f.q, 'aria-label': 'Search', oninput: e => { clearTimeout(searchTimer); const v = e.target.value; searchTimer = setTimeout(() => { S.f.q = v; render(true); }, 250); }}))),
    h('div', {class: 'fchips mfilters'},
      h('button', {class: 'fc fc-all only-narrow', onclick: () => openPane(filterSheet(), {key: 'filters', title: 'Filters'})}, icon('tune'), nSet ? `Filters · ${nSet}` : 'Filters'),
      shownFilters().map(filterChip), chips,
      h('span', {class: 'sp'}),
      h('button', {class: 'btn ghost sm', 'aria-pressed': String(SIDE.open), title: SIDE.open ? 'Close the sidebar' : 'Show the senders and the filters by kind, origin, type, topic and label', onclick: () => { openSenders(!SIDE.open); render(true); }}, SIDE.open ? 'Hide sidebar' : 'Show sidebar'),
      h('button', {class: 'btn ghost sm', title: 'Save this selection as a watcher: it counts what arrives and tells you what is new', onclick: () => newWatcher({query: currentQuery(), name: S.f.q || ''})}, 'Watch'),
      h('button', {class: 'btn ghost sm', title: 'Count this selection in groups (by sender, domain, year …) on Discover', onclick: () => newAggregation()}, 'Aggregate'),
      isDefault ? null : h('button', {class: 'btn ghost sm', title: 'Back to received mail in every account', onclick: () => { S.f = defaults(); render(); }}, 'Reset')),
    SIDE.open ? h('div', {class: 'mwrap'}, side, column)
      : h('div', {class: 'mwrap closed'}, h('button', {class: 'sside-tab', title: 'Show the senders and the filters', 'aria-label': 'Show the sidebar',
          onclick: () => { openSenders(true); render(true); }}, h('span', null, '› Senders & filters')), column));
}

// ---------------------------------------------------------------- Teams, a place of its own
// Moved out of Mail on 30 September 2026 ("it gets messy with trying to shoehorn it in"): its own
// filters (both directions by default), the chats told apart from the team channels, a found message
// opened where it was said, posting with a confirmation, and a fast lane while the page is open.
const TEAMS = {fromThread: false, open: new Set(), checked: null, latest: null, boostUntil: 0, boxes: new Map()};
try { const x = JSON.parse(localStorage.getItem('talos-teams-open') || '[]'); if (Array.isArray(x)) TEAMS.open = new Set(x); } catch (e) {}
const TEAMS_KINDS = [['', 'Everything'], ['oneOnOne', 'One-to-one'], ['group', 'Group chats'], ['meeting', 'Meetings'], ['channel', 'Channels']];
const KIND_SHORT = {oneOnOne: '1:1', group: 'Group', meeting: 'Meeting'};
// A row's kind: 1:1, Group or Meeting for a chat; Team › Channel for a channel thread.
function teamsKindPill(r) {
  if (r.medium === 'teams_channel' || r.team) {
    const where = `${r.team || 'Team'} › ${r.channel || 'Channel'}`;
    return h('span', {class: 'pill tk tk-channel', title: 'A thread in the channel ' + where}, '# ' + (r.channel || 'Channel'));
  }
  const k = r.chat_type;
  return h('span', {class: 'pill tk tk-' + (k || 'chat'), title: CHAT_KIND[k] || 'Teams chat'}, KIND_SHORT[k] || 'Chat');
}
function freshTeams() { S.fs.teams = teamsDefaults(); TEAMS.fromThread = false; }

async function viewTeams() {
  const token = ++msgToken;
  if (S.f.medium !== 'teams') S.f = {...S.f, medium: 'teams'};
  const facetsP = api('/api/facets?' + filterParams(S.f)).catch(() => null);
  const placesP = api('/api/teams/places').catch(() => null);
  if (innerWidth < 820 && !SIDE.phoneAsked) SIDE.open = false;
  const peopleP = SIDE.open ? api('/api/senders?' + filterParams(S.f, ['sender']) + (SIDE.all ? '&all=1' : '&limit=' + SENDERS_TOP)).catch(e => ({error: errText(e)})) : null;
  await loadMessages(false);
  teamsLive();
  const chips = [];
  if (S.f.thread) chips.push(h('span', {class: 'chip'}, 'one conversation', h('button', {'aria-label': 'Remove', onclick: () => { S.f = {...S.f, thread: ''}; render(); }}, '×')));
  if (S.f.sender) chips.push(h('span', {class: 'chip'}, `from: ${S.f.sender}`, h('button', {'aria-label': 'Remove', onclick: () => { S.f = {...S.f, sender: ''}; render(); }}, '×')));
  if (S.f.team) chips.push(h('span', {class: 'chip'}, S.f.channel ? `${S.f.team} › ${S.f.channel}` : S.f.team,
    h('button', {'aria-label': 'Remove', onclick: () => { S.f = {...S.f, team: '', channel: ''}; render(); }}, '×')));
  for (const [k, ttl] of FACET_PANELS) if (S.f[k]) chips.push(h('span', {class: 'chip'}, `${ttl.toLowerCase()}: ${String(S.f[k]).replace(/_/g, ' ')}`, h('button', {'aria-label': `Remove the ${ttl.toLowerCase()} filter`, onclick: () => { S.f = {...S.f, [k]: ''}; render(true); }}, '×')));
  const list = h('div', {class: 'group mlist', onkeydown: e => {
    const row = e.target;
    if (e.altKey || e.metaKey || e.ctrlKey || !row.classList || !row.classList.contains('mrow')) return;
    const dir = e.key === 'ArrowDown' || e.key === 'j' ? 1 : e.key === 'ArrowUp' || e.key === 'k' ? -1 : 0;
    if (!dir) return;
    e.preventDefault();
    const next = dir > 0 ? row.nextElementSibling : row.previousElementSibling;
    if (next && next.classList.contains('mrow')) next.focus();
  }});
  const sel = 'teams|' + listKind + '|' + filterParams(S.f).toString();
  const before = LIST_SEEN.sel === sel ? LIST_SEEN.ids : null;
  LIST_SEEN.sel = sel; LIST_SEEN.ids = new Set(listRows.map(m => m.id));
  const draw = () => {
    list.replaceChildren();
    if (!listRows.length) { list.append(empty('Nothing matches', 'Clear a filter or change the search.')); return; }
    listRows.forEach(m => { const row = listKind === 'threads' ? threadRow(m) : messageRow(m); if (before && !before.has(m.id)) row.classList.add('arrived'); list.append(row); });
    if (listRows.length < listTotal) list.append(h('div', {class: 'more'}, h('button', {class: 'btn', onclick: async () => { await loadMessages(true); draw(); }}, `Load more (${fmt(listTotal - listRows.length)} left)`)));
  };
  draw();
  // The sidebar: the teams and their channels, then the people, then the filter panels.
  const placesBox = h('div'), peopleBox = h('div'), panelsBox = h('div', {class: 'fpanels'}, FACET_PANELS.filter(([k]) => k !== 'label').map(([k, ttl]) => facetPanel(k, ttl, null)));
  placesP.then(p => { if (token === msgToken && p) fill(placesBox, teamsTree(p)); });
  if (peopleP) { fill(peopleBox, senderPanel(null)); peopleP.then(res => { if (token === msgToken) fill(peopleBox, res && res.error ? h('div', {class: 'err'}, res.error) : senderPanel(res)); }); }
  facetsP.then(f => { if (token === msgToken && f) { for (const k of ['sphere', 'keep']) FACET_COUNTS[k] = Object.fromEntries((f[k] || []).map(r => [r.value, r.n]));
    fill(panelsBox, FACET_PANELS.filter(([k]) => k !== 'label').map(([k, ttl]) => facetPanel(k, ttl, f[k] || []))); } });
  const side = h('aside', {class: 'card sside', 'aria-label': 'Teams, people and filters'}, placesBox, peopleBox, panelsBox);
  const kind = seg(TEAMS_KINDS, S.f.chat || '', v => { S.f = {...S.f, chat: v, ...(v && v !== 'channel' ? {team: '', channel: ''} : {})}; render(true); });
  kind.classList.add('tk-seg');
  const nSet = setFilterCount() + chips.length + (S.f.chat ? 1 : 0);
  const isDefault = JSON.stringify(S.f) === JSON.stringify(teamsDefaults());
  const live = TEAMS.checked ? `Checked ${new Date(TEAMS.checked).toLocaleTimeString('sv-SE')}` : 'Checking for new messages…';
  return h('div', null,
    header(withHelp('Teams', 'teams'), [`${fmt(listTotal)} ${listKind === 'threads' ? (listTotal === 1 ? 'conversation' : 'conversations') : (listTotal === 1 ? 'message' : 'messages')} · `,
      h('span', {id: 'teams-live', title: 'Talos looks for new chat messages every 20 seconds, all the time; channels every 5 minutes'}, live)],
      h('div', {class: 'rrow'}, h('div', {class: 'lmode', title: 'One row per conversation, or one per message (a found message opens where it was said). Remembered in this browser.'},
        seg([['threads', 'Conversations'], ['messages', 'Messages']], listKind, v => { if (v !== listKind) setListMode(v); })))),
    h('div', {class: 'msearch'},
      h('label', {class: 'search-l'}, icon('search'),
        h('input', {class: 'search', type: 'search', id: 'q', placeholder: 'Search the chats and channels', value: S.f.q, 'aria-label': 'Search Teams', oninput: e => { clearTimeout(searchTimer); const v = e.target.value; searchTimer = setTimeout(() => { S.f.q = v; render(true); }, 250); }}))),
    h('div', {class: 'fchips mfilters'}, kind,
      h('button', {class: 'fc fc-all only-narrow', onclick: () => openPane(filterSheet(), {key: 'filters', title: 'Filters'})}, icon('tune'), nSet ? `Filters · ${nSet}` : 'Filters'),
      shownFilters().map(filterChip), chips,
      h('span', {class: 'sp'}),
      h('button', {class: 'btn ghost sm', 'aria-pressed': String(SIDE.open), onclick: () => { openSenders(!SIDE.open); render(true); }}, SIDE.open ? 'Hide sidebar' : 'Show sidebar'),
      isDefault ? null : h('button', {class: 'btn ghost sm', title: 'Back to every chat and channel, both directions', onclick: () => { freshTeams(); render(); }}, 'Reset')),
    SIDE.open ? h('div', {class: 'mwrap'}, side, h('div', {class: 'mcol'}, list))
      : h('div', {class: 'mwrap closed'}, h('button', {class: 'sside-tab', 'aria-label': 'Show the sidebar', onclick: () => { openSenders(true); render(true); }}, h('span', null, '› Teams & people')), h('div', {class: 'mcol'}, list)));
}

// The sidebar's teams: each team folds open to its channels; a click narrows the list to it. Beside a
// channel, ✎ starts a new post in it.
function teamsTree(p) {
  const chatN = k => fmt((p.chats || {})[k] || 0);
  const pick = patch => { S.f = {...S.f, ...patch}; render(true); };
  const save = () => { try { localStorage.setItem('talos-teams-open', JSON.stringify([...TEAMS.open])); } catch (e) {} };
  return h('div', {class: 'tside'},
    h('div', {class: 'fp-h'}, h('b', null, 'Chats')),
    [['oneOnOne', 'One-to-one'], ['group', 'Group chats'], ['meeting', 'Meetings']].map(([k, l]) =>
      h('button', {class: 'tside-row' + (S.f.chat === k ? ' on' : ''), onclick: () => pick({chat: S.f.chat === k ? '' : k, team: '', channel: ''})},
        h('span', {class: 'pill tk tk-' + k}, KIND_SHORT[k]), h('span', {class: 'tside-l'}, l), h('span', {class: 'n', title: 'conversations'}, chatN(k)))),
    h('div', {class: 'fp-h'}, h('b', null, 'Teams')),
    (p.teams || []).length ? p.teams.map(tm => {
      const open = TEAMS.open.has(tm.team) || S.f.team === tm.team;
      return h('div', {class: 'tside-team'},
        h('div', {class: 'tside-row' + (S.f.team === tm.team && !S.f.channel ? ' on' : '')},
          h('button', {class: 'tside-fold', 'aria-expanded': String(open), 'aria-label': (open ? 'Fold ' : 'Open ') + tm.team,
            onclick: () => { open ? TEAMS.open.delete(tm.team) : TEAMS.open.add(tm.team); save(); render(true); }}, open ? '▾' : '▸'),
          h('button', {class: 'tside-l linkish', onclick: () => pick(S.f.team === tm.team && !S.f.channel ? {team: '', channel: ''} : {chat: 'channel', team: tm.team, channel: ''})}, tm.team),
          h('span', {class: 'n', title: 'messages'}, fmt(tm.n))),
        open ? tm.channels.map(ch => h('div', {class: 'tside-row tside-ch' + (S.f.team === tm.team && S.f.channel === ch.channel ? ' on' : '')},
          h('button', {class: 'tside-l linkish', title: `${ch.threads} threads, ${ch.n} messages`, onclick: () => pick(S.f.channel === ch.channel && S.f.team === tm.team ? {channel: ''} : {chat: 'channel', team: tm.team, channel: ch.channel})}, '# ' + ch.channel),
          h('button', {class: 'tside-post', title: `New post in ${tm.team} › ${ch.channel}`, 'aria-label': `New post in ${tm.team} › ${ch.channel}`,
            onclick: () => openPane(h('div', null, h('h2', null, `New post in ${tm.team} › ${ch.channel}`), teamsComposer({team: tm.team, channel: ch.channel}, 'A new post in the channel', {big: true})),
              {key: 'tpost:' + tm.team + '/' + ch.channel, title: 'New post'})}, '✎'),
          h('span', {class: 'n'}, fmt(ch.n)))) : null);
    }) : h('div', {class: 'small muted'}, 'No channels read yet.'));
}

// ---- writing in Teams: a box under the conversation (or a new post in a channel). In a conversation Enter
// sends (Shift + Enter for a new line); in a new channel post ⌘/Ctrl + Enter does. There is no separate
// confirmation: the box belongs to one conversation, and the owner's Enter in it is the
// confirmation. The page asks for a one-time token for exactly that target and text and redeems it at once
// (promise 1, send.py). A conversation's box is kept per conversation (TEAMS.boxes), so the live refresh
// redraws the messages round it without losing what is being written.
function teamsComposer(target, label, opts = {}) {
  const key = target.thread_id ? 't:' + target.thread_id : null;
  if (key && TEAMS.boxes.has(key)) return TEAMS.boxes.get(key);
  const enterSends = !opts.big;
  const box = h('div', {class: 'tcompose' + (opts.big ? ' big' : '')});
  const text = h('textarea', {class: 'field', rows: opts.big ? '8' : '2', 'aria-label': label,
    placeholder: label + (enterSends ? ' (Enter sends, Shift + Enter for a new line)' : ' (⌘/Ctrl + Enter to send)'),
    onkeydown: e => {
      if (e.key !== 'Enter' || e.isComposing || e.shiftKey || e.altKey) return;
      if (enterSends || e.metaKey || e.ctrlKey) { e.preventDefault(); send(); }
    }});
  const err = h('div', {class: 'err', role: 'alert'});
  const sendBtn = h('button', {class: 'btn primary', onclick: () => send()}, 'Send');
  let busy = false;
  const send = async () => {
    if (busy) return;
    err.replaceChildren();
    const body = text.value;
    if (!body.trim()) return;
    busy = true; sendBtn.disabled = true; text.readOnly = true;
    let res;
    try {
      const c = await post('/api/teams/confirm', {...target, text: body}, true);
      res = await post('/api/teams/send', {...target, text: body, token: c.token});
    } catch (e) { err.replaceChildren(errText(e)); }
    busy = false; sendBtn.disabled = false; text.readOnly = false;
    if (!res) return;
    if (text.value === body) text.value = '';
    text.focus({preventScroll: true});
    if (opts.big) flash(`Posted to ${res.where}.`);
    teamsAfterPost();
  };
  fill(box, text, h('div', {class: 'rrow', style: 'margin-top:6px'}, sendBtn,
    h('span', {class: 'small muted'}, enterSends ? 'Plain text. Enter sends to this conversation.' : 'Plain text. ⌘/Ctrl + Enter or Send posts it in this channel.')), err);
  if (key) TEAMS.boxes.set(key, box);
  return box;
}
// After a post: the server has started a look for it and boosted the lane (syncnow.TeamsFast.boost), so the page
// asks often for a while too, wherever the owner is; the conversation is redrawn when the post, or an answer, arrives.
function teamsAfterPost() {
  CACHE.clear();
  TEAMS.boostUntil = Date.now() + 120000;
  teamsLive(true);
}
// The open conversation redrawn in place: its box keeps its text and the cursor, and a chat read to the end
// stays at the end, so a new message shows.
function teamsRedrawOpen() {
  if (!PANE.open || !PANE.cur || !PANE.cur.key.startsWith('t:')) return;
  const b = PANE.body, atEnd = b.scrollHeight - b.scrollTop - b.clientHeight < 60;
  const ta = b.querySelector('.tcompose textarea');
  const typing = ta && document.activeElement === ta ? [ta.selectionStart, ta.selectionEnd] : null;
  openThread(Number(PANE.cur.key.slice(2)), {replace: true}).then(() => {
    if (atEnd) b.scrollTop = b.scrollHeight;
    if (typing && ta.isConnected) { ta.focus({preventScroll: true}); ta.setSelectionRange(...typing); }
  });
}

// ---- live: the Talos background service (<prefix>.teams) looks for new chat messages every 20 seconds, all
// the time. While the place is open the page asks the server every TEAMS_LIVE_MS what the newest Teams message
// is, and redraws the list, and the open conversation, when it changed. Should the service have stopped (its
// last look over a minute ago), the page starts a look itself. For a while after a post (the server's boost)
// it asks every TEAMS_BOOST_MS instead and has the server look each time it may (every 5 seconds), on any page.
const TEAMS_LIVE_MS = 10000;
const TEAMS_BOOST_MS = 2500;
let teamsTimer = null, teamsBusy = false;
const teamsBoosted = () => TEAMS.boostUntil > Date.now();
// now: look at once (after a post) instead of at the next turn. One look at a time; a look under way
// schedules the next by the boost when it is done.
function teamsLive(now = false) {
  if (teamsBusy) return;
  if (teamsTimer) { if (!now) return; clearTimeout(teamsTimer); teamsTimer = null; }
  teamsTick();
}
async function teamsTick() {
  teamsTimer = null;
  if (S.view !== 'teams' && !teamsBoosted()) return;
  teamsBusy = true;
  if (!document.hidden) try {
    if (teamsBoosted()) await post('/api/teams/refresh', {}, true);
    let st = await fetchJSON('/api/teams/refresh');
    if (!st.checked_at || Date.now() - Date.parse(st.checked_at) > 60000) {
      await post('/api/teams/refresh', {}, true);
      await new Promise(r => setTimeout(r, 5000));
      st = await fetchJSON('/api/teams/refresh');
    }
    if (st.boost) TEAMS.boostUntil = Math.max(TEAMS.boostUntil, Date.now() + st.boost * 1000);
    TEAMS.checked = st.checked_at ? Date.parse(st.checked_at) : Date.now();
    const arrived = TEAMS.latest != null && st.latest_id != null && st.latest_id !== TEAMS.latest;
    TEAMS.latest = st.latest_id;
    if (arrived) {
      CACHE.clear();
      teamsRedrawOpen();
      if (S.view === 'teams') render(true);
    } else {
      const el = document.getElementById('teams-live');
      if (el) el.textContent = `Checked ${new Date(TEAMS.checked).toLocaleTimeString('sv-SE')}` + (teamsBoosted() ? ' · looking every 5 s' : '');
    }
  } catch (e) {}
  teamsBusy = false;
  teamsTimer = setTimeout(teamsTick, teamsBoosted() ? TEAMS_BOOST_MS : TEAMS_LIVE_MS);
}

// ---------------------------------------------------------------- composing and sending
// A mail the owner writes, in the reading pane (key 'd:<draft id>'). Talos sends a mail only when they
// press Send on it and then confirms the sending account (CLAUDE.md, promise 1). The pane is
// built round the From block: the account's name, address and colour, large, and the whole pane
// washed in that colour. Send names the address ("Send from …"), and the confirmation restates it
// in large type with every recipient; there is no way round it and no "don't ask again". The
// draft is saved in Talos while they type; nothing reaches a server until "Yes, send from …".
const COMPOSE_TITLE = {new: 'New email', reply: 'Reply', reply_all: 'Reply all', forward: 'Forward'};
const kindOfMode = mode => mode === 'new' ? 'new' : 'reply';
// "a@b, Name <c@d>, "Nyström, Oskar" <e@f>" → the addresses, commas inside quotes or <> kept.
function splitAddresses(text) {
  const out = []; let cur = '', q = false, ang = false;
  for (const ch of String(text || '')) {
    if (ch === '"' && !ang) q = !q;
    else if (ch === '<' && !q) ang = true;
    else if (ch === '>' && !q) ang = false;
    if ((ch === ',' || ch === ';') && !q && !ang) { if (cur.trim()) out.push(cur.trim()); cur = ''; continue; }
    cur += ch;
  }
  if (cur.trim()) out.push(cur.trim());
  return out;
}
async function newEmail() {
  let v;
  try { v = await post('/api/drafts', {mode: 'new'}); } catch (e) { flash(errText(e)); return; }
  openCompose(v);
  refreshDraftCount();
}
async function replyTo(m, mode) {
  let v;
  try { v = await post('/api/drafts', {mode, message_id: m.id}); } catch (e) { flash(errText(e)); return; }
  openCompose(v);
  refreshDraftCount();
}
function openDraft(id) {
  return paneLoad({key: 'd:' + id, title: 'Draft'}, async () => {
    const v = await fetchJSON(`/api/drafts/${id}`);
    return {node: composeForm(v), label: v.draft.subject || COMPOSE_TITLE[v.draft.mode]};
  });
}
function openCompose(v) {
  openPane(composeForm(v), {key: 'd:' + v.draft.id, title: COMPOSE_TITLE[v.draft.mode] || 'New email', label: v.draft.subject || COMPOSE_TITLE[v.draft.mode]});
  const first = PANE.body.querySelector(v.draft.mode === 'reply' || v.draft.mode === 'reply_all' ? '.cbody' : '.caddr');
  if (first) first.focus({preventScroll: true});
}
// Reply, Reply all and Forward under a mail (not a Teams message: that is answered in Teams).
function replyBar(m) {
  if (isTeams(m)) return null;
  const from = acctName(m.account_id);
  const b = (mode, sym, label, color, key) => abtn('main', sym, label, () => replyTo(m, mode), {color, key,
    title: label + (mode === 'forward' ? '' : ` from ${from}, the account it came to`) + ` (${key.toUpperCase()})`});
  return agroup('Reply', [b('reply', 'reply', 'Reply', 'var(--act-reply)', MESSAGE_KEYS.reply),
    b('reply_all', 'replyall', 'Reply all', 'var(--act-replyall)', MESSAGE_KEYS.reply_all),
    b('forward', 'forward', 'Forward', 'var(--act-forward)', MESSAGE_KEYS.forward)], {cls: 'agrp-reply', note: `From ${from}, the account it came to`});
}
// How many drafts wait, for the Drafts button in Messages.
let draftCount = null;
function refreshDraftCount() {
  fetchJSON('/api/drafts').then(rows => {
    draftCount = rows.length;
    document.querySelectorAll('.drafts-n').forEach(el => fill(el, draftCount ? String(draftCount) : ''));
  }).catch(() => {});
}
// ---- Sync now: the mail accounts synced at once, between the 5-minute runs (never Teams). The
// button follows the run and says what came in; the page redraws in place when it is done.
const SYNC = {running: false, timer: null};
async function syncNow(accounts) {
  if (SYNC.running) return;
  SYNC.running = true;
  document.querySelectorAll('.syncbtn').forEach(b => { b.disabled = true; b.textContent = 'Syncing…'; });
  try { await post('/api/sync', accounts ? {accounts} : {}, true); } catch (e) { SYNC.running = false; flash(errText(e)); render(true); return; }
  const look = async () => {
    let st;
    try { st = await fetchJSON('/api/sync'); } catch (e) { SYNC.timer = setTimeout(look, 3000); return; }
    if (st.running) { SYNC.timer = setTimeout(look, 2000); return; }
    SYNC.running = false;
    const res = Object.entries(st.results || {});
    const added = res.reduce((n, [, r]) => n + (r.added || 0), 0);
    const notes = res.filter(([, r]) => r.note).map(([a, r]) => `${acctName(a)}: ${r.note}`);
    flash((added ? `${fmt(added)} new ${added === 1 ? 'message' : 'messages'}` : 'No new mail') + (notes.length ? ' · ' + notes.join(' · ') : ''));
    CACHE.clear();
    render(true);
  };
  SYNC.timer = setTimeout(look, 1500);
}
const syncButton = (cls = 'btn') => h('button', {class: cls + ' syncbtn', disabled: SYNC.running,
  title: 'Fetch new mail from every mail account now, instead of waiting for the 5-minute sync. Teams keeps its own 5-minute sync.',
  onclick: () => syncNow()}, SYNC.running ? 'Syncing…' : '↻ Sync mail now');

function composeButtons() {
  if (draftCount == null) refreshDraftCount();
  return h('div', {class: 'rrow cbtns'},
    syncButton('btn ghost'),
    h('button', {class: 'btn ghost', onclick: openDrafts, title: 'Mails you started and have not sent; signatures and sending settings are there too'}, 'Drafts', h('span', {class: 'pill drafts-n'}, draftCount ? String(draftCount) : '')),
    h('button', {class: 'btn primary', onclick: newEmail, title: 'Write a new mail. You choose the account it goes from, and confirm it before it is sent.'}, 'New email'));
}
function openDrafts() {
  return paneLoad({key: 'drafts', title: 'Drafts'}, async () => {
    const rows = await fetchJSON('/api/drafts');
    draftCount = rows.length;
    return {label: 'Drafts', node: h('div', null,
      h('h2', null, 'Drafts'),
      h('p', {class: 'small muted'}, 'Saved in Talos only, while you write. A draft never reaches a mail server; discarding one removes it from Talos.'),
      rows.length ? rows.map(r => h('div', {class: 'imp-row acct-tint', tabindex: '0', role: 'button', 'data-pane-key': 'd:' + r.id, style: `--acct:${r.account_id ? acctColor(r.account_id) : 'transparent'}`,
          onclick: () => openDraft(r.id), onkeydown: e => { if (e.key === 'Enter') openDraft(r.id); }},
        h('span', {class: 'imp-l'}, r.account_id ? acctDot(r.account_id) : h('span', {class: 'acct-dot', style: 'background:var(--axis)', title: 'No account chosen'})),
        h('span', {class: 'imp-m'}, h('b', null, r.subject || '(no subject)'),
          h('span', {class: 'small muted'}, (COMPOSE_TITLE[r.mode] || r.mode) + (r.to_addrs.length ? ' · to ' + r.to_addrs.join(', ') : ''))),
        h('span', {class: 'imp-r small muted'}, when(r.updated_at)))) : empty('No drafts', 'New email in Messages starts one.'),
      h('div', {class: 'dactions'}, h('button', {class: 'btn primary', onclick: newEmail}, 'New email'),
        h('button', {class: 'btn', onclick: openSendSettings, title: 'Default account, your name, signatures and the send log'}, 'Signatures and sending')))};
  });
}

function composeForm(v) {
  const d = {...v.draft};
  const senders = v.senders, sigs = v.signatures, suggested = v.suggested || {};
  let warnings = v.warnings || [], timer = null, saving = null, dirty = false, showCc = d.cc_addrs.length > 0 || d.bcc_addrs.length > 0;
  const root = h('div', {class: 'compose'});
  const sender = () => senders.find(s => s.id === d.account_id) || null;
  const status = h('span', {class: 'small muted cstatus', role: 'status'});
  const err = h('div', {class: 'err', role: 'alert'});

  // ---- the From block
  const fromBox = h('section', {class: 'cfrom', 'aria-label': 'Send from'});
  const how = h('div', {class: 'note chow', hidden: true});
  const drawFrom = () => {
    const s = sender();
    root.style.setProperty('--acct', s ? acctColor(s.id) : 'var(--axis)');
    root.classList.toggle('no-acct', !s);
    const pick = senders.map(x => {
      const on = x.id === d.account_id;
      return h('button', {class: 'cpick' + (on ? ' on' : ''), role: 'radio', 'aria-checked': String(on), style: `--acct:${acctColor(x.id)}`,
          'aria-label': `${x.name}, ${x.address}` + (x.pickable ? '' : `, ${x.status}`),
          'aria-disabled': x.pickable ? null : 'true', title: x.pickable ? `Send from ${x.address}` : `${x.name}: ${x.status}. ${x.why || ''}`,
          onclick: () => {
            if (!x.pickable) { fill(how, h('div', null, h('b', null, `${x.name}: ${x.status}. `), x.why || '', h('div', {class: 'mono small', style: 'margin-top:6px;overflow-wrap:anywhere'}, x.how || ''))); how.hidden = false; return; }
            how.hidden = true;
            if (d.account_id === x.id) return;
            d.account_id = x.id;
            // The signature follows the account: its default for this kind of mail.
            d.signature_id = suggested[x.id] == null ? null : suggested[x.id];
            drawFrom(); drawSig(); drawSend(); schedule(0);
          }},
        h('span', {class: 'adot', 'aria-hidden': 'true'}),
        h('span', {class: 'cp-t'}, h('b', null, x.name), h('span', {class: 'cp-a'}, x.address)),
        x.pickable ? (x.default ? h('span', {class: 'pill', title: 'The default account for new mail'}, 'default') : null)
          : h('span', {class: 'pill md'}, x.status));
    });
    // A reply says which account the original came to, and when it is not that one, says so plainly.
    const orig = d.mode !== 'new' && d.original_account_id ? senders.find(x => x.id === d.original_account_id) : null;
    const origNote = !orig ? null : s && s.id === orig.id ? h('div', {class: 'cfrom-orig'}, 'The account the original came to.')
      : h('div', {class: 'cfrom-orig warn'}, `The original came to ${orig.name} (${orig.address})` + (orig.pickable ? '.' : `, which cannot send: ${orig.status}.`)
          + (s ? ` You are answering from ${s.name}.` : ''));
    fill(fromBox,
      h('div', {class: 'cfrom-l'}, 'From'),
      s ? h('div', {class: 'cfrom-big'}, h('span', {class: 'cfrom-dot', 'aria-hidden': 'true'}),
            h('div', {style: 'min-width:0'}, h('div', {class: 'cfrom-addr'}, s.address), h('div', {class: 'cfrom-name'}, s.name))) :
        h('div', {class: 'cfrom-big none'}, 'Choose the account to send from'),
      origNote,
      h('div', {class: 'cpicks', role: 'radiogroup', 'aria-label': 'Account to send from'}, pick),
      how);
  };

  // ---- recipients, with suggestions from the people the owner has mailed
  const addrField = (key, label) => {
    const input = h('input', {class: 'field caddr', id: 'c-' + key, autocomplete: 'off', spellcheck: 'false', value: d[key].join(', '),
      placeholder: key === 'to_addrs' ? 'name@example.com, …' : ''});
    const list = h('div', {class: 'csugg', role: 'listbox', hidden: true});
    let st = null, seq = 0;
    const lastToken = () => { const parts = input.value.split(/[,;]/); return parts[parts.length - 1].trim(); };
    const choose = r => {
      const parts = splitAddresses(input.value);
      if (!/[,;]\s*$/.test(input.value)) parts.pop();
      const name = r.name && r.name.toLowerCase() !== r.address ? (/[,;<>"@()]/.test(r.name) ? `"${r.name.replace(/"/g, '')}"` : r.name) + ` <${r.address}>` : r.address;
      input.value = [...parts, name].join(', ') + ', ';
      list.hidden = true;
      input.focus();
      changed();
    };
    input.addEventListener('input', () => {
      changed();
      clearTimeout(st);
      const q = lastToken();
      if (q.length < 2) { list.hidden = true; return; }
      const mine = ++seq;
      st = setTimeout(async () => {
        let rows;
        try { rows = await fetchJSON('/api/compose/suggest?q=' + encodeURIComponent(q)); } catch (e) { return; }
        if (mine !== seq) return;
        fill(list, rows.map(r => h('button', {class: 'csugg-i', role: 'option', type: 'button', onmousedown: e => e.preventDefault(), onclick: () => choose(r),
            onkeydown: e => { if (e.key === 'ArrowDown' && e.target.nextElementSibling) { e.preventDefault(); e.target.nextElementSibling.focus(); }
                              else if (e.key === 'ArrowUp') { e.preventDefault(); (e.target.previousElementSibling || input).focus(); }
                              else if (e.key === 'Escape') { e.preventDefault(); list.hidden = true; input.focus(); } }},
          h('span', null, r.name ? h('b', null, r.name) : null, ' ', r.address),
          h('span', {class: 'small muted'}, r.sent ? `you mailed ${fmt(r.sent)}×` : r.received ? `wrote to you ${fmt(r.received)}×` : ''))));
        list.hidden = !rows.length;
      }, 150);
    });
    input.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown' && !list.hidden && list.firstChild) { e.preventDefault(); list.firstChild.focus(); }
      else if (e.key === 'Escape' && !list.hidden) { e.preventDefault(); list.hidden = true; }
    });
    input.addEventListener('blur', () => setTimeout(() => { if (!list.contains(document.activeElement)) list.hidden = true; }, 150));
    return {input, row: [h('label', {class: 'flabel', for: 'c-' + key}, label), h('div', {class: 'csuggw'}, input, list)]};
  };
  const to = addrField('to_addrs', 'To'), cc = addrField('cc_addrs', 'Cc'), bcc = addrField('bcc_addrs', 'Bcc');
  const subject = h('input', {class: 'field', id: 'c-subject', value: d.subject, maxlength: '998', oninput: () => changed()});
  const body = h('textarea', {class: 'field cbody', id: 'c-body', rows: '12', placeholder: 'Write your mail…', oninput: () => changed()}, d.body);
  const ccRows = h('div', {class: 'cgrid', hidden: !showCc}, cc.row, bcc.row);
  const ccToggle = h('button', {class: 'linkbtn', type: 'button', hidden: showCc, onclick: () => { showCc = true; ccRows.hidden = false; ccToggle.hidden = true; cc.input.focus(); }}, 'Cc / Bcc');

  // ---- the signature
  const sigBox = h('div', {class: 'csig'});
  const drawSig = () => {
    const kind = kindOfMode(d.mode);
    const fits = sigs.filter(x => d.account_id && x.accounts.includes(d.account_id) && (kind === 'new' ? x.for_new : x.for_reply));
    const cur = sigs.find(x => x.id === d.signature_id) || null;
    const options = cur && !fits.includes(cur) ? [...fits, cur] : fits;
    const sel = h('select', {class: 'sel', 'aria-label': 'Signature', onchange: e => { d.signature_id = e.target.value ? Number(e.target.value) : null; drawSig(); changed(); }},
      h('option', {value: ''}, 'No signature'),
      options.map(x => h('option', {value: String(x.id), selected: x.id === d.signature_id}, x.name + (x.body_html ? ' (formatted)' : ''))));
    fill(sigBox,
      h('div', {class: 'rrow', style: 'justify-content:space-between'},
        h('span', {class: 'rrow'}, h('span', {class: 'flabel'}, 'Signature'), sel),
        h('button', {class: 'linkbtn', type: 'button', onclick: openSendSettings}, 'Manage signatures')),
      cur ? h('div', {class: 'csig-t'}, cur.body_text || '(formatted only)') :
        h('div', {class: 'small muted'}, !d.account_id ? 'Choose the account first: signatures follow it.' : fits.length ? 'None chosen.' : `No signature applies to ${acctName(d.account_id)} for ${kind === 'new' ? 'new mail' : 'replies and forwards'}.`));
  };

  // ---- the quoted original
  const quote = d.quoted ? (() => {
    const inc = h('input', {type: 'checkbox', class: 'switch', id: 'c-quote', checked: d.include_quote, onchange: e => { d.include_quote = e.target.checked; changed(); }});
    return h('details', {class: 'cquote'},
      h('summary', null, d.mode === 'forward' ? 'The forwarded message' : 'The quoted original', h('span', {class: 'small muted'}, ` · ${fmt(d.quoted.split('\n').length)} lines`)),
      h('label', {class: 'rrow small', for: 'c-quote', style: 'margin:6px 0'}, inc, 'Include it below your text' + (d.mode === 'forward' ? ' (attachments of the original are not forwarded in this version)' : '')),
      h('div', {class: 'body-text cquote-t'}, d.quoted));
  })() : null;

  // ---- warnings: they never stop a send, and the confirmation says them again
  const warnBox = h('div', {class: 'cwarns', 'aria-live': 'polite'});
  const drawWarn = () => fill(warnBox, warnings.map(w => h('div', {class: 'cwarn ' + w.level}, h('span', {class: 'cw-i', 'aria-hidden': 'true'}, w.level === 'warn' ? '!' : 'i'), w.text)));

  // ---- Send
  const sendBtn = h('button', {class: 'btn primary csend', onclick: () => confirmSend()});
  const drawSend = () => {
    const s = sender();
    sendBtn.disabled = !s;
    fill(sendBtn, s ? `Send from ${s.address}` : 'Choose an account to send from');
  };
  const fields = () => ({account_id: d.account_id, to_addrs: splitAddresses(to.input.value), cc_addrs: splitAddresses(cc.input.value),
                         bcc_addrs: splitAddresses(bcc.input.value), subject: subject.value, body: body.value,
                         include_quote: d.include_quote, signature_id: d.signature_id});
  const changed = () => { dirty = true; fill(status, 'Editing…'); schedule(); };
  const schedule = (ms = 700) => { clearTimeout(timer); timer = setTimeout(save, ms); };
  const save = async () => {
    clearTimeout(timer);
    if (saving) await saving;
    const f = fields();
    saving = (async () => {
      try {
        const r = await post(`/api/drafts/${d.id}`, f);
        dirty = false;
        warnings = r.warnings; drawWarn();
        fill(status, 'Saved in Talos · ' + new Date().toLocaleTimeString('sv-SE', {hour: '2-digit', minute: '2-digit'}));
      } catch (e) { fill(status, h('span', {class: 'err'}, 'Not saved: ' + errText(e))); }
    })();
    await saving;
    saving = null;
  };
  const discard = async () => {
    if (!confirm('Discard this draft? It is removed from Talos.')) return;
    clearTimeout(timer);
    try { await post(`/api/drafts/${d.id}/discard`, {}); } catch (e) { flash(errText(e)); return; }
    refreshDraftCount();
    closePane();
    flash('Draft discarded.');
  };

  // ---- the confirmation: From in large type, every recipient, the subject, the attachments.
  // "Yes, send from …" is the only way to send; the focus starts on "Back to editing".
  async function confirmSend() {
    clearTimeout(timer);
    fill(err);
    sendBtn.disabled = true;
    let c;
    try { c = await post(`/api/drafts/${d.id}/confirm`, fields()); }
    catch (e) { fill(err, errText(e)); drawSend(); return; }
    finally { dirty = false; }
    drawSend();
    warnings = c.warnings; drawWarn();
    const s = c.summary, acct = senders.find(x => x.id === s.account_id) || {name: s.account_id};
    const line = (label, list) => list.length ? [h('dt', null, label), h('dd', null, list.join(', '))] : null;
    const msg = h('div', {class: 'err', role: 'alert'});
    const yes = h('button', {class: 'btn cc-yes', disabled: true}, `Yes, send from ${s.from_addr}`);
    const back = h('button', {class: 'btn', onclick: () => close()}, 'Back to editing');
    const dlg = h('dialog', {class: 'cconfirm', style: `--acct:${acctColor(s.account_id)}`, 'aria-labelledby': 'cc-h',
        onkeydown: e => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); } },
        oncancel: e => { e.preventDefault(); close(); }},
      h('h2', {id: 'cc-h'}, 'Send this mail?'),
      h('div', {class: 'cc-from'}, h('div', {class: 'cfrom-l'}, 'From'),
        h('div', {class: 'cc-addr'}, s.from_addr),
        h('div', {class: 'cfrom-name'}, acct.name + (s.from_name ? ` · as ${s.from_name}` : ''))),
      h('dl', {class: 'cc-kv'}, line('To', s.to), line('Cc', s.cc), line('Bcc', s.bcc),
        h('dt', null, 'Subject'), h('dd', null, s.subject || '(no subject)'),
        h('dt', null, 'Attachments'), h('dd', null, s.attachments ? String(s.attachments) : 'none'),
        h('dt', null, 'Text'), h('dd', null, `${fmt(s.characters)} characters` + (s.reply ? ' · a reply, in its thread' : ''))),
      c.warnings.length ? h('div', {class: 'cwarns'}, c.warnings.map(w => h('div', {class: 'cwarn ' + w.level}, h('span', {class: 'cw-i', 'aria-hidden': 'true'}, w.level === 'warn' ? '!' : 'i'), w.text))) : null,
      msg,
      h('div', {class: 'cc-actions'}, yes, back),
      h('div', {class: 'small muted'}, 'Every mail is confirmed like this. The confirmation is for this exact mail and lasts five minutes; any change asks again.'));
    const close = () => { dlg.close(); dlg.remove(); sendBtn.focus(); };
    yes.addEventListener('click', async () => {
      yes.disabled = back.disabled = true;
      fill(yes, 'Sending…');
      let r;
      try { r = await post(`/api/drafts/${d.id}/send`, {...fields(), token: c.token}); }
      catch (e) {
        fill(msg, errText(e));
        fill(yes, 'Not sent');
        back.disabled = false;
        back.focus();
        return;
      }
      dlg.close(); dlg.remove();
      refreshDraftCount();
      openPane(sentView(r, s.account_id), {key: 'sent:' + d.id, title: 'Sent', label: r.subject, replace: true});
      flash(`Sent from ${r.from_addr}.`);
    });
    document.body.append(dlg);
    dlg.showModal();
    back.focus();
    // A moment to read before "Yes" can be pressed (the server refuses a faster one).
    setTimeout(() => { if (dlg.open) yes.disabled = false; }, Math.max(1000, (c.wait || 1) * 1000 + 100));
  }

  drawFrom(); drawSig(); drawWarn(); drawSend();
  fill(status, 'Saved in Talos');
  add(root, [
    h('div', {class: 'meta', style: 'margin:0 0 8px'}, COMPOSE_TITLE[d.mode] || 'New email', helpBtn('compose'),
      v.original ? h('button', {class: 'chipbtn', onclick: () => openMessage(v.original.id), title: 'Open the original'},
        (v.original.from_name || v.original.from_address || '') + ' · ' + (v.original.subject || '(no subject)')) : null),
    fromBox,
    h('div', {class: 'cgrid'}, to.row, h('span'), h('div', {class: 'rrow'}, ccToggle)),
    ccRows,
    h('div', {class: 'cgrid'}, h('label', {class: 'flabel', for: 'c-subject'}, 'Subject'), subject),
    body,
    sigBox,
    quote,
    warnBox,
    err,
    h('div', {class: 'dactions csend-row'}, sendBtn, h('button', {class: 'btn ghost', onclick: discard}, 'Discard'), status)]);
  root.addEventListener('focusout', e => { if (dirty && !root.contains(e.relatedTarget)) save(); });
  return root;
}

function sentView(r, account) {
  return h('div', {class: 'compose sent', style: `--acct:${acctColor(account)}`},
    h('div', {class: 'cfrom'}, h('div', {class: 'cfrom-l'}, 'Sent from'), h('div', {class: 'cfrom-big'}, h('span', {class: 'cfrom-dot', 'aria-hidden': 'true'}), h('div', {class: 'cfrom-addr'}, r.from_addr))),
    h('h2', null, r.subject || '(no subject)'),
    h('dl', {class: 'cc-kv'}, [['To', r.to], ['Cc', r.cc], ['Bcc', r.bcc]].filter(([, l]) => l.length).map(([k, l]) => [h('dt', null, k), h('dd', null, l.join(', '))]),
      h('dt', null, 'Message-ID'), h('dd', {class: 'mono small'}, r.message_id)),
    note('The copy lands in Sent on the server and comes into Talos with the next sync.'),
    h('div', {class: 'dactions'}, h('button', {class: 'btn primary', onclick: newEmail}, '+ New email'), h('button', {class: 'btn ghost', onclick: () => closePane()}, 'Close')));
}

// ---- Sending settings: the default account, the name recipients see, the accounts that can send,
// the signatures and the send log. Opened from Messages and from the compose pane.
function openSendSettings() {
  return paneLoad({key: 'sendset', title: 'Sending and signatures'}, async () => {
    const [st, sg] = await Promise.all([fetchJSON('/api/compose/senders'), fetchJSON('/api/compose/signatures')]);
    return {node: sendSettings(st, sg), label: 'Sending and signatures'};
  });
}
function sendSettings(st, sg, flashText) {
  const reload = async text => {
    const [a, b] = await Promise.all([fetchJSON('/api/compose/senders'), fetchJSON('/api/compose/signatures')]);
    openPane(sendSettings(a, b, text), {key: 'sendset', title: 'Sending and signatures', replace: true});
  };
  const ready = st.senders.filter(s => s.pickable);
  const setDefault = async id => { try { await post('/api/compose/settings', {default_account: id}); } catch (e) { flash(errText(e)); return; } reload(id ? `New mail starts from ${acctName(id)}.` : 'No default: every new mail starts with the choice.'); };
  const nameIn = h('input', {class: 'field', id: 's-name', value: st.from_name_set || '', placeholder: st.from_name || 'Your name', maxlength: '120'});
  const sigList = sg.signatures;
  return h('div', {class: 'sendset'},
    h('h2', null, 'Sending and signatures'),
    flashText ? h('div', {class: 'small', role: 'status', style: 'color:var(--good-ink)'}, flashText) : null,
    h('div', {class: 'dsec'}, h('h4', null, 'Default account for new mail'),
      h('div', {class: 'small muted', style: 'margin-bottom:8px'}, 'Even with a default, every mail shows the From block, and nothing is sent until you confirm the account.'),
      h('div', {class: 'cpicks', role: 'radiogroup'},
        ready.map(s => h('button', {class: 'cpick' + (st.default === s.id ? ' on' : ''), role: 'radio', 'aria-checked': String(st.default === s.id), style: `--acct:${acctColor(s.id)}`,
            'aria-label': `${s.name}, ${s.address}`, onclick: () => setDefault(s.id)},
          h('span', {class: 'adot', 'aria-hidden': 'true'}), h('span', {class: 'cp-t'}, h('b', null, s.name), h('span', {class: 'cp-a'}, s.address)))),
        h('button', {class: 'cpick' + (!st.default ? ' on' : ''), role: 'radio', 'aria-checked': String(!st.default), 'aria-label': 'No default: choose every time', onclick: () => setDefault(null)},
          h('span', {class: 'cp-t'}, h('b', null, 'No default'), h('span', {class: 'cp-a'}, 'Choose every time'))))),
    h('div', {class: 'dsec'}, h('h4', null, 'Your name, as recipients see it'),
      h('div', {class: 'rrow'}, nameIn, h('button', {class: 'btn', onclick: async () => { try { await post('/api/compose/settings', {from_name: nameIn.value}); } catch (e) { flash(errText(e)); return; } reload('Name saved.'); }}, 'Save')),
      h('div', {class: 'small muted', style: 'margin-top:4px'}, st.from_name_set ? '' : `Empty: the name on most of your sent mail${st.from_name ? ` (${st.from_name})` : ''}.`)),
    h('div', {class: 'dsec'}, h('h4', null, 'Accounts'),
      st.senders.map(s => h('div', {class: 'csender', style: `--acct:${acctColor(s.id)}`},
        h('div', {class: 'rrow'}, h('span', {class: 'acct-dot', style: `background:${acctColor(s.id)}`}), h('b', null, s.name), h('span', {class: 'small muted'}, s.address),
          h('span', {class: 'pill ' + (s.pickable ? 'ok' : 'md')}, s.pickable ? 'can send' : s.status)),
        s.pickable ? null : h('div', {class: 'small', style: 'margin-top:4px'}, s.why || '', h('div', {class: 'mono small', style: 'margin-top:4px;overflow-wrap:anywhere'}, s.how || ''))))),
    h('div', {class: 'dsec'}, h('div', {class: 'rrow', style: 'justify-content:space-between;margin-bottom:8px'}, h('h4', {style: 'margin:0'}, `Signatures · ${sigList.length}`),
        h('button', {class: 'btn sm', onclick: () => openPane(signatureForm(null, st.senders, reload), {key: 'sig:new', title: 'New signature'})}, '+ New signature')),
      sigList.length ? sigList.map(x => h('div', {class: 'csigrow'},
        h('div', {class: 'rrow', style: 'justify-content:space-between'},
          h('b', null, x.name),
          h('span', {class: 'rrow'},
            h('button', {class: 'btn sm', onclick: () => openPane(signatureForm(x, st.senders, reload), {key: 'sig:' + x.id, title: 'Signature', label: x.name})}, 'Edit'),
            h('button', {class: 'btn ghost sm', onclick: async () => { if (!confirm(`Remove the signature “${x.name}”?`)) return; try { await post(`/api/compose/signatures/${x.id}/remove`, {}); } catch (e) { flash(errText(e)); return; } reload('Signature removed.'); }}, 'Remove'))),
        h('div', {class: 'rrow small', style: 'margin:4px 0'},
          x.accounts.map(a => h('span', {class: 'pill', style: `--acct:${acctColor(a)}`}, acctDot(a), acctName(a))),
          h('span', {class: 'muted'}, [x.for_new ? 'new mail' : null, x.for_reply ? 'replies and forwards' : null].filter(Boolean).join(' and ')),
          x.defaults.length ? h('span', {class: 'pill ok'}, 'default: ' + x.defaults.map(k => { const [a, m] = k.split(':'); return `${acctName(a)} ${m === 'new' ? 'new' : 'replies'}`; }).join(', ')) : null,
          x.body_html ? h('span', {class: 'pill'}, 'formatted') : null),
        h('div', {class: 'csig-t'}, x.body_text || '(formatted only)'))) : empty('No signatures yet', 'Make one for each account, for new mail and for replies.')),
    h('div', {class: 'dsec'}, h('h4', null, 'Send log · the latest 20'),
      sg.send_log.length ? h('table', {class: 't'}, h('tbody', null, sg.send_log.map(l => h('tr', null,
        h('td', {class: 'muted', style: 'white-space:nowrap'}, new Date(l.at).toLocaleString('sv-SE', {dateStyle: 'short', timeStyle: 'short'})),
        h('td', null, l.account_id ? acctDot(l.account_id) : null, ' ', l.subject || '(no subject)', h('div', {class: 'small muted'}, (l.to_addrs || []).concat(l.cc_addrs || []).join(', ') || '—')),
        h('td', {class: 'r'}, h('span', {class: 'pill ' + (l.result === 'sent' ? 'ok' : l.result === 'failed' ? 'hi' : 'md'), title: l.detail || ''}, l.result)))))) :
        h('div', {class: 'small muted'}, 'Nothing sent from Talos yet. The log keeps who, when, from, to, subject and the Message-ID, never the text.')));
}
function signatureForm(x, senders, reload) {
  const v = x || {name: '', body_text: '', body_html: '', accounts: [], for_new: true, for_reply: true, defaults: []};
  const name = h('input', {class: 'field', id: 'sig-name', value: v.name, maxlength: '120'});
  const text = h('textarea', {class: 'field', id: 'sig-text', rows: '6', placeholder: 'Alex Lind\nIT'}, v.body_text || '');
  const html = h('textarea', {class: 'field mono', id: 'sig-html', rows: '4', placeholder: '<b>Alex Lind</b><br>IT'}, v.body_html || '');
  const accts = senders.map(s => ({s, box: h('input', {type: 'checkbox', class: 'switch', 'aria-label': `Applies to ${s.name}`, checked: v.accounts.includes(s.id)}),
    def: h('input', {type: 'checkbox', class: 'switch', 'aria-label': `The default for ${s.name}`, checked: v.defaults.some(k => k.startsWith(s.id + ':'))})}));
  const forNew = h('input', {type: 'checkbox', class: 'switch', id: 'sig-new', checked: v.for_new});
  const forReply = h('input', {type: 'checkbox', class: 'switch', id: 'sig-reply', checked: v.for_reply});
  const err = h('div', {class: 'err', role: 'alert'});
  const save = async () => {
    fill(err);
    const accounts = accts.filter(a => a.box.checked).map(a => a.s.id);
    const payload = {name: name.value, body_text: text.value, body_html: html.value.trim() || null, accounts, for_new: forNew.checked, for_reply: forReply.checked,
                     default_for: accts.filter(a => a.box.checked && a.def.checked).map(a => a.s.id)};
    try { await post(x ? `/api/compose/signatures/${x.id}` : '/api/compose/signatures', payload); }
    catch (e) { fill(err, errText(e)); return; }
    reload(x ? 'Signature saved.' : 'Signature added.');
  };
  return h('div', {class: 'sendset'},
    h('h2', null, x ? x.name : 'New signature'),
    h('div', {class: 'wform sigform'},
      h('label', {class: 'flabel', for: 'sig-name'}, 'Name'), name,
      h('label', {class: 'flabel', for: 'sig-text'}, 'Text'), text,
      h('label', {class: 'flabel', for: 'sig-html'}, 'Formatted (optional)'), h('div', null, html, h('div', {class: 'small muted', style: 'margin-top:4px'}, 'Simple HTML: bold, links, a logo over https. It is cleaned when saved (no scripts, no forms) and sent as an HTML part beside the text.')),
      h('span', {class: 'flabel'}, 'Use for'), h('div', {class: 'rrow'}, h('label', {class: 'rrow', for: 'sig-new'}, forNew, 'New mail'), h('label', {class: 'rrow', for: 'sig-reply'}, forReply, 'Replies and forwards')),
      h('span', {class: 'flabel'}, 'Accounts'),
      h('table', {class: 't sigacc'}, h('thead', null, h('tr', null, h('th', null, 'Account'), h('th', null, 'Applies'), h('th', {title: 'The one a mail starts with, for each kind above'}, 'Default'))),
        h('tbody', null, accts.map(a => h('tr', null, h('td', null, acctDot(a.s.id), ' ', a.s.name, h('span', {class: 'small muted'}, ' ' + a.s.address)), h('td', null, a.box), h('td', null, a.def)))))),
    err,
    h('div', {class: 'dactions'}, h('button', {class: 'btn primary', onclick: save}, x ? 'Save' : 'Add'), h('button', {class: 'btn ghost', onclick: () => openSendSettings()}, 'Cancel')));
}

// ---------------------------------------------------------------- events (Discover › Events)
// Events come as one long list; the page shows them 50 at a time, filtered by status and kind.
const EVT = {status: '', kind: '', shown: 50};
async function viewEvents() {
  const rows = await api('/api/events');
  const kinds = [...new Set(rows.map(r => r.kind))].sort();
  const count = st => rows.filter(r => r.status === st && (!EVT.kind || r.kind === EVT.kind)).length;
  const sel = rows.filter(r => (!EVT.status || r.status === EVT.status) && (!EVT.kind || r.kind === EVT.kind));
  const shown = sel.slice(0, EVT.shown);
  const set = patch => { Object.assign(EVT, patch, {shown: 50}); render(true); };
  const kindSel = h('select', {class: 'sel fc-sel' + (EVT.kind ? ' on' : ''), 'aria-label': 'Kind', onchange: e => set({kind: e.target.value})},
    h('option', {value: ''}, 'Every kind'), kinds.map(k => h('option', {value: k}, k)));
  kindSel.value = EVT.kind;
  return h('div', null, header(withHelp('Events', 'events'), 'Machine mail read as status: backups, alarms, alerts'),
    h('p', {class: 'lead'}, rows.length ? `${rows.filter(r => r.status === 'failed').length} of the latest ${rows.length} events are failures.` : 'No events yet.'),
    rows.length ? [
      h('div', {class: 'fchips', style: 'margin-top:14px'},
        seg([['', 'All'], ...['failed', 'warning', 'ok', 'info'].map(st => [st, `${st} · ${fmt(count(st))}`])], EVT.status, v => set({status: v})), kindSel),
      h('section', {class: 'card'}, h('div', {class: 'tw'}, h('table', {class: 't'},
        h('thead', null, h('tr', null, h('th', null, 'When'), h('th', null, 'Kind'), h('th', null, 'Status'), h('th', null, 'System'), h('th', null, 'Subject'))),
        h('tbody', null, shown.map(r => h('tr', {class: 'click', tabindex: '0', 'data-pane-key': 'm:' + r.message_id,
            onclick: () => openMessage(r.message_id), onkeydown: e => { if (e.key === 'Enter') openMessage(r.message_id); }},
          h('td', {class: 'num'}, when(r.occurred_at)), h('td', null, r.kind),
          h('td', null, h('span', {class: 'pill ' + (r.status === 'failed' ? 'hi' : r.status === 'warning' ? 'md' : r.status === 'ok' ? 'ok' : '')}, r.status)),
          h('td', null, r.system || '—'), h('td', null, r.subject)))))),
        sel.length > shown.length ? h('div', {class: 'more'}, h('button', {class: 'btn', onclick: () => { EVT.shown += 50; render(true); }}, `Show 50 more (${fmt(sel.length - shown.length)} left)`))
          : h('div', {class: 'small muted', style: 'padding:10px 8px 0'}, `${fmt(sel.length)} ${sel.length === 1 ? 'event' : 'events'}`))]
      : card('How events appear', null, null, note('Run  talos events run  after a sync. The extractors are in src/talos/events.py and are tuned on real mail.')));
}

// The Rules view keeps the picked rule and the last run's outcome across its own redraws.
const RULES = {sel: null, msg: null, pending: false};
const actionText = a => a.dimension ? `${a.dimension}: ${a.value}` : `object ${a.object}`;

// Save the rule, then re-run every rule so its values (and every count) follow at once.
async function saveAndRun(body, msg) {
  let saved;
  try { saved = await post('/api/rules', body); } catch (e) { msg.replaceChildren(h('span', {class: 'err'}, errText(e))); return; }
  const r = saved.rule;
  msg.replaceChildren(h('span', {class: 'muted'}, `Saved ${r.id}@${r.version}. Re-running the rules…`));
  RULES.sel = r.id;
  await rerun(saved.created ? `Created ${r.id}@${r.version}.` : saved.new_version ? `Saved ${r.id} as version ${r.version}.` : `Saved ${r.id} (same version: only the name, description or priority changed).`);
}
async function rerun(prefix) {
  try {
    const res = await post('/api/rules/run', {});
    RULES.msg = `${prefix ? prefix + ' ' : ''}Re-ran ${fmt(res.rules)} rules: ${fmt(res.assigned)} values in ${res.seconds} s.`;
    RULES.pending = false;
  } catch (e) { RULES.msg = `${prefix ? prefix + ' ' : ''}The re-run failed: ${errText(e)}`; }
  await render();
  scrollTo(0, 0);  // the outcome is noted under the header, above the rule it concerns
}

// The form behind Edit and Save as rule. rule is null for a new one; conds is the JSON text.
async function ruleForm(rule, conds, back) {
  let dims, objs;
  try { [dims, objs] = await Promise.all([api('/api/dimensions'), api('/api/objects')]); } catch (e) { return h('div', {class: 'err'}, errText(e)); }
  const a = rule ? rule.action : {dimension: 'type', value: ''};
  const name = h('input', {class: 'field', id: 'rule-name', value: rule ? rule.name : '', maxlength: '200', placeholder: 'For example: Nordvik is a customer'});
  const rid = h('input', {class: 'field mono', id: 'rule-id', value: rule ? rule.id : '', maxlength: '80', placeholder: 'made from the name', disabled: !!rule});
  const desc = h('textarea', {class: 'field', id: 'rule-desc', rows: '2', placeholder: 'Where it came from, and why'}, rule && rule.description || '');
  const prio = h('input', {class: 'field', id: 'rule-prio', type: 'number', step: '1', value: rule ? rule.priority : 100});
  const box = h('textarea', {class: 'json', id: 'rule-conds', 'aria-label': 'Conditions as JSON'}, conds);
  const kind = {v: a.object != null ? 'object' : 'dimension'};
  const dimSel = h('select', {class: 'sel', 'aria-label': 'Dimension', onchange: () => valueBox()}, dims.map(d => h('option', {value: d.id}, `${d.label} (${d.id})`)));
  dimSel.value = a.dimension || 'type';
  const valueWrap = h('span');
  let valueEl;
  const valueBox = (v) => {
    const d = dims.find(x => x.id === dimSel.value) || {values: []};
    const cur = v != null ? v : valueEl ? valueEl.value : '';
    valueEl = Array.isArray(d.allowed) && d.allowed.length
      ? h('select', {class: 'sel', 'aria-label': 'Value'}, d.allowed.map(x => h('option', {value: x}, x)))
      : h('input', {class: 'field', 'aria-label': 'Value', list: 'rule-values', placeholder: 'value', style: 'width:220px'});
    valueEl.value = cur;
    fill(valueWrap, valueEl, h('datalist', {id: 'rule-values'}, (d.values || []).map(x => h('option', {value: x}))));
  };
  valueBox(a.value || '');
  const objSel = h('select', {class: 'sel', 'aria-label': 'Object'}, objs.map(o => h('option', {value: o.id}, `${o.name} · ${KIND_LABEL[o.kind] || o.kind}`)));
  if (a.object != null) objSel.value = String(a.object);
  const actionRow = h('div', {class: 'rrow'});
  const drawAction = () => fill(actionRow,
    seg([['dimension', 'Set a value'], ['object', 'Add to an object']], kind.v, v => { kind.v = v; drawAction(); }),
    kind.v === 'dimension' ? [dimSel, valueWrap] : objs.length ? objSel : h('span', {class: 'small muted'}, 'No objects yet.'));
  drawAction();
  const msg = h('div', {class: 'small', role: 'status'});
  const body = () => {
    let conditions;
    try { conditions = JSON.parse(box.value); } catch (e) { throw new Error('The conditions are not valid JSON: ' + e.message); }
    return {id: rid.value.trim() || undefined, new: !rule, name: name.value, description: desc.value, priority: prio.value === '' ? undefined : Number(prio.value),
            conditions, enabled: rule ? rule.enabled : true,
            action: kind.v === 'object' ? {object: objSel.value} : {dimension: dimSel.value, value: valueEl.value}};
  };
  const save = async () => {
    let b;
    try { b = body(); } catch (e) { msg.replaceChildren(h('span', {class: 'err'}, e.message)); return; }
    msg.replaceChildren(h('span', {class: 'muted'}, 'Saving…'));
    await saveAndRun(b, msg);
  };
  return h('div', {class: 'rform'},
    h('div', {class: 'small muted'}, rule ? `Editing ${rule.id}@${rule.version}. A change to the conditions or the action saves version ${rule.version + 1}; the rules are then re-run.` :
      'A new rule. Saving re-runs every rule, so the values and counts follow at once.'),
    h('label', {class: 'flabel', for: 'rule-name'}, 'Name'), name,
    h('div', {class: 'rrow'}, h('div', {style: 'flex:1'}, h('label', {class: 'flabel', for: 'rule-id'}, 'Id'), rid),
      h('div', {style: 'width:110px'}, h('label', {class: 'flabel', for: 'rule-prio', title: 'Lower runs first; for a one-value dimension the first match wins'}, 'Priority'), prio)),
    h('label', {class: 'flabel', for: 'rule-desc'}, 'Description'), desc,
    h('label', {class: 'flabel', for: 'rule-conds'}, 'Conditions (all must hold)'), box,
    h('span', {class: 'flabel'}, 'Action'), actionRow,
    h('div', {class: 'rrow', style: 'margin-top:6px'},
      h('button', {class: 'btn sm primary', onclick: save}, rule ? 'Save and re-run' : 'Create and re-run'),
      h('button', {class: 'btn ghost sm', onclick: back}, 'Cancel')),
    msg);
}

// ---- Removing a rule. Every saved rule, and every rule made from a suggestion, has a Remove that asks
// first, inline: its values go at once (each message falls back to its next source), the rules are
// re-run so the others fill in, and the definition is kept in the history with the why. A rule made
// from suggestions makes them suggestions again.
const madeFromSuggestions = r => !!(r.origin && (r.origin.suggestion || r.origin.group)) || String(r.id).startsWith('suggest-');
function removeConfirm(r, onCancel) {
  const why = h('input', {class: 'field', placeholder: 'Why (optional, kept in the history)', maxlength: '300', 'aria-label': 'Why remove it'});
  const msg = h('span', {class: 'small', role: 'status'});
  const go = h('button', {class: 'btn sm danger'}, 'Remove');
  const box = h('div', {class: 'rm-confirm', role: 'group', 'aria-label': `Remove ${r.name}`},
    h('div', {class: 'small'}, h('b', null, `Remove “${r.name}”?`), ' ',
      `The values it set go now${r.hits ? ` (${fmt(r.hits)})` : ''}; each message falls back to its next source (another rule after the re-run, or the accepted value). ` +
      (madeFromSuggestions(r) ? 'Its suggestions come back under Suggested rules. ' : '') + 'The rule is kept under Removed rules.'),
    h('div', {class: 'rrow'}, why, go, h('button', {class: 'btn sm ghost', onclick: () => onCancel(box)}, 'Cancel'), msg));
  go.addEventListener('click', async () => {
    go.disabled = true; fill(go, 'Removing…');
    try {
      const res = await post(`/api/rules/${encodeURIComponent(r.id)}/remove`, {why: why.value});
      if (RULES.sel === r.id) RULES.sel = null;
      await rerun(`Removed ${r.id}@${res.version} (${fmt(res.values)} values, ${fmt(res.edges)} memberships).` +
        (madeFromSuggestions(r) ? ' Its suggestions are back under Suggested rules.' : ''));
    } catch (e) { fill(msg, h('span', {class: 'err'}, errText(e))); go.disabled = false; fill(go, 'Remove'); }
  });
  setTimeout(() => why.focus(), 0);
  return box;
}
function removedCard() {
  const body = h('div', {class: 'small muted'}, 'Loading…');
  const box = h('details', {class: 'rm-hist'}, h('summary', null, 'Removed rules'), body);
  box.addEventListener('toggle', async () => {
    if (!box.open || body.dataset.done) return;
    body.dataset.done = '1';
    let rows;
    try { rows = await api('/api/rules/removed'); } catch (e) { fill(body, h('span', {class: 'err'}, errText(e))); return; }
    fill(body, rows.length ? h('table', {class: 't'}, h('tbody', null, rows.map(x => h('tr', null,
      h('td', {class: 'nm'}, x.definition.name, h('small', null, `${x.rule_id}@${x.definition.version} · ${actionText(x.definition.action)}`),
        x.why ? h('small', {class: 'desc'}, `Why: ${x.why}`) : null),
      h('td', {class: 'r muted'}, when(x.removed_at)))))) : 'No rule has been removed yet.');
  });
  return box;
}

// ---- Suggested rules: drafted by the server (talos.suggest) from the accepted values, as a drill-down:
// field (Kind, Topic, …) → category (Topic › Work; for a field with few values the category is the
// value: Kind › Alert) → value (Work/Backup) → the suggestions (a sender, or a sender and a subject
// pattern). Each level shows its suggestions and the messages they would cover. Ticking a value makes
// ONE broader rule from all its suggestions (a category of several values: one rule per value); the
// server says first what it would match and how many of those messages have another accepted value.
// Every rule is saved off. The search box filters by sender or value on the server.
const SUG_PAGE = 25;
const SUG = {open: new Set(), q: '', sort: 'coverage'};
function suggestionsCard() {
  const body = h('div', null, h('div', {class: 'muted small'}, 'Drafting suggestions from the accepted values… (a few seconds the first time)'));
  loadSuggestionTree(body);
  return card('Suggested rules', 'Drafted from the values you gave and the ones accepted from Jev: where at least 20 messages from one sender (or one sender\'s subject pattern) share a value, and none has another. Open a field, a category and a value; tick a category or a value to make one rule from all of its suggestions.', null, body);
}
async function loadSuggestionTree(body) {
  const stat = h('span', {class: 'small muted'});
  const search = h('input', {class: 'field sug-q', type: 'search', value: SUG.q, placeholder: 'Filter by sender or value', 'aria-label': 'Filter the suggestions by sender or value'});
  const sortSel = h('select', {class: 'sel', 'aria-label': 'Sort by'},
    [['coverage', 'Most messages first'], ['agree', 'Most agreeing first'], ['name', 'By name']].map(([v, l]) => h('option', {value: v}, l)));
  sortSel.value = SUG.sort;
  const tree = h('div', {class: 'sug-tree', role: 'tree', 'aria-label': 'Suggested rules'});
  fill(body, h('div', {class: 'sug-bar'}, search, sortSel, stat), tree);
  let res, seq = 0;
  const load = async () => {
    const mine = ++seq;
    try { res = await api('/api/rules/suggestions/tree' + (SUG.q ? '?' + new URLSearchParams({q: SUG.q}) : '')); }
    catch (e) { fill(tree, h('div', {class: 'err'}, errText(e))); return; }
    if (mine !== seq) return;
    fill(stat, SUG.q ? `${fmt(res.shown)} of ${fmt(res.total)} suggestions match` :
      `${fmt(res.total)} suggestions` + (res.computed_at ? ` · drafted ${when(res.computed_at)} in ${res.seconds} s` : ''));
    drawTree(tree, res);
  };
  let timer;
  search.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => { SUG.q = search.value.trim(); load(); }, 300); });
  sortSel.addEventListener('change', () => { SUG.sort = sortSel.value; if (res) drawTree(tree, res); });
  await load();
}
const sugSort = (xs, label) => [...xs].sort(SUG.sort === 'name' ? (a, b) => label(a).localeCompare(label(b), 'sv')
  : SUG.sort === 'agree' ? (a, b) => b.agree - a.agree : (a, b) => b.coverage - a.coverage);
const sugCounts = x => `${fmt(x.count)} suggestion${x.count === 1 ? '' : 's'} · ${fmt(x.coverage)} message${x.coverage === 1 ? '' : 's'}`;
function drawTree(box, res) {
  const fields = res.fields.filter(f => f.count || f.categories.some(c => c.values.some(v => v.rules.length)));
  if (!fields.length) {
    fill(box, empty(SUG.q ? 'Nothing matches' : 'No suggestions', SUG.q ? `No suggestion's sender or value holds “${SUG.q}”.` :
      `No sender has ${res.threshold} or more messages that agree on a value without a conflict, or each one is a rule already.`));
    return;
  }
  fill(box, fields.map(f => sugNode(1, 'f:' + f.dimension, f.label, f, null,
    kids => fill(kids, sugSort(f.categories, c => c.name).map(c => c.single
      ? valueNode(2, f, c, c.values[0], c.name)
      : sugNode(2, 'c:' + c.key, c.name, c, {category: c.key, label: `${f.label} › ${c.name}`},
          kids2 => fill(kids2, sugSort(c.values, v => v.label).map(v => valueNode(3, f, c, v, v.label)))))))));
}
// One row of the tree: a toggle, its counts, a tick (for a category or a value), and the rules made here.
function sugNode(level, key, label, x, tick, drawKids, extra) {
  const open = !!SUG.q || SUG.open.has(key);
  const kids = h('div', {class: 'sug-kids', role: 'group', hidden: !open});
  const panel = h('div', {class: 'sug-panel2'});
  const tog = h('button', {class: 'sug-tog', 'aria-expanded': String(open)}, h('span', {class: 'caret', 'aria-hidden': 'true'}), label);
  let drawn = false;
  const show = () => { if (!drawn) { drawn = true; drawKids(kids); } };
  tog.addEventListener('click', () => {
    const now = kids.hidden;
    kids.hidden = !now;
    tog.setAttribute('aria-expanded', String(now));
    if (now) { SUG.open.add(key); show(); } else SUG.open.delete(key);
  });
  if (open) show();
  const tickBtn = tick && x.count ? h('button', {class: 'btn sm sug-tick', title: 'Make one rule from all of these suggestions (saved off)',
    onclick: () => groupPreview(panel, tick, tickBtn)}, h('span', {class: 'tickbox', 'aria-hidden': 'true'}), 'One rule') : null;
  return h('div', {class: `sug-node l${level}`, role: 'treeitem', 'aria-expanded': String(open)},
    h('div', {class: 'sug-row'}, tog, h('span', {class: 'sug-n'}, sugCounts(x)), extra || null, tickBtn), panel, kids);
}
function valueNode(level, f, c, v, label) {
  const rulesHere = v.rules.length ? h('span', {class: 'sug-made'}, v.rules.map(r => madeRule(r))) : null;
  return sugNode(level, `v:${f.dimension}:${v.value}`, label, v, {dimension: f.dimension, value: v.value, label: `${f.label} › ${v.label}`},
    kids => v.suggestions.length ? fill(kids, h('div', {class: 'sug-list'}, sugSort(v.suggestions, x => x.sender).map(x => suggestionRow(x))))
      : loadValue(kids, c, v, 0), rulesHere);
}
// A rule already made from this value's suggestions: its name, on or off, and its Remove.
function madeRule(r) {
  const wrap = h('span', {class: 'made'});
  const rm = h('button', {class: 'linkbtn', title: 'Remove this rule; its suggestions come back'}, 'Remove');
  rm.addEventListener('click', () => {
    const row = wrap.closest('.sug-node');
    const panel = row.querySelector(':scope > .sug-panel2');
    fill(panel, removeConfirm({...r, origin: {suggestion: true}}, () => panel.replaceChildren()));
  });
  fill(wrap, h('span', {class: 'pill ' + (r.enabled ? 'ok' : ''), title: r.id},
    `${r.kind === 'group' ? `Rule from ${fmt(r.members)} suggestion${r.members === 1 ? '' : 's'}` : 'Rule'}: ${r.enabled ? 'on' : 'off'}`),
    h('button', {class: 'linkbtn', onclick: () => { RULES.sel = r.id; render(); scrollTo(0, 0); }}, 'Show'), rm);
  return wrap;
}
async function loadValue(kids, c, v, offset) {
  if (!offset) fill(kids, h('div', {class: 'muted small sug-loading'}, 'Counting what each rule would match…'));
  let res;
  try {
    res = await api('/api/rules/suggestions?' + new URLSearchParams({category: c.key, value: v.value, offset: String(offset),
      limit: String(SUG_PAGE), sort: SUG.sort === 'agree' ? 'agree' : 'coverage'}));
  } catch (e) { fill(kids, h('div', {class: 'err'}, errText(e))); return; }
  let list = kids.querySelector('.sug-list');
  if (!offset || !list) { list = h('div', {class: 'sug-list'}); fill(kids, list); }
  add(list, res.rows.map(r => suggestionRow({...r, coverage: r.preview})));
  const more = kids.querySelector('.sug-more');
  if (more) more.remove();
  const next = offset + res.rows.length;
  if (next < res.count) kids.append(h('button', {class: 'btn sm sug-more', onclick: e => { e.target.disabled = true; loadValue(kids, c, v, next); }},
    `Show ${fmt(Math.min(SUG_PAGE, res.count - next))} more of ${fmt(res.count)}`));
}
// Ticking: what it would make, then Create (off) or Cancel.
async function groupPreview(panel, tick, btn) {
  if (panel.childElementCount) { panel.replaceChildren(); btn.setAttribute('aria-pressed', 'false'); return; }
  btn.setAttribute('aria-pressed', 'true');
  fill(panel, h('div', {class: 'sug-confirm muted small'}, 'Counting what it would match, and the conflicts…'));
  const body = tick.category ? {category: tick.category} : {dimension: tick.dimension, value: tick.value};
  let res;
  try { res = await post('/api/rules/suggestions/group/preview', body, true); }
  catch (e) { fill(panel, h('div', {class: 'sug-confirm err'}, errText(e))); return; }
  const n = res.rules.length;
  const create = h('button', {class: 'btn sm primary'}, n === 1 ? (res.rules[0].exists ? 'Update the rule (off)' : 'Create the rule (off)') : `Create ${n} rules (off)`);
  const msg = h('span', {class: 'small', role: 'status'});
  create.addEventListener('click', async () => {
    create.disabled = true; fill(create, 'Creating…');
    try {
      const made = await post('/api/rules/suggestions/group', body);
      RULES.msg = `Created ${made.rules.map(r => `“${r.name}”`).join(', ')}, switched off. Switch ${made.rules.length === 1 ? 'it' : 'them'} on under Saved rules to use ${made.rules.length === 1 ? 'it' : 'them'}.`;
      RULES.sel = made.rules[0].id;
      await render(); scrollTo(0, 0);
    } catch (e) { fill(msg, h('span', {class: 'err'}, errText(e))); create.disabled = false; }
  });
  fill(panel, h('div', {class: 'sug-confirm'},
    h('div', {class: 'small'}, h('b', null, tick.label), n === 1 ? ': one rule, an OR over all its suggestions.' :
      `: ${n} rules, one per value (a rule sets one value), each an OR over that value's suggestions.`),
    h('ul', {class: 'sug-plan'}, res.rules.map(r => h('li', null,
      h('b', null, r.name), r.exists ? h('span', {class: 'muted'}, ' (adds to the existing rule: a new version)') : null, ' · ',
      `matches ${fmt(r.count)} message${r.count === 1 ? '' : 's'}`, ' · ',
      h('span', {class: r.conflicts ? 'warn' : 'muted'}, `${fmt(r.conflicts)} with another accepted value`),
      r.ruled ? h('span', {class: 'muted'}, ` · ${fmt(r.ruled)} set by another rule already`) : null,
      h('details', {class: 'sug-cond'}, h('summary', null, 'Conditions'), h('pre', {class: 'mono small'}, JSON.stringify(r.conditions, null, 2)))))),
    h('div', {class: 'rrow'}, create, h('button', {class: 'btn sm ghost', onclick: () => { panel.replaceChildren(); btn.setAttribute('aria-pressed', 'false'); }}, 'Cancel'), msg)));
}
function suggestionRow(r) {
  const act = h('div', {class: 'sug-a'});
  const addBtn = h('button', {class: 'btn sm', title: 'Save it as a rule, switched off'}, 'Add as rule (off)');
  addBtn.addEventListener('click', async () => {
    addBtn.disabled = true; fill(addBtn, 'Adding…');
    try {
      const res = await post('/api/rules/suggestions/add', {id: r.id});
      const rule = {...res.rule, hits: 0};
      const again = h('button', {class: 'linkbtn', title: 'Remove the rule; it becomes a suggestion again'}, 'Remove');
      again.addEventListener('click', () => fill(act, removeConfirm(rule, () => fill(act, added))));
      const added = [h('span', {class: 'pill'}, 'Added, off'),
        h('button', {class: 'linkbtn', onclick: () => { RULES.sel = rule.id; render(); scrollTo(0, 0); }}, 'Show in Saved rules ›'), again];
      fill(act, added);
      flash(`Added ${rule.id}, switched off. Switch it on under Saved rules to use it; Remove takes it back.`);
    } catch (e) { fill(addBtn, 'Add as rule (off)'); addBtn.disabled = false; flash(errText(e)); }
  });
  act.append(addBtn);
  const cover = r.coverage != null ? r.coverage : r.preview;
  return h('div', {class: 'sug'},
    h('div', {class: 'sug-m'},
      h('div', {class: 'sug-t', title: r.conditions ? 'Conditions: ' + JSON.stringify(r.conditions) : null},
        h('b', null, r.sender), r.skeleton ? h('span', {class: 'pill mono', title: 'Only subjects like this (its subject pattern)'}, `“${r.skeleton}…”`) : null),
      h('div', {class: 'small sec'}, `${fmt(r.agree)} messages agree and none disagrees · the rule would match ${fmt(cover)} message${cover === 1 ? '' : 's'}`),
      r.examples && r.examples.length ? h('ul', {class: 'sug-ex'}, r.examples.map(e => h('li', null,
        h('button', {class: 'linkbtn', title: 'Open the message', onclick: () => openMessage(e.id)}, e.subject || '(no subject)')))) : null),
    act);
}

// ---------------------------------------------------------------- rules (Tune › Rules)
// The rules as data (talos.rules): each with its conditions, what it sets and how much it matches;
// switch one off, remove it, run them all, or write a new one; the suggestions sit below.
async function viewRules() {
  const rows = await api('/api/rules');
  const box = h('textarea', {class: 'json', id: 'cond', 'aria-label': 'Conditions as JSON'}, '[\n  {"field": "from_domain", "op": "is", "value": "klarna.com"}\n]');
  const out = h('div', {style: 'margin-top:12px'});
  const run = async () => {
    out.replaceChildren(h('div', {class: 'muted small'}, 'Counting…'));
    let conditions;
    try { conditions = JSON.parse(box.value); } catch (e) { out.replaceChildren(h('div', {class: 'err'}, 'The conditions are not valid JSON: ' + e.message)); return; }
    try {
      const res = await post('/api/rules/preview', {conditions}, true);
      out.replaceChildren(h('div', null, h('b', null, `${fmt(res.count)} messages`)), h('table', {class: 't'}, h('tbody', null, res.sample.map(r => h('tr', {class: 'click', tabindex: '0', 'data-pane-key': 'm:' + r.id, onclick: () => openMessage(r.id), onkeydown: e => { if (e.key === 'Enter') openMessage(r.id); }}, h('td', null, day(r.received_at)), h('td', null, r.from_address), h('td', null, r.subject))))));
    } catch (e) { out.replaceChildren(h('div', {class: 'err'}, errText(e))); }
  };
  const title = h('div', {class: 'rtitle'});
  const tester = h('div', null, title, box, out);
  const panel = h('div', null, tester);
  const showTester = () => panel.replaceChildren(tester);
  const openForm = async (rule, conds) => {
    panel.replaceChildren(h('div', {class: 'muted small'}, 'Loading…'));
    panel.replaceChildren(await ruleForm(rule, conds, showTester));
    const first = document.getElementById(rule ? 'rule-desc' : 'rule-name');
    if (first) first.focus();
  };
  // Clicking a saved rule loads it into the tester and runs it, so every rule can be checked
  // against the archive the same way a new condition is; Edit opens it in the form.
  const pick = (r, tr) => {
    RULES.sel = r.id;
    tbody.querySelectorAll('tr.sel').forEach(x => x.classList.remove('sel'));
    if (tr) tr.classList.add('sel');
    box.value = JSON.stringify(r.conditions, null, 2);
    const confirmBox = h('div');
    fill(title, h('div', {class: 'rtitle-h'}, h('div', null, h('b', null, r.name), h('span', {class: 'muted small'}, ` · ${r.id}@${r.version} · ${actionText(r.action)}`)),
        h('div', {class: 'rrow', style: 'margin:0;flex-wrap:nowrap'},
          h('button', {class: 'btn sm', onclick: () => openForm(r, JSON.stringify(r.conditions, null, 2))}, 'Edit'),
          h('button', {class: 'btn sm danger-ghost', title: 'Remove this rule (asks first)',
            onclick: () => fill(confirmBox, removeConfirm(r, () => confirmBox.replaceChildren()))}, 'Remove'))),
      confirmBox,
      h('div', {class: 'small ' + (r.description ? 'sec' : 'muted')}, r.description || 'No description yet. Edit the rule to say where it came from.'));
    showTester();
    run();
  };
  const toggle = async (r, input, tr) => {
    input.disabled = true;
    try {
      const res = await post(`/api/rules/${encodeURIComponent(r.id)}/enabled`, {enabled: input.checked});
      r.enabled = res.enabled;
      tr.classList.toggle('off', !r.enabled);
      RULES.pending = true;
      rerunBtn.classList.add('primary');
      fill(rerunBtn, 'Re-run to apply');
    } catch (e) { input.checked = r.enabled; alert(errText(e)); }
    input.disabled = false;
  };
  // Each rule: click shows what it matches; the switch turns it on or off; Remove asks first, in a row below it.
  const tbody = h('tbody', null, rows.map(r => {
    const sw = h('input', {type: 'checkbox', class: 'switch', checked: r.enabled, title: r.enabled ? 'Enabled: click to switch off' : 'Off: click to switch on', 'aria-label': `Enabled: ${r.name}`,
      onclick: e => e.stopPropagation(), onkeydown: e => e.stopPropagation()});
    const rm = h('button', {class: 'iconbtn rm', title: `Remove ${r.name} (asks first)`, 'aria-label': `Remove ${r.name}`}, '×');
    const tr = h('tr', {class: 'click' + (r.enabled ? '' : ' off') + (RULES.sel === r.id ? ' sel' : ''), tabindex: '0', title: 'Show what this rule matches',
        onclick: e => pick(r, e.currentTarget), onkeydown: e => { if (e.key === 'Enter' && e.target === e.currentTarget) pick(r, e.currentTarget); }},
      h('td', {class: 'nm'}, r.name, h('small', null, `${r.id}@${r.version} · priority ${r.priority}` + (madeFromSuggestions(r) ? ' · from a suggestion' : '')),
        r.description ? h('small', {class: 'desc'}, r.description) : null),
      h('td', null, h('span', {class: 'pill ac'}, actionText(r.action))), h('td', {class: 'r'}, fmt(r.hits)), h('td', {class: 'r act'}, h('span', {class: 'act-in'}, sw, rm)));
    sw.addEventListener('change', () => toggle(r, sw, tr));
    rm.addEventListener('click', e => {
      e.stopPropagation();
      const next = tr.nextElementSibling;
      if (next && next.classList.contains('rm-row')) { next.remove(); return; }
      const row = h('tr', {class: 'rm-row'});
      fill(row, h('td', {colspan: '4'}, removeConfirm(r, () => row.remove())));
      tr.after(row);
    });
    rm.addEventListener('keydown', e => e.stopPropagation());
    return tr;
  }));
  const rerunBtn = h('button', {class: 'btn sm' + (RULES.pending ? ' primary' : ''), title: 'Recompute every rule-made value; your own decisions are never touched',
    onclick: async () => { rerunBtn.disabled = true; fill(rerunBtn, 'Running…'); await rerun(''); }}, RULES.pending ? 'Re-run to apply' : 'Re-run rules');
  const shown = RULES.msg; RULES.msg = null;
  const view = h('div', null, header(withHelp('Rules', 'rules'), 'The researched rules (TALOS_HOME/config/rules) and the ones you add; each can be switched off or removed', rerunBtn),
    h('p', {class: 'lead'}, 'Conditions as data, compiled to SQL. A re-run recomputes only rule-made values; your own decisions always win.'),
    shown ? h('div', {style: 'margin-top:12px'}, note(shown)) : null,
    h('div', {class: 'grid g2', style: 'margin-top:16px'},
      card('Saved rules', 'Click one to see what it matches; the switch turns it on or off; × removes it (asks first)', null,
        rows.length ? h('table', {class: 't'}, h('thead', null, h('tr', null, h('th', null, 'Rule'), h('th', null, 'Sets'), h('th', {class: 'r'}, 'Matches'), h('th', {class: 'r'}, 'On'))), tbody) :
          emptyPage('No rules yet', 'Write a condition on the right and save it as a rule, add one from the suggestions below, or load your rules file (TALOS_HOME/config/rules/…) with  talos rules load .'),
        removedCard()),
      card('Try a condition', 'Fields: from_address, from_domain, subject, label, folder, recipient, attachment_type, body (search), is_automated, header:<name>. All must hold; {"any": [a, [b, c]]} is an OR group, and op "in" takes a list',
        h('div', {class: 'head-r'},
          h('button', {class: 'btn sm', title: 'Make these conditions a new rule', onclick: () => openForm(null, box.value)}, 'Save as rule'),
          h('button', {class: 'btn sm primary', onclick: () => { showTester(); run(); }}, 'Preview')),
        panel)),
    suggestionsCard());
  const sel = RULES.sel && rows.find(r => r.id === RULES.sel);
  if (sel) pick(sel, [...tbody.children][rows.indexOf(sel)]);
  return view;
}

// ---------------------------------------------------------------- changesets
// Open changesets (draft, planned, committed, applying) lead; finished ones (done, failed, cancelled,
// undone) sit under History, collapsed. Each status has its colour: the good / warning / critical
// tokens, as pills with a mark, apart from the account colours (filled dots) and the object kinds
// (stripes). "undone" is not stored: the server shows a done changeset whose undo is done as undone.
const CS_STATUS = {
  draft: {word: 'draft', means: 'not planned yet'},
  planned: {word: 'planned', means: 'waiting for your review'},
  committed: {word: 'committed', means: 'approved, not applied'},
  applying: {word: 'applying', means: 'being written to the server'},
  done: {word: 'done', means: 'applied'},
  failed: {word: 'failed', means: 'some operations failed'},
  cancelled: {word: 'cancelled', means: 'stopped; nothing more happens'},
  undone: {word: 'undone', means: 'applied, then reversed'},
};
const CS_LEGEND = ['planned', 'committed', 'applying', 'done', 'failed', 'cancelled', 'undone'];
const CS = {history: false};
function csPill(st) {
  const x = CS_STATUS[st] || {word: st, means: ''};
  return h('span', {class: `pill cs cs-${st}`, title: x.means}, h('span', {class: 'cs-mark', 'aria-hidden': 'true'}), x.word);
}
function csLegend() {
  return h('div', {class: 'cs-legend', 'aria-label': 'What the statuses mean'},
    CS_LEGEND.map(st => h('div', null, csPill(st), h('span', {class: 'small sec'}, CS_STATUS[st].means))));
}
// A date as "26 Sep 20:14".
function csWhen(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return `${d.getDate()} ${['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][d.getMonth()]} ` +
    `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}
// What happens next, in plain words, for the top of a changeset's pane. cs is the detail: shown
// (the status to show), op_counts ({pending, done, skipped, failed}), summary, the dates, undo_of
// and undone_by.
function csNext(cs) {
  const n = cs.op_counts || {}, sum = cs.summary || {}, dr = sum.dry_run, id = cs.id;
  const msgs = k => `${k} message${k === 1 ? '' : 's'}`;
  const pending = n.pending || 0, done = n.done || 0, failed = n.failed || 0, skipped = n.skipped || 0;
  const back = cs.undo_of ? ` It reverses changeset #${cs.undo_of}.` : '';
  switch (cs.shown) {
    case 'draft':
      return `Draft: nothing is planned yet. Plan it (talos changeset plan ${id}) to see exactly which messages would change.${back}`;
    case 'planned':
      if (!pending) return `Planned, but nothing would change: every message it selected is already so, or gone. Cancel it (talos changeset cancel ${id}).${back}`;
      return (dr ? `Planned: the dry run ${csWhen(dr.at)} found ${dr.ok} ready${dr.problems ? ` and ${dr.problems} with a problem` : ''}. Commit to approve`
                 : 'Planned: dry-run it, then commit to approve') +
        ` (talos changeset commit ${id} --max ${pending || 1}). ${msgs(pending)} would change; nothing has been written yet.${back}`;
    case 'committed':
      return `Committed ${csWhen(cs.committed_at)}: approved, not applied. Apply writes ${msgs(pending)} to the server: talos changeset apply ${id}.${back}`;
    case 'applying':
      return `Applying: ${done} of ${done + pending + failed} done so far. If it stopped, talos changeset apply ${id} resumes it.${back}`;
    case 'done':
      return `Done ${csWhen(cs.finished_at)}: ${msgs(done)} changed${skipped ? `, ${skipped} skipped` : ''}.` +
        (cs.undone_by ? ` Its undo is changeset #${cs.undone_by} (${cs.undone_by_status}).` : cs.undo_of ? back :
          ` Undo creates a reversing changeset: talos changeset undo ${id}.`);
    case 'undone':
      return `Done ${csWhen(cs.finished_at)}: ${msgs(done)} changed, then undone by changeset #${cs.undone_by}. Nothing more happens.`;
    case 'failed':
      return `Failed ${csWhen(cs.finished_at)}: ${msgs(done)} changed, ${failed} failed (see the operations below).` +
        (cs.undone_by ? ` Its undo is changeset #${cs.undone_by} (${cs.undone_by_status}).` : ` Undo reverses the ones that worked: talos changeset undo ${id}.`) + back;
    case 'cancelled':
      return `Cancelled ${csWhen(cs.finished_at)}: nothing more happens.${done ? ` ${msgs(done)} had changed before.` : ' Nothing was written.'}${back}`;
    default:
      return `Status: ${cs.shown || cs.status}.`;
  }
}
// The undo links of a row or a pane: "undoes #1", "undone by #2"; each opens that changeset.
function csLinks(r, small) {
  const link = (id, text) => h('button', {class: 'linkbtn cs-link' + (small ? ' small' : ''), title: `Open changeset ${id}`,
    onclick: e => { e.stopPropagation(); openChangeset(id); }}, text);
  return [r.undo_of ? link(r.undo_of, `undoes #${r.undo_of}`) : null, r.undone_by ? link(r.undone_by, `undone by #${r.undone_by}`) : null];
}
function csTable(rows, history) {
  return h('div', {class: 'tw'}, h('table', {class: 't'},
    h('thead', null, h('tr', null, h('th', null, '#'), h('th', null, 'Title'), h('th', null, 'Status'),
      h('th', {class: 'r'}, history ? 'Changed' : 'Will change'), h('th', null, history ? 'Finished' : 'Dry run'), h('th', {class: 'r'}, 'Created'))),
    h('tbody', null, rows.map(r => {
      const dr = (r.summary || {}).dry_run, ops = r.ops || {};
      return h('tr', {class: 'click', tabindex: '0', 'data-pane-key': 'c:' + r.id, onclick: () => openChangeset(r.id), onkeydown: e => { if (e.key === 'Enter') openChangeset(r.id); }},
        h('td', {class: 'num'}, r.id), h('td', {class: 'nm'}, r.title, h('small', {class: 'cs-links'}, csLinks(r, true))), h('td', null, csPill(r.shown)),
        h('td', {class: 'r'}, history ? fmt(ops.done) : fmt(ops.pending != null ? ops.pending : (r.summary || {}).will_change)),
        history ? h('td', {class: 'muted small'}, r.finished_at ? csWhen(r.finished_at) : '—') :
          h('td', null, dr ? h('span', {class: 'pill ' + (dr.problems ? 'md' : 'ok')}, dr.problems ? `${dr.problems} problem${dr.problems > 1 ? 's' : ''}` : `${dr.ok} ready`) : h('span', {class: 'muted'}, '—')),
        h('td', {class: 'r muted'}, when(r.created_at)));
    }))));
}
async function viewChangesets() {
  const rows = await api('/api/changesets');
  const open = rows.filter(r => r.group === 'open'), past = rows.filter(r => r.group === 'history');
  const hist = h('details', {class: 'cs-history', open: CS.history, ontoggle: e => { CS.history = e.target.open; }},
    h('summary', null, h('span', {class: 'cs-hist-h'}, `History · ${fmt(past.length)}`), h('span', {class: 'small muted'}, ' done, failed, cancelled and undone')),
    past.length ? csTable(past, true) : empty('No history yet', 'Applied, cancelled and undone changesets are kept here.'));
  return h('div', null, header(withHelp('Changesets', 'changesets'), 'Batches of mailbox changes: plan → dry run → commit → apply → undo'),
    h('p', {class: 'lead'}, 'Created, committed and applied from the command line; here you read the plan, dry-run it and follow each step. A dry run reads the mailbox and sends nothing. A commit is refused over 1 message unless raised on purpose (talos changeset commit ID --max N). After apply, talos changeset check ID asks the server where the messages are now.'),
    rows.length ? h('div', {class: 'cs-layout'},
      h('div', null,
        card('Open', open.length ? `${fmt(open.length)} waiting for you or running` : 'Nothing is waiting for you', null,
          open.length ? csTable(open, false) : empty('Nothing open', 'Every changeset is finished. New ones appear here, planned, for your review.')),
        h('section', {class: 'card', style: 'margin-top:16px'}, hist)),
      h('aside', {class: 'card cs-aside'}, h('h2', null, 'Statuses'), csLegend())) :
      h('section', {class: 'card', style: 'margin-top:16px'},
        emptyPage('No changesets yet', 'Example: talos changeset create --title "Label the test mail" --op add_label --args \'{"label":"Talos/test"}\' --ids 123')));
}

function openChangeset(id) {
  return paneLoad({key: 'c:' + id, title: 'Changeset'}, async () => {
    const cs = await api(`/api/changesets/${id}`);
    return {node: changesetDetail(cs), label: cs.title};
  });
}
// The machinery, step by step: when each step happened and how long it took. The server check and
// the sync that brings the change into Talos's copy come after apply (talos changeset check ID).
function csSteps(cs) {
  const sum = cs.summary || {}, dr = sum.dry_run, ap = sum.apply, ck = sum.check;
  const at = iso => iso ? new Date(iso).toLocaleString('sv-SE', {dateStyle: 'short', timeStyle: 'medium'}) : null;
  const steps = [
    ['Created', at(cs.created_at), 'what was selected and asked for'],
    ['Planned', at(cs.planned_at), `one operation per message, against Talos's copy of the server: ${fmt(sum.will_change || 0)} would change`],
    ['Dry run', dr ? at(dr.at) : null, dr ? `read each message on the server, sent nothing${dr.seconds != null ? ` · ${dr.seconds} s` : ''}` : 'not run'],
    ['Committed', at(cs.committed_at), 'your approval; nothing written yet'],
    ['Applied', ap || cs.finished_at ? at(cs.finished_at) : null, ap ? `${fmt(ap.done)} done, ${fmt(ap.failed)} failed in ${ap.seconds} s` : ''],
    ['Checked on the server', ck ? at(ck.at) : null, ck ? `${Object.entries(ck.server).map(([k, v]) => `${fmt(v)} in ${k}`).join(', ') || '—'}` : 'talos changeset check ' + cs.id],
    ['In Talos\'s copy', ck && ck.mirror_caught_up ? `${fmt(ck.mirror_caught_up)} of ${fmt(ck.checked)}` : null,
      ck && ck.mirror_lag_s ? `the sync saw it ${ck.mirror_lag_s.min}–${ck.mirror_lag_s.max} s after apply` : 'after the next sync (every 5 minutes)'],
  ];
  return h('ol', {class: 'cs-steps'}, steps.map(([name, when_, what]) =>
    h('li', {class: when_ ? 'on' : ''}, h('b', null, name), h('span', {class: 'small muted'}, when_ || '—'), h('span', {class: 'small'}, what))));
}
function csApplyCard(ap) {
  const conns = Object.entries(ap.connections || {});
  return h('section', {class: 'card', style: 'margin:0 0 12px'},
    h('div', {class: 'card-h'}, h('div', null, h('h2', null, 'Apply'), h('p', null, `${fmt(ap.done)} done, ${fmt(ap.failed)} failed · ${ap.seconds} s · ${ap.per_message_ms} ms per message`))),
    h('dl', {class: 'kv'},
      h('dt', null, 'Chunks'), h('dd', null, (ap.chunks || []).map(c => `${fmt(c.messages)} in ${c.seconds} s`).join(' · ') || '—'),
      conns.map(([acct, s]) => [h('dt', null, acctName(acct)), h('dd', null,
        `${fmt(s.batches)} batches, ${fmt(s.requests)} requests, ${s.http_s} s waiting on the server` +
        (s.throttled ? ` · throttled ${fmt(s.throttled)} times, ${s.waited_s} s waiting as asked` : ' · never throttled'))])));
}
function csCheckCard(ck) {
  const list = o => Object.entries(o || {}).map(([k, v]) => `${fmt(v)} × ${k}`).join(', ') || '—';
  return h('section', {class: 'card', style: 'margin:0 0 12px'},
    h('div', {class: 'card-h'}, h('div', null, h('h2', null, 'Check'), h('p', null, `${new Date(ck.at).toLocaleString('sv-SE')} · read-only · ${fmt(ck.checked)} messages${ck.sampled ? ' (a random sample)' : ''} in ${ck.seconds} s`))),
    h('dl', {class: 'kv'},
      h('dt', null, 'On the server'), h('dd', null, list(ck.server)),
      Object.keys(ck.problems || {}).length ? [h('dt', null, 'Problems'), h('dd', {class: 'err'}, list(ck.problems))] : null,
      h('dt', null, "In Talos's copy"), h('dd', null, list(ck.mirror))));
}
function changesetDetail(cs) {
  const id = cs.id;
  const req = cs.request || {}, sum = cs.summary || {}, dr = sum.dry_run;
  const status = h('div', {class: 'small muted', 'aria-live': 'polite'});
  const canDry = ['planned', 'committed'].includes(cs.status);
  const run = async () => {
    status.textContent = 'Checking against the server, read-only…';
    try { await post(`/api/changesets/${id}/dry-run`, {}); openChangeset(id); render(); }
    catch (e) { status.textContent = String(e); }
  };
  return h('div', null,
    h('div', {class: 'meta', style: 'margin:0 0 6px'}, `Changeset ${cs.id}`, '·', csPill(cs.shown || cs.status), csLinks(cs)),
    h('h2', null, cs.title),
    h('div', {class: `cs-next cs-next-${cs.shown || cs.status}`, role: 'note'}, h('b', null, 'Next: '), csNext(cs)),
    h('dl', {class: 'kv'},
      h('dt', null, 'Operation'), h('dd', null, req.op + (req.args && Object.keys(req.args).length ? ' ' + JSON.stringify(req.args) : '')),
      h('dt', null, 'Will change'), h('dd', null, fmt(sum.will_change)),
      sum.skipped_because && Object.keys(sum.skipped_because).length ? [h('dt', null, 'Skipped'), h('dd', null, Object.entries(sum.skipped_because).map(([k, v]) => `${v} × ${k}`).join(', '))] : null,
      cs.note ? [h('dt', null, 'Note'), h('dd', null, cs.note)] : null),
    h('div', {style: 'display:flex;gap:8px;align-items:center;margin:12px 0'},
      canDry ? h('button', {class: 'btn sm primary', onclick: run}, dr ? 'Dry-run again' : 'Dry run') : null, status),
    csSteps(cs),
    sum.apply ? csApplyCard(sum.apply) : null,
    sum.check ? csCheckCard(sum.check) : null,
    dr ? h('section', {class: 'card', style: 'margin:0 0 12px'},
      h('div', {class: 'card-h'}, h('div', null, h('h2', null, 'Dry run'), h('p', null, `${new Date(dr.at).toLocaleString('sv-SE')} · nothing was sent · ${fmt(dr.ok)} ready, ${fmt(dr.problems)} with a problem` +
        (dr.seconds != null ? ` · ${dr.seconds} s` : '') + (dr.items_total ? ` · showing ${dr.items.length} of ${fmt(dr.items_total)}` : '')))),
      dr.items.map(i => h('div', {class: 'dry-item'},
        h('div', null, acctDot(i.account), ' ', h('b', null, i.subject || '(no subject)'), h('span', {class: 'muted small'}, ` · message ${i.message_id}`)),
        i.server && i.server.labels ? h('div', {class: 'small muted'}, `Server now: flags ${i.server.flags.join(' ') || '—'} · labels ${i.server.labels.join(', ') || '—'}`) :
          i.server && i.server.folder ? h('div', {class: 'small muted'}, `Server now: in ${i.server.folder}${i.server.read === false ? ', unread' : ''}`) : null,
        i.would.length ? h('pre', {class: 'cmds'}, i.would.join('\n')) : null,
        i.problem ? h('div', {class: 'err small'}, i.problem) : null))) : null,
    h('h3', null, 'Operations'),
    cs.ops.length ? h('table', {class: 't'},
      h('thead', null, h('tr', null, h('th', null, 'Message'), h('th', null, 'Op'), h('th', null, 'Status'), h('th', null, 'Note'))),
      h('tbody', null, cs.ops.map(o => h('tr', {class: 'click', tabindex: '0', onclick: () => openMessage(o.message_id), onkeydown: e => { if (e.key === 'Enter') openMessage(o.message_id); }},
        h('td', {class: 'nm'}, acctDot(o.account_id), ' ', o.subject || '(no subject)'),
        h('td', null, o.op), h('td', null, h('span', {class: 'pill ' + (o.status === 'done' ? 'ok' : o.status === 'failed' ? 'hi' : '')}, o.status)),
        h('td', {class: 'small muted'}, o.error || ''))))) : empty('Not planned yet', `talos changeset plan ${cs.id}`),
    cs.ops.length ? rowsLegend(cs.ops) : null);
}

// ---------------------------------------------------------------- structure
// The mailbox structure planner (talos.structure): where every mail would go under Talos/, per
// account, as a tree with counts. A node shows the rule that places mail there and examples; an
// example's "Why here?" shows the values and every rule tested in order. The page only reads: the
// changesets it lists were prepared from the command line and stay planned until the owner commits them.
const STRUCT = {acc: null, sel: null, rule: null, open: {}};
const PLACE_TEXT = {inbox: 'stays in the inbox', label: 'label', leave: 'left alone'};
async function viewStructure() {
  const [d, chk] = await Promise.all([api('/api/structure'), api('/api/structure/checklist').catch(() => null)]);
  const accs = d.accounts || [];
  if (!accs.length) return h('div', null, header('Structure', 'The new Talos/ structure, planned'), structureLead(), d.rules_error ? h('p', {class: 'err'}, d.rules_error) : null,
    emptyPage('No plan yet', 'Compute one with  talos structure plan  (nothing is written to a mailbox).'));
  if (!accs.find(a => a.account === STRUCT.acc)) { STRUCT.acc = (accs.find(a => a.planned) || accs[0]).account; STRUCT.sel = null; }
  const a = accs.find(x => x.account === STRUCT.acc);
  const pick = acc => { STRUCT.acc = acc; STRUCT.sel = null; STRUCT.rule = null; render(true); };
  const detail = h('div');
  const choose = (node, row) => {
    STRUCT.sel = node.path; STRUCT.rule = null;
    $main.querySelectorAll('.stree .srow.on').forEach(x => x.classList.remove('on'));
    if (row) row.classList.add('on');
    structureNode(a, node, detail);
  };
  const tree = h('div', {class: 'stree', role: 'tree', 'aria-label': `Target structure of ${a.name}`}, structureTree(a.tree, 0, choose));
  const csRows = (d.changesets || []).filter(c => !c.structure || c.structure.account === a.account);
  const view = h('div', null,
    header(withHelp('Structure', 'structure'), `The new Talos/ structure, planned · rules v${d.rules ? d.rules.version : '?'}${d.rules ? ' (' + d.rules.sha + ')' : ''}`,
      seg(accs.map(x => [x.account, x.name]), STRUCT.acc, pick)),
    structureLead(a),
    d.rules_error ? h('p', {class: 'err'}, 'The rules file cannot be read: ' + d.rules_error) : null,
    a.stale_rules ? h('div', {style: 'margin-top:12px'}, note('The rules file has changed since this plan was computed. Run  talos structure plan  to see its effect.')) : null,
    h('div', {style: 'margin-top:16px'}, structureChecklist(a, ((chk || {}).accounts || []).find(x => x.account === a.account))),
    !a.planned ? h('div', {style: 'margin-top:16px'}, emptyPage(`${a.name} is not planned yet`, `talos structure plan --account ${a.account}`)) :
    h('div', null,
      h('div', {class: 'kpis', style: 'border:0;margin:16px 0 14px;padding:0'},
        h('div', {class: 'kpi'}, h('div', {class: 'l'}, 'Inbox now → after'), h('div', {class: 'v'}, `${fmt(a.inbox_before)} → ${fmt(a.inbox_after)}`),
          h('div', {class: 'n'}, `${fmt(a.inbox_before - a.inbox_after)} would leave the inbox`)),
        h('div', {class: 'kpi'}, h('div', {class: 'l'}, 'To sort'), h('div', {class: 'v'}, fmt(a.to_sort)),
          h('div', {class: 'n'}, `${a.total ? Math.round(100 * a.to_sort / a.total) : 0}% not decided yet`)),
        h('div', {class: 'kpi'}, h('div', {class: 'l'}, 'Email messages'), h('div', {class: 'v'}, fmt(a.total)),
          h('div', {class: 'n'}, `${fmt(a.left_alone)} left alone (sent, drafts…)`)),
        h('div', {class: 'kpi'}, h('div', {class: 'l'}, 'Computed'), h('div', {class: 'v'}, when(a.computed_at)),
          h('div', {class: 'n'}, a.full_run && a.full_run.seconds != null ? `full plan in ${a.full_run.seconds} s` : ''))),
      h('div', {class: 'grid g2'},
        card('Target structure', a.provider === 'gmail' ? 'Gmail labels under Talos/. Click a place to see its rule and examples.' :
          'Folders under a Talos folder, planned only. Click a place to see its rule and examples.', null, tree),
        h('section', {class: 'card'}, detail)),
      h('div', {class: 'grid g2'},
        card('Old labels and folders', 'Where the mail in each old place would go. "Agree" counts mail whose new place is the one its old place maps to (rules/structure.json, "old").', null,
          structureOld(a)),
        card('Prepared changesets', a.changesets_possible ? 'Made with "Prepare the next changesets" above, or  talos structure changesets --account ' + a.account + ' . Each stays planned until you commit it.' :
          'None for this account: moving mail into Talos/ folders waits for your go (Microsoft 365 is planned only).',
          h('button', {class: 'btn sm', onclick: () => go('changesets')}, 'Open Changesets'),
          csRows.length ? h('table', {class: 't'},
            h('thead', null, h('tr', null, h('th', null, '#'), h('th', null, 'Title'), h('th', null, 'Status'), h('th', {class: 'r'}, 'Will change'))),
            h('tbody', null, csRows.map(c => h('tr', {class: 'click', tabindex: '0', 'data-pane-key': 'c:' + c.id, onclick: () => openChangeset(c.id), onkeydown: e => { if (e.key === 'Enter') openChangeset(c.id); }},
              h('td', {class: 'num'}, c.id), h('td', {class: 'nm'}, c.title), h('td', null, h('span', {class: 'pill' + (c.status === 'planned' ? '' : ' md')}, c.status)),
              h('td', {class: 'r'}, fmt((c.summary || {}).will_change))))))
            : empty('None yet', a.changesets_possible ? `talos structure changesets --account ${a.account} --limit 1` : 'Planned only.')))));
  const flat = [], walk = ns => ns.forEach(n => { flat.push(n); walk(n.children); });
  walk(a.tree || []);
  const sel = flat.find(n => n.path === STRUCT.sel) || flat.find(n => n.leaf && n.count) || flat[0];
  if (sel && a.planned) { STRUCT.sel = sel.path; structureNode(a, sel, detail); queueMicrotask(() => { const r = tree.querySelector(`[data-path="${CSS.escape(sel.path)}"]`); if (r) r.classList.add('on'); }); }
  return view;
}
function structureLead(a) {
  return h('div', null,
    structureIntro(a),
    note(a && a.provider !== 'gmail' && a.provider ? 'Microsoft 365 is planned only. Folders under Talos and the moves into them wait for your go.' :
      'Gmail gets labels under Talos/ and leaves the inbox by an archive changeset. Every changeset is prepared here (Prepare the next changesets) or from the command line, stays planned, and you dry-run and commit it yourself (1, then 10, 100, all).'));
}
// What the page is, in plain words, before any number.
function structureIntro(a) {
  const box = (title, ...text) => h('div', {class: 'sintro-i'}, h('h3', null, title), h('p', null, ...text));
  // Why here? for one example of the place chosen in the tree (or the biggest one).
  const whyExample = async () => {
    const flat = [], walk = ns => ns.forEach(n => { flat.push(n); walk(n.children || []); });
    walk(a.tree || []);
    const n = flat.find(x => x.path === STRUCT.sel && x.count) || flat.filter(x => x.leaf && x.count).sort((x, y) => y.count - x.count)[0];
    if (!n) return;
    const ex = await api('/api/structure/examples?' + new URLSearchParams({account: a.account, target: n.path, limit: '1'}));
    if (ex.rows.length) openWhy(ex.rows[0].id);
  };
  const why = a && a.planned ? h('button', {class: 'linkbtn', style: 'padding:0', onclick: whyExample}, 'Why here?') : h('b', null, 'Why here?');
  return h('div', {class: 'sintro'},
    box('What the tree is', 'The planned ', h('b', null, 'Talos/…'), ' labels (Gmail) and folders (Microsoft 365), per account, and how much mail would land in each. It is where your mail would live in a tidier mailbox: a few places by what the mail is to you, instead of the old labels and folders.'),
    box('How mail gets its place', 'Every mail is tested against the rules in rules/structure.json, top to bottom, and ', h('b', null, 'the first rule that matches decides'),
      '. The rules read the values Talos has for the mail (kind, value, topic, sender, whether you replied). Click a place, then ', why, ' on an example to see its values and every rule tested in order.'),
    box('Nothing has changed yet', 'This is a plan. ', h('b', null, 'No mailbox has been touched.'),
      ' It becomes real only through changesets that you review, dry-run, commit and apply yourself, a few messages at a time. The checklist below says how far each account is.'));
}

// "How to make it real": the steps from the plan to the mailbox, with their live state. Talos sees
// the first steps (the plan, the Talos labels on the server, the changesets waiting); the last ones
// are the owner's, by hand, and their tick is kept in this browser.
const STRUCT_TICKS_KEY = 'talos-structure-ticks';
function structTicks() {
  try { const x = JSON.parse(localStorage.getItem(STRUCT_TICKS_KEY) || '{}'); return x && typeof x === 'object' ? x : {}; } catch (e) { return {}; }
}
function structTick(key, on) {
  const t = structTicks();
  if (on) t[key] = true; else delete t[key];
  try { localStorage.setItem(STRUCT_TICKS_KEY, JSON.stringify(t)); } catch (e) {}
}
function checkStep(n, state, title, detail, action, extra) {
  const mark = state === true ? '✓' : state === 'hand' ? '✎' : '';
  return h('li', {class: 'chk chk-' + (state === true ? 'done' : state === 'hand' ? 'hand' : 'todo')},
    h('span', {class: 'chk-box', 'aria-hidden': 'true'}, mark || String(n)),
    h('div', {class: 'chk-body'},
      h('div', {class: 'chk-t'}, h('b', null, title), h('span', {class: 'sr-only'}, state === true ? ' (done)' : ' (not done yet)')),
      detail ? h('div', {class: 'small sec'}, detail) : null, extra || null),
    h('div', {class: 'chk-a'}, action || null));
}
function structureChecklist(a, cl) {
  if (!cl) return null;
  const st = Object.fromEntries(cl.steps.map(s => [s.id, s]));
  const ticks = structTicks();
  const toChangesets = h('button', {class: 'btn sm', onclick: () => go('changesets')}, 'Review in Changesets ›');
  const manual = (id, title, detail) => {
    const key = `${cl.account}:${id}`;
    const box = h('input', {type: 'checkbox', checked: !!ticks[key], 'aria-label': `Mark done: ${title}`,
      onchange: e => { structTick(key, e.target.checked); render(true); }});
    return [title, detail, h('label', {class: 'small chk-tick'}, box, ' done, by hand'), ticks[key] ? true : 'hand'];
  };
  const items = [];
  let n = 0;
  const push = (state, title, detail, action, extra) => items.push(checkStep(++n, state, title, detail, action, extra));
  const plan = st.plan;
  push(plan.done, 'Plan computed',
    plan.done ? `Computed ${csWhen(plan.at)} with rules v${plan.rules_version}: ${fmt(plan.count)} messages placed.` +
      (plan.stale ? ' The rules have changed since: run  talos structure plan  again.' : '') : 'Not yet: run  talos structure plan  (nothing is written to a mailbox).',
    null);
  if (st.labels) {
    const L = st.labels;
    const targets = h('details', {class: 'chk-more'}, h('summary', {class: 'small'}, `Per label (${L.targets.length})`),
      h('table', {class: 't'}, h('thead', null, h('tr', null, h('th', null, 'Talos label'), h('th', {class: 'r'}, 'Planned'), h('th', {class: 'r'}, 'On the server'),
        h('th', {class: 'r'}, 'Waiting for review'), h('th', null, 'Changesets'))),
        h('tbody', null, L.targets.map(t => h('tr', null, h('td', null, t.target.replace('Talos/', '')), h('td', {class: 'r'}, fmt(t.planned)),
          h('td', {class: 'r'}, fmt(t.on_server)), h('td', {class: 'r'}, fmt(t.waiting)),
          h('td', null, t.changesets.map(id => h('button', {class: 'linkbtn cs-link', onclick: () => openChangeset(id)}, `#${id}`))))))));
    push(L.done, 'Gmail: labels written',
      `${fmt(L.on_server)} of ${fmt(L.planned)} messages carry their Talos label on the server. ${fmt(L.waiting)} are in changesets waiting for your review; ${fmt(L.to_prepare)} are not prepared yet.`,
      L.changesets.length ? toChangesets : null, targets);
  }
  if (st.archive) {
    const A = st.archive;
    push(A.done, 'Gmail: inbox cleaned',
      `${fmt(A.left)} of ${fmt(A.planned)} messages that should leave the inbox have left it. ${fmt(A.ready)} carry their label and can be archived; ${fmt(A.waiting)} are in archive changesets waiting. ` +
      'Only labelled mail is archived, so nothing leaves the inbox before it can be found in its new place: write the labels, sync, then archive.',
      A.changesets.length ? toChangesets : null);
  }
  if (st.consent) {
    push(st.consent.done, `${a.name}: write-back for the Talos/ folders`,
      st.consent.done ? 'Write-back is on for this account.' :
        'Your go first: the work account\'s write-back is switched on only for an agreed use, and off afterwards (docs/writeback-test-plan.md); the Talos/ folders are not one yet. Until then it is plan only: no folders are made and nothing moves.',
      null);
  }
  const [t5, d5, a5, s5] = manual('old_rules', 'Remove the old rules and filters, by hand',
    a.provider === 'gmail' ? 'Gmail filters still put new mail under the old labels. Remove them once the Talos labels are in place, or they keep filling the old places.' :
      'Outlook rules still move new mail into the old folders. Remove them once the Talos folders are in place.');
  push(s5, t5, d5, a5);
  const R = st.retire;
  const [t6, d6, a6, s6] = manual('retire', 'Retire the old labels and folders, later, by hand',
    `${fmt(R.count)} old ${a.provider === 'gmail' ? 'labels' : 'folders'} still held mail at the last plan. Remove them when nothing needs them any more; the mail keeps its new place.`);
  push(s6, t6, d6, a6);
  return card('How to make it real', `The steps from this plan to ${a.name}'s mailbox, with where each stands now`,
    cl.provider === 'gmail' ? structurePrepare(cl) : null,
    h('ol', {class: 'chk-list'}, items));
}
// The button that prepares the next changesets: planned only, never committed. Their size grows as
// write-back does (1, then 10, 100, all), by the largest structure changeset applied so far.
function structurePrepare(cl) {
  const sizes = [['1', '1 per label'], ['10', '10 per label'], ['100', '100 per label'], ['', 'all']];
  const cur = cl.next_size == null ? '' : String(cl.next_size);
  const size = h('select', {class: 'sel', 'aria-label': 'How many messages per label'}, sizes.map(([v, t]) => h('option', {value: v}, t)));
  size.value = cur;
  const btn = h('button', {class: 'btn sm primary', disabled: !cl.can_prepare,
      title: cl.can_prepare ? 'Prepare planned changesets for your review; nothing is committed' : 'Nothing to prepare: every planned label is on the server or in a changeset waiting for review'},
    'Prepare the next changesets');
  btn.addEventListener('click', async () => {
    const limit = size.value ? Number(size.value) : null;
    const what = limit ? `up to ${limit} message${limit === 1 ? '' : 's'} per Talos label` : 'every message still to label';
    if (!confirm(`Prepare changesets for ${cl.name}: ${what}, and an archive for labelled inbox mail?\n\nThey are only prepared, as planned changesets. Nothing is committed and nothing is written to the mailbox. You review, dry-run and commit each one yourself under Operations › Changesets.`)) return;
    btn.disabled = true; fill(btn, 'Preparing…');
    try {
      const res = await post('/api/structure/prepare', {account: cl.account, limit});
      const k = res.made.length;
      flash(k ? `Prepared ${k} changeset${k === 1 ? '' : 's'} (${fmt(res.will_change)} messages), planned. Review them under Changesets.` : (res.notes[0] || 'Nothing new to prepare.'));
    } catch (e) { flash('Could not prepare: ' + errText(e)); }
    render(true);
  });
  return h('div', {class: 'head-r'}, size, btn);
}
function structureTree(nodes, depth, choose) {
  return nodes.map(n => {
    const kids = n.children || [];
    const open = STRUCT.open[n.path] !== false;
    const row = h('div', {class: 'srow' + (n.leaf ? '' : ' grp') + (n.place === 'leave' ? ' dim' : ''), role: 'treeitem', tabindex: '0', 'data-path': n.path,
        'aria-expanded': kids.length ? String(open) : null, style: `padding-left:${8 + depth * 16}px`,
        onclick: e => choose(n, e.currentTarget), onkeydown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); choose(n, e.currentTarget); } }},
      kids.length ? h('button', {class: 'linkbtn stog', 'aria-label': open ? 'Collapse' : 'Expand', onclick: e => { e.stopPropagation(); STRUCT.open[n.path] = !open; render(true); }}, open ? '▾' : '▸') : h('span', {class: 'stog'}),
      h('span', {class: 'sname'}, n.name),
      n.leaf && n.place === 'inbox' ? h('span', {class: 'pill ac'}, 'stays') : null,
      n.leaves_inbox ? h('span', {class: 'small muted', title: 'In the inbox now; would leave it'}, `${fmt(n.leaves_inbox)} leave the inbox`) : null,
      h('span', {class: 'scount'}, fmt(n.count)));
    return [row, kids.length && open ? h('div', {role: 'group'}, structureTree(kids, depth + 1, choose)) : null];
  });
}
async function structureNode(a, n, box) {
  const rules = n.rules || [];
  const list = h('div', null, h('div', {class: 'muted small'}, 'Loading examples…'));
  fill(box,
    h('div', {class: 'card-h'}, h('div', null, h('h2', null, n.path), h('p', null, `${fmt(n.count)} messages` + (n.in_inbox ? ` · ${fmt(n.in_inbox)} in the inbox now` : '') + (n.leaves_inbox ? `, ${fmt(n.leaves_inbox)} would leave it` : '')))),
    rules.length ? h('div', {class: 'dsec'}, h('h4', null, rules.length > 1 ? 'Rules that place mail here' : 'The rule that places mail here'),
      rules.map(r => h('div', {class: 'srule'},
        h('div', null, h('b', null, r.id), ' ', h('span', {class: 'pill'}, PLACE_TEXT[r.place] || r.place),
          rules.length > 1 ? h('button', {class: 'linkbtn', style: 'margin-left:6px', onclick: () => { STRUCT.rule = STRUCT.rule === r.id ? null : r.id; loadEx(0); }}, 'only this rule') : null),
        r.why ? h('div', {class: 'small sec'}, r.why) : null,
        h('ul', {class: 'conds small'}, r.conditions.length ? r.conditions.map(c => h('li', null, c)) : h('li', {class: 'muted'}, 'no conditions: everything that reaches it'))))) :
      (n.leaf ? null : h('div', {class: 'small muted'}, 'A folder: the counts include everything under it.')),
    h('div', {class: 'dsec'}, h('h4', null, 'Examples'), list));
  const loadEx = async offset => {
    try {
      const q = new URLSearchParams({account: a.account, target: n.path, offset: String(offset), limit: '25'});
      if (STRUCT.rule) q.set('rule', STRUCT.rule);
      const ex = await api('/api/structure/examples?' + q);
      fill(list, ex.rows.length ? [
        h('div', {class: 'small muted', style: 'margin-bottom:6px'}, `${fmt(offset + 1)}–${fmt(offset + ex.rows.length)} of ${fmt(ex.total)}${STRUCT.rule ? ' · rule ' + STRUCT.rule : ''}, newest first`),
        h('table', {class: 't'}, h('tbody', null, ex.rows.map(m => h('tr', {class: 'click', tabindex: '0', 'data-pane-key': 'm:' + m.id,
            onclick: () => openMessage(m.id), onkeydown: e => { if (e.key === 'Enter') openMessage(m.id); }},
          h('td', {style: 'width:74px'}, when(m.received_at)),
          h('td', {class: 'nm'}, h('b', null, m.from_name || m.from_address || '?'), h('div', {class: 'small muted'}, m.subject || '(no subject)'),
            n.leaf ? null : h('div', {class: 'small muted'}, m.target.replace(n.path + '/', ''))),
          h('td', {class: 'small muted'}, m.rule_id, m.leaves_inbox ? h('div', null, 'leaves the inbox') : null),
          h('td', {class: 'r'}, h('button', {class: 'btn sm', title: 'The values and rules that put it here', onclick: e => { e.stopPropagation(); openWhy(m.id); }}, 'Why here?')))))),
        h('div', {style: 'display:flex;gap:8px;margin-top:8px'},
          offset > 0 ? h('button', {class: 'btn sm', onclick: () => loadEx(Math.max(0, offset - 25))}, 'Newer') : null,
          offset + ex.rows.length < ex.total ? h('button', {class: 'btn sm', onclick: () => loadEx(offset + 25)}, 'Older') : null)]
        : empty('Nothing here', 'No message is placed here in the current plan.'));
    } catch (e) { fill(list, h('div', {class: 'err'}, errText(e))); }
  };
  loadEx(0);
}
function structureOld(a) {
  const rows = a.old || [];
  if (!rows.length) return empty('No old places', 'The plan was computed before the report existed, or the account has none.');
  return h('div', null,
    a.same_place != null ? h('div', {class: 'small muted', style: 'margin-bottom:6px'}, `${fmt(a.same_place)} messages already sit in an old place that maps to their new one.`) : null,
    h('table', {class: 't'},
      h('thead', null, h('tr', null, h('th', null, 'Old place'), h('th', {class: 'r'}, 'Mail'), h('th', null, 'Goes mostly to'), h('th', {class: 'r'}, 'Agree'))),
      h('tbody', null, rows.map(o => h('tr', null,
        h('td', {class: 'nm'}, o.old, o.maps_to ? h('small', null, '→ ' + o.maps_to.replace('Talos/', '')) : null),
        h('td', {class: 'r'}, fmt(o.count)),
        h('td', {class: 'small'}, o.targets.slice(0, 3).map(t => h('div', null, `${t.target.replace('Talos/', '')} · ${fmt(t.count)}`))),
        h('td', {class: 'r'}, o.maps_to ? `${Math.round(100 * o.same / Math.max(1, o.count))}%` : h('span', {class: 'muted'}, '—')))))));
}
function openWhy(id) {
  return paneLoad({key: 'sw:' + id, title: 'Why here'}, async () => {
    const w = await api(`/api/structure/why/${id}`);
    return {node: whyDetail(w), label: 'Why here'};
  });
}
function whyDetail(w) {
  if (!w.placed) return h('div', null, h('h2', null, 'Not placed'), h('p', null, w.reason));
  const f = w.features || {};
  const show = v => Array.isArray(v) ? (v.length ? v.join(', ') : '—') : v == null ? '—' : v === true ? 'yes' : v === false ? 'no' : String(v);
  const src = {};
  (w.values || []).forEach(v => { src[v.dimension_id] = `${SOURCE_TEXT[v.source_kind] || v.source_kind}${v.via === 'thread' ? ', on the thread' : ''}`; });
  const rows = [['sender', f.sender, f.sender_kind ? 'sender_kind' : f.origin ? 'from the origin' : ''], ['kind', f.kind], ['type', f.type], ['origin', f.origin],
    ['topic', f.topic], ['value', f.value], ['keep', f.keep], ['sphere', f.sphere], ['form', f.form], ['ask', f.ask], ['ask proposed', f.ask_proposed],
    ['replied', f.replied], ['flagged', f.flagged], ['in the inbox now', f.in_inbox], ['age (days)', f.age_days], ['direction', f.direction],
    ['folders', f.folders], ['labels', f.labels]];
  let past = false;
  return h('div', null,
    h('div', {class: 'meta', style: 'margin:0 0 6px'}, `Message ${w.message_id}`, '·', w.account),
    h('h2', null, w.now.target), h('p', {class: 'small'}, `Placed by ${w.now.rule_id}` + (w.stored ? ` · stored plan: ${w.stored.target}${w.stale ? ' (differs: the plan is older than the values or the rules)' : ''}` : ' · not in the stored plan yet')),
    h('div', {style: 'margin:6px 0 10px'}, h('button', {class: 'btn sm', onclick: () => openMessage(w.message_id)}, 'Open the message')),
    h('div', {class: 'dsec'}, h('h4', null, 'The values it was placed by'),
      h('dl', {class: 'kv', style: 'display:grid;grid-template-columns:auto minmax(0,1fr);gap:3px 12px;font-size:12.5px;margin:0'},
        rows.map(([k, v, extra]) => [h('dt', {class: 'muted'}, k), h('dd', {style: 'margin:0;overflow-wrap:anywhere'}, show(v),
          (src[k] || extra) ? h('span', {class: 'muted'}, ` · ${src[k] || extra}`) : null)]))),
    h('div', {class: 'dsec'}, h('h4', null, 'The rules, in order'),
      w.tests.map(t => {
        const cls = t.first ? 'why-t first' : past ? 'why-t after' : 'why-t';
        if (t.first) past = true;
        return h('div', {class: cls},
          h('div', null, h('span', {class: 'why-mark', 'aria-hidden': 'true'}, t.matched ? '✓' : '✗'), ' ', h('b', null, t.path), h('span', {class: 'muted small'}, ` · ${t.id}`),
            t.first ? h('span', {class: 'pill ac', style: 'margin-left:6px'}, 'first match') : null, h('span', {class: 'sr-only'}, t.matched ? ' matches' : ' does not match')),
          h('ul', {class: 'conds small'}, t.conditions.map(c => h('li', {class: c.ok ? 'ok' : 'no'}, h('span', {'aria-hidden': 'true'}, c.ok ? '✓ ' : '✗ '), c.text))));
      })));
}

// ---- Security: the sessions that are signed in, and what happened at the door (talos.webauth).
const DOOR_WORDS = {login_ok: 'Signed in', login_fail: 'Wrong password or code', logout: 'Signed out', signout_all: 'Signed out everywhere',
  session_minted: 'Session made on this Mac', recovery_used: 'Recovery code used', bulk_stop: 'Reading paused (a lot at once)',
  step_up: 'Code entered to go on', step_up_fail: 'Wrong code to go on'};
function securityCard() {
  const box = h('div', null, h('div', {class: 'small muted'}, 'Loading…'));
  fetchJSON('/auth/me').then(d => {
    const lim = d.limits;
    fill(box,
      h('p', {class: 'small muted'}, `A session lasts ${lim.max_days} days at most, and ends after ${lim.idle_hours} hours unused. `
        + `Opening more than about ${fmt(Math.round(lim.read_budget / 2))} messages in ${lim.read_window_minutes} minutes asks for a code.`),
      h('h4', {class: 'sec-h'}, 'Signed in'),
      h('table', {class: 't'}, h('tbody', null, d.sessions.map(x => h('tr', null,
        h('td', {class: 'nm'}, x.origin.replace(/^local:.*/, 'On this Mac').replace(/^tailnet:/, 'Tailscale · '), h('small', null, (x.user_agent || '').slice(0, 80))),
        h('td', {class: 'r muted'}, 'last ' + when(x.last_seen)),
        h('td', {class: 'r'}, x.current ? h('span', {class: 'pill ok'}, 'this one') : null))))),
      h('div', {class: 'rrow', style: 'margin:8px 0 4px'},
        h('button', {class: 'btn sm', disabled: d.sessions.length < 2, onclick: async () => {
          if (!confirm('Sign out every other session? This one stays signed in.')) return;
          try { const r = await post('/auth/signout-all', {}); flash(`${fmt(r.ended)} signed out`); render(true); } catch (e) { alert(errText(e)); }
        }}, 'Sign out everywhere else'),
        h('button', {class: 'btn ghost sm', onclick: signOut}, 'Sign out')),
      h('h4', {class: 'sec-h'}, 'At the door'),
      d.events.length ? h('table', {class: 't'}, h('tbody', null, d.events.map(e => h('tr', null,
        h('td', {class: e.kind.includes('fail') || e.kind === 'bulk_stop' ? 'nm err' : 'nm'}, DOOR_WORDS[e.kind] || e.kind),
        h('td', {class: 'muted small'}, e.origin.replace(/^local:.*/, 'on this Mac').replace(/^tailnet:/, 'Tailscale · ')),
        h('td', {class: 'r muted'}, when(e.at)))))) : h('div', {class: 'small muted'}, 'Nothing yet.'));
  }).catch(e => fill(box, h('div', {class: 'err'}, errText(e))));
  return card('Security', 'Who is signed in, and what happened at the door', null, box);
}

// ---------------------------------------------------------------- sources (Tune › Sources)
// The accounts and their syncs, the Keychain items they need (never their values), and the door.
async function viewSources() {
  const o = await api('/api/overview');
  pollWhileRefreshing(o, '/api/overview');
  return h('div', null, header(withHelp('Sources', 'sources'), 'Where Talos reads from', syncButton()),
    h('p', {class: 'lead'}, 'Talos reads, and sends only a mail you compose and confirm. Nothing is ever deleted.'),
    h('div', {class: 'srcgrid', style: 'margin-top:16px'}, o.accounts.map(a => h('section', {class: 'card srccard', style: `--acct:${acctColor(a.id)}`},
      h('div', {class: 'top'}, h('span', {class: 'status-dot', style: `background:${acctColor(a.id)}`}), h('div', {style: 'flex:1'}, h('b', null, a.display_name || acctName(a.id)), h('div', {class: 'small muted'}, a.provider)),
        h('span', {class: 'pill ' + (!a.enabled ? '' : a.last_sync ? 'ok' : 'md')}, !a.enabled ? 'off' : a.last_sync ? 'syncing' : 'not synced yet')),
      h('dl', {class: 'kv'}, h('dt', null, 'Messages'), h('dd', null, fmt(a.messages)), h('dt', null, 'Newest'), h('dd', null, day(a.latest)), h('dt', null, 'Last sync'), h('dd', null, a.last_sync ? new Date(a.last_sync).toLocaleString('sv-SE') : 'never'))))),
    securityCard(),
    card('Guarantees', 'Enforced in code and tests', null,
      note('Sync is read-only: IMAP folders are opened read-only and bodies fetched with BODY.PEEK; Graph receives GET requests only. The tests fail on any write.'),
      note('Talos sends a mail only when you press Send on a mail you composed and then confirm the account it goes from. No rule, job or AI can send: one module holds the sending code, only the compose pane\'s send route reaches it, and it needs a fresh confirmation for that exact mail. Nothing can delete permanently. A guard test scans the source for all of it.'),
      note('Secrets are in the macOS Keychain. The server listens on 127.0.0.1 and refuses other Host headers.')));
}

// ---------------------------------------------------------------- acceptance
// The acceptance explorer (talos.acceptance; docs/enrichment-plan.md §11), written for a reader who
// is not a statistician. Jev gives each answer a confidence; per field a slider sets how sure Jev
// must be before Talos accepts the answer, and the card says in one sentence what that level gives:
// how many of the owner's messages get a value, and how often that is wrong (and seriously wrong) on the
// answer key. The four boundaries come first (the costly mistakes: a person taken for a machine),
// then the exact values, each accepted only on its boundary's side. The level applied now is a
// marker on each slider; nothing changes until Apply. Every number is recomputed here from what
// /api/acceptance sent (the answer key's cases, the archive's histograms); only the example
// messages are asked for, when the slider stops. Apply first shows what would change (a dry run),
// then asks.
const ACC = {goldRun: '', t: {}, st: null, preview: null, busy: false, timers: {}, pending: null};
const pctText = x => x == null ? '—' : `${(100 * x).toFixed(x > 0 && x < 0.995 ? 1 : 0)} %`;
const lvlText = t => `${Math.round(100 * Number(t))} %`;
const accLabel = (f, v) => (f.values && f.values[v]) || String(v == null ? '—' : v).replace(/_/g, ' ');
// Plain words per field: what getting a value means, what a serious mistake mixes up, and (for an
// exact field) a mild mistake, for contrast.
const ACC_PLAIN = {
  sender_kind: {get: 'are marked as written by a person or by a machine', mix: 'a person and a machine',
                serious: 'A person taken for a machine, or a machine for a person: a question from someone could be filed away as a notice.'},
  sphere: {get: 'are marked work or personal', mix: 'work and personal', serious: 'Work taken for personal, or personal for work.'},
  form: {get: 'are marked conversation or other', mix: 'a conversation and something else',
         serious: 'A conversation with people taken for something else (a notice, a newsletter), or the other way round.'},
  keep: {get: 'are marked worth keeping or short-lived', mix: 'worth keeping and short-lived',
         serious: 'Something worth keeping taken for short-lived, or the other way round.'},
  kind: {get: 'get a kind', mix: 'one kind with another',
         serious: 'A message of the wrong kind: a question filed as a notice, an alert as a report, an offer as an announcement.'},
  origin: {get: 'get an origin', mild: 'an alert called a notification'},
  type: {get: 'get a type', mild: 'a request called a question'},
  topic: {get: 'get a topic', mild: 'one work topic taken for another'},
  value: {get: 'get a value', mild: 'a reference called knowledge'}};
const accPlain = f => ACC_PLAIN[f.id] || {get: `get a ${String(f.label).toLowerCase()}`};
const accMix = (f, b) => (f.kind !== 'exact' ? accPlain(f).mix : b && accPlain(b).mix) || 'the two sides of its boundary';
function accSerious(f, b) {
  if (f.kind !== 'exact') return accPlain(f).serious || `A message put on the wrong side of ${String(f.label).toLowerCase()}.`;
  const mild = accPlain(f).mild;
  return `A wrong ${String(f.label).toLowerCase()} that also mixes up ${accMix(f, b)}.` + (mild ? ` Other mistakes, such as ${mild}, are mild.` : '');
}
// k of n as "1 in 20" (or a share, when it is more than half); "none" when k is 0.
function oneIn(k, n, about = true) {
  if (!k) return 'none';
  const r = n / k;
  return (about ? 'about ' : '') + (r >= 2 ? `1 in ${fmt(Math.round(r))}` : `${Math.round(100 * k / n)} %`);
}
const isShare = x => x.endsWith('%');
// The sentence under a slider: what this level gives in the archive, and how often it is wrong on
// the answer key. An exact field's archive count is "up to": a value its boundary disagrees with is held back.
function accSentence(f, b, t, sc, ar, hasGold) {
  const exact = f.kind === 'exact';
  const first = ar ? `${exact ? 'up to ' : ''}${fmt(ar.n)} of your messages ${accPlain(f).get}.` : 'Jev has made no suggestions for this field in your archive yet.';
  let second;
  if (!hasGold || !sc.n) second = 'There is no answer key run to measure it against yet.';
  else if (!sc.decided) second = 'On the answer key, Jev is never this sure, so no message there would get a value.';
  else {
    const wrong = sc.decided - sc.right, w = oneIn(wrong, sc.decided);
    if (!wrong) second = 'On the answer key, none of these is wrong.';
    else if (f.kind === 'kind') second = `On the answer key, ${w} of these ${isShare(w) ? 'are' : 'is'} of the wrong kind.`;
    else if (!exact) second = `On the answer key, ${w} of these ${isShare(w) ? 'are' : 'is'} on the wrong side, mixing up ${accMix(f, b)}.`;
    else {
      const c = oneIn(sc.costly, sc.decided, false);
      second = `On the answer key, ${w} of these ${isShare(w) ? 'are' : 'is'} wrong, and ${c} ${isShare(c) ? 'mix' : 'mixes'} up ${accMix(f, b)}.`;
    }
  }
  return [h('b', null, `At ${lvlText(t)}: `), first, ' ', second];
}
// One field on the answer key at a threshold: as acceptance.score() in Python (a test holds them equal).
function accScore(cases, t, margin, tb) {
  let decided = 0, right = 0, costly = 0;
  const wrong = new Map();
  for (const x of cases) {
    let ok = x.c >= t - 1e-9 && (margin == null || x.m >= margin - 1e-9);
    if (ok && tb != null && x.bs != null && x.bp >= tb - 1e-9) ok = x.bs === x.ps;
    if (!ok) continue;
    decided++;
    if (x.p === x.r) { right++; continue; }
    const k = `${x.p}→${x.r}`;
    wrong.set(k, (wrong.get(k) || 0) + 1);
    if (x.ps != null && x.rs != null && x.ps !== x.rs) costly++;
  }
  return {n: cases.length, decided, right, costly, coverage: cases.length ? decided / cases.length : null,
          accuracy: decided ? right / decided : null, confusions: [...wrong].sort((a, b) => b[1] - a[1]).slice(0, 5)};
}
const accBucket = t => Math.min(19, Math.max(0, Math.floor(t * 20 + 1e-6)));
function accArchive(st, f, t) {
  const h = st.archive[f.id];
  if (!h) return null;
  const arr = f.kind !== 'exact' ? h.all : h.margin_ok;
  return {n: arr.slice(accBucket(t)).reduce((a, b) => a + b, 0), total: h.total, active: h.active};
}
// The archive's suggestions as 20 bars (0–100 %), those at or over the line in the accent colour; a
// solid line at the level tried, a dashed one at the level in use.
function accHistogram(st, f, t, applied) {
  const h = st.archive[f.id];
  const arr = h ? (f.kind !== 'exact' ? h.all : h.margin_ok) : [];
  const max = Math.max(1, ...arr), W = 200, H = 44, bw = W / 20;
  return s('svg', {class: 'acc-hist', viewBox: `0 0 ${W} ${H + 12}`, role: 'img', 'aria-label': `Suggestions by how sure Jev is; ${fmt(arr.slice(accBucket(t)).reduce((a, b) => a + b, 0))} at ${lvlText(t)} or more`},
    arr.map((n, i) => s('rect', {x: i * bw + 0.5, y: H - (n / max) * H, width: bw - 1, height: Math.max(n ? 1 : 0, (n / max) * H), class: i >= accBucket(t) ? 'on' : 'off'},
      s('title', null, `${i * 5}–${(i + 1) * 5} % sure: ${fmt(n)}`))),
    applied != null ? s('line', {x1: accBucket(applied) * bw, x2: accBucket(applied) * bw, y1: 0, y2: H, class: 'applied'}) : null,
    s('line', {x1: accBucket(t) * bw, x2: accBucket(t) * bw, y1: 0, y2: H, class: 'mark'}),
    s('text', {x: 0, y: H + 11, class: 'ax'}, '0 %'), s('text', {x: 10 * bw, y: H + 11, class: 'ax', 'text-anchor': 'middle'}, '50 %'),
    s('text', {x: W, y: H + 11, class: 'ax', 'text-anchor': 'end'}, '100 %'));
}
function accExampleRows(f, rows, pass) {
  if (!rows) return h('div', {class: 'small muted'}, '…');
  if (!rows.length) return h('div', {class: 'small muted'}, pass ? 'No message is accepted at this level.' : 'No message is just below the line.');
  return h('div', {class: 'acc-ex'}, rows.map(r => {
    const row = h('button', {class: 'acc-exrow', 'data-pane-key': 'm:' + r.id, title: `Open this message (Jev is ${lvlText(r.confidence)} sure)`,
      onclick: () => openMessage(r.id, {origin: row})},
      h('span', {class: 'acc-exp num'}, lvlText(r.confidence)),
      h('span', {class: 'acc-ext'}, h('b', null, r.subject || '(no subject)'), h('small', null, `${r.from_name || r.from_address || '?'} · ${day(r.received_at)}`)),
      h('span', {class: 'pill ' + (pass ? 'ac' : 'prop')}, accLabel(f, r.value)));
    return row;
  }));
}
function accLoadExamples(f, box) {
  clearTimeout(ACC.timers[f.id]);
  ACC.timers[f.id] = setTimeout(async () => {
    const t = ACC.t[f.id];
    try {
      const ex = await api(`/api/acceptance/examples?field=${encodeURIComponent(f.id)}&t=${t}`);
      if (ACC.t[f.id] !== t) return;
      fill(box, h('div', {class: 'acc-exg'},
        h('div', null, h('h4', null, 'Accepted at this level'), h('p', {class: 'acc-exsub'}, 'The least sure answers that still pass the line.'), accExampleRows(f, ex.over, true)),
        h('div', null, h('h4', null, 'Just below the line (stays a suggestion)'), h('p', {class: 'acc-exsub'}, 'The surest answers that do not pass it.'), accExampleRows(f, ex.under, false))));
      paneMark();
    } catch (e) { fill(box, h('div', {class: 'err'}, errText(e))); }
  }, 300);
}
function accFieldCard(st, f, cards) {
  const byId = Object.fromEntries(st.fields.map(x => [x.id, x]));
  const b = f.boundary ? byId[f.boundary] : null;
  const cases = st.gold ? st.gold.fields[f.id] || [] : [];
  const [lo, hi] = st.slider;
  const applied = st.applied.thresholds[f.id] != null ? Number(st.applied.thresholds[f.id]) : null;
  const at = x => Math.min(1, Math.max(0, (x - lo) / (hi - lo)));
  const out = h('output', {class: 'acc-t num'});
  const say = h('p', {class: 'acc-say', id: 'acc-say-' + f.id});
  const state = h('div', {class: 'acc-state'});
  const slider = h('input', {type: 'range', min: String(lo), max: String(hi), step: '0.05', value: String(ACC.t[f.id]),
    'aria-label': `How sure Jev must be: ${f.label}`, 'aria-describedby': say.id,
    oninput: e => { ACC.t[f.id] = Number(e.target.value); ACC.preview = null; redraw(); if (cards[f.id + ':dep']) cards[f.id + ':dep'](); if (ACC.pending) ACC.pending(); }});
  const range = h('div', {class: 'acc-range', style: applied != null ? `--at:${at(applied)}` : null}, slider,
    applied != null ? h('span', {class: 'acc-applied', title: `In use now: ${lvlText(applied)}`}, h('span', null, `in use ${lvlText(applied)}`)) : null);
  const nums = h('div', {class: 'acc-nums'}), hist = h('div', {class: 'acc-histbox'}), conf = h('div', {class: 'acc-conf'}), ex = h('div', {class: 'acc-exbox'});
  function redraw(examples = true) {
    const t = ACC.t[f.id];
    out.value = lvlText(t);
    slider.setAttribute('aria-valuetext', lvlText(t));
    const sc = accScore(cases, t, f.kind === 'exact' ? st.margin : null, b ? ACC.t[b.id] : null);
    const ar = accArchive(st, f, t);
    const serious = f.kind === 'exact' ? sc.costly : sc.decided - sc.right;
    fill(say, accSentence(f, b, t, sc, ar, !!st.gold));
    fill(state, applied == null ? [h('span', {class: 'pill'}, 'not applied'), ' Nothing is accepted for this field until you press Apply.']
      : Math.abs(applied - t) < 1e-9 ? [h('span', {class: 'pill ok'}, 'in use'), ' This is the level Talos uses now.']
      : [h('span', {class: 'pill md'}, 'trying'), ` Talos still uses ${lvlText(applied)}. Nothing changes until you press Apply.`]);
    const kpi = (l, v, n, cls, tip) => h('div', {class: 'kpi' + (cls ? ' ' + cls : ''), title: tip || null}, h('div', {class: 'l'}, l), h('div', {class: 'v'}, v), h('div', {class: 'n'}, n));
    fill(nums,
      h('div', {class: 'acc-grp'}, h('div', {class: 'acc-grp-h'}, 'On the answer key'),
        h('div', {class: 'acc-grp-k'},
          kpi('Get a value', pctText(sc.coverage), sc.n ? `${fmt(sc.decided)} of ${fmt(sc.n)} messages` : 'no answer key yet', null,
            'Of the answer-key messages, the share where Jev is at least this sure'),
          kpi('Right', pctText(sc.accuracy), sc.decided ? `${fmt(sc.right)} of the ${fmt(sc.decided)} that get one` : 'none get a value', null,
            'Of those that get a value, the share where Jev agrees with the answer key'),
          kpi('Serious mistakes', fmt(serious), `mix up ${accMix(f, b)}`, serious ? 'bad' : '', accSerious(f, b)))),
      h('div', {class: 'acc-grp'}, h('div', {class: 'acc-grp-h'}, 'In your archive'),
        h('div', {class: 'acc-grp-k'},
          kpi('Messages in your archive', ar ? fmt(ar.n) : '—', ar ? `get a value, of ${fmt(ar.total)} with a suggestion` : 'no suggestions yet', null,
            f.kind === 'exact' ? 'Up to this many: a value its boundary disagrees with is held back when you apply' : 'Messages whose suggestion reaches the line'))),
      h('p', {class: 'acc-serious'}, h('b', null, 'Serious here: '), accSerious(f, b)));
    fill(hist, st.archive[f.id] ? [accHistogram(st, f, t, applied),
      h('div', {class: 'acc-histcap'}, 'How sure Jev is across your archive. Bars from the line up are accepted', applied != null ? '; the dashed line is the level in use.' : '.')] : null);
    fill(conf, sc.confusions.length ? [h('span', {class: 'small muted'}, 'Most common mistakes at this level (Jev said → the answer key says): '),
      sc.confusions.map(([k, n]) => { const [p, r] = k.split('→'); return h('span', {class: 'pill mono'}, `${accLabel(f, p)} → ${accLabel(f, r)} ×${n}`); })]
      : h('span', {class: 'small muted'}, sc.decided ? 'No mistakes on the answer key at this level.' : ''));
    if (examples) accLoadExamples(f, ex);
  }
  cards[f.id] = redraw;
  if (b) cards[b.id + ':dep'] = () => cards[f.id](false);   // the agreement rule: the boundary's line moves this one's numbers
  redraw();
  const sub = f.kind === 'kind'
    ? `How you read mail, in ${Object.keys(f.sides || {}).length} kinds (${Object.values(f.sides || {}).join(', ').toLowerCase()}). Worked out from Jev's answer for type: a kind is as sure as the types under it together. An accepted kind also holds back a type of another kind.`
    : f.kind === 'boundary'
    ? `${Object.values(f.sides || {}).join(' or ')}. Worked out from Jev's answer for ${f.from}.`
    : `The exact ${String(f.label).toLowerCase()}. Accepted only when Jev is at least this sure, its answer leads the next best by ${Math.round(100 * st.margin)} points, and it agrees with its boundary${b ? ` (${Object.values(b.sides || {}).join(' or ').toLowerCase()})` : ''}.`;
  return h('section', {class: 'card acc-card'},
    h('div', {class: 'card-h'}, h('div', null, h('h2', null, f.label), h('p', null, sub)),
      applied != null ? h('span', {class: 'pill ok', title: 'The level Talos uses now'}, `in use ${lvlText(applied)}`) : h('span', {class: 'pill'}, 'not applied')),
    h('div', {class: 'acc-slide'}, h('span', {class: 'small muted'}, lvlText(lo)), range, h('span', {class: 'small muted'}, lvlText(hi)), out),
    state, say,
    h('div', {class: 'acc-top'}, nums, hist), conf, ex);
}
async function accApply(box, really) {
  if (ACC.busy) return;
  ACC.busy = true;
  fill(box, h('span', {class: 'small muted'}, really ? 'Applying…' : 'Working out what would change…'));
  try {
    const res = await post('/api/acceptance/apply', {thresholds: ACC.t, dry_run: !really}, !really);
    ACC.busy = false;
    if (really) { flash('Applied: ' + Object.entries(res.fields).map(([f, d]) => `${f} ${fmt(d.active)}`).join(', ')); ACC.preview = null; render(); return; }
    ACC.preview = res;
    const th = (l, tip) => h('th', {class: 'r', title: tip}, l);
    fill(box, h('div', {class: 'acc-preview'},
      h('p', null, h('b', null, 'Apply these levels?'), ' Jev\'s answers from the line up become accepted values (solid in Messages); the rest stay suggestions (dashed). Rules and your own values still win. You can undo it with talos enrich unaccept --run all.'),
      h('div', {class: 'tw'}, h('table', {class: 't'}, h('thead', null, h('tr', null, h('th', null, 'Field'), th('Level', 'How sure Jev must be'), th('Newly accepted', 'Suggestions that become accepted values'),
          th('Back to suggestion', 'Accepted values that fall under the new line'), th('Held back', 'Over the line, but on the other side of its boundary: stays a suggestion'), th('Accepted after', 'Accepted values for this field after applying'))),
        h('tbody', null, Object.entries(res.fields).map(([f, d]) => h('tr', null, h('td', null, (ACC.st.fields.find(x => x.id === f) || {label: f}).label), h('td', {class: 'r'}, lvlText(d.threshold)),
          h('td', {class: 'r'}, fmt(d.promoted)), h('td', {class: 'r'}, fmt(d.demoted)), h('td', {class: 'r'}, fmt(d.held_back)), h('td', {class: 'r'}, fmt(d.active))))))),
      h('div', {class: 'dactions'}, h('button', {class: 'btn primary', onclick: () => accApply(box, true)}, 'Apply now'),
        h('button', {class: 'btn', onclick: () => { ACC.preview = null; fill(box, accApplyButton(box)); }}, 'Cancel'))));
  } catch (e) { ACC.busy = false; fill(box, h('div', {class: 'err'}, errText(e)), accApplyButton(box)); }
}
const accApplyButton = box => h('button', {class: 'btn primary', onclick: () => accApply(box, false)}, 'Apply these levels…');
// What the answer key is, for the reader who has not built one: the yardstick the page measures with.
function accKeyExplainer(n) {
  return h('details', {class: 'acc-key'}, h('summary', null, 'What is the answer key?'),
    h('p', null, 'A fixed sample of about 300 of your messages, labelled by Claude and checked by you', n ? ` (${fmt(n)} of them are in the run chosen here)` : '', '. ',
      'Talos asked Jev about the same messages and compares its answers with these labels. That is how this page knows how often Jev is right at each level: the answer key is the yardstick.'),
    h('p', null, 'The counts for your whole archive come from Jev\'s suggestions for every message; only the answer key says whether they are right.'));
}
async function viewAccept() {
  const st = await api('/api/acceptance' + (ACC.goldRun ? '?gold_run=' + encodeURIComponent(ACC.goldRun) : ''));
  ACC.st = st;
  for (const f of st.fields) if (ACC.t[f.id] == null) ACC.t[f.id] = Number(st.applied.thresholds[f.id] ?? f.default);
  const cards = {};
  const bounds = st.fields.filter(f => f.kind === 'boundary'), exact = st.fields.filter(f => f.kind === 'exact');
  const kinds = st.fields.filter(f => f.kind === 'kind');
  const applyBox = h('div', {class: 'acc-apply'});
  fill(applyBox, accApplyButton(applyBox));
  const goldSel = h('select', {class: 'sel', 'aria-label': 'Answer-key run', onchange: e => { ACC.goldRun = e.target.value; render(); }},
    st.gold_runs.map(r => h('option', {value: r.id}, `${r.id} (${r.unit}, templates v${r.template_version})`)));
  goldSel.value = st.gold_run || '';
  const ap = st.applied;
  const keyN = st.gold ? new Set(Object.values(st.gold.fields).flat().map(x => x.i)).size : 0;
  // Nothing changes until Apply: a bar at the bottom says so while any slider is off the level in use.
  const pending = h('div', {class: 'acc-pending', role: 'status'});
  ACC.pending = () => {
    const changed = st.fields.filter(f => ap.thresholds[f.id] == null || Math.abs(Number(ap.thresholds[f.id]) - ACC.t[f.id]) > 1e-9).length;
    pending.hidden = !changed;
    fill(pending, h('span', null, h('b', null, ap.at ? `${changed} ${changed === 1 ? 'level differs' : 'levels differ'} from what Talos uses.` : 'Nothing is applied yet.'),
        ' Nothing changes until you press Apply.'),
      h('span', {class: 'acc-pending-b'},
        h('button', {class: 'btn primary sm', onclick: () => { applyBox.scrollIntoView({block: 'center', behavior: 'smooth'}); accApply(applyBox, false); }}, 'Apply these levels…'),
        ap.at ? h('button', {class: 'btn ghost sm', onclick: () => { for (const f of st.fields) if (ap.thresholds[f.id] != null) ACC.t[f.id] = Number(ap.thresholds[f.id]); ACC.preview = null; render(); }}, 'Back to the levels in use') : null));
  };
  ACC.pending();
  return h('div', {class: 'acc-page'},
    header(withHelp('Acceptance', 'acceptance'), 'How sure Jev must be before Talos accepts its answer. Nothing changes until you press Apply.'),
    h('div', {class: 'grid g2'},
      card('How it works', null, null,
        h('ol', {class: 'acc-steps'},
          h('li', null, h('b', null, 'Jev answers, and says how sure it is. '), 'For every message and every field, Jev (the model) gives an answer and a confidence from 0 to 100 %.'),
          h('li', null, h('b', null, 'The slider sets the line. '), 'Talos accepts Jev\'s answer only when Jev is at least that sure. Below the line the answer stays a suggestion (dashed in Messages) until you, a rule or a later run decide.'),
          h('li', null, h('b', null, 'Higher means fewer, but more of them right. '), 'Raise the line and fewer messages get a value, but more of those are right. Lower it and more get one, with more mistakes.'),
          h('li', null, h('b', null, 'Two kinds of fields. '), 'The four boundaries are the coarse splits that are costly to get wrong: person or machine, work or personal, conversation or other, keep or short-lived. The exact values say more (which kind of machine, which topic); they can use a lower line, but are accepted only when they agree with their boundary.')),
        accKeyExplainer(keyN)),
      card('In use now', ap.at ? `Last applied ${new Date(ap.at).toLocaleString('sv-SE')}` : 'Nothing applied yet: every answer from Jev is still a suggestion', null,
        ap.at ? h('div', {class: 'chips'}, st.fields.map(f => h('span', {class: 'pill ' + (ap.thresholds[f.id] != null ? 'ok' : '')}, `${f.label} ${ap.thresholds[f.id] != null ? lvlText(ap.thresholds[f.id]) : '—'}`))) : null,
        h('p', {class: 'small muted', style: 'margin:10px 0'}, 'The marker on each slider shows the level in use. Moving a slider only shows what a level would give; nothing changes until you press Apply.'),
        h('label', {class: 'fsel acc-gold'}, h('span', {class: 'flabel'}, 'Answer key run (the yardstick)'), goldSel),
        h('div', {class: 'small muted', style: 'margin:6px 0 10px'}, `${fmt(st.messages)} messages in your archive; Jev's suggestions come from ${st.runs.length} run${st.runs.length === 1 ? '' : 's'}.`),
        h('div', {class: 'toolbar'}, applyBox,
          h('button', {class: 'btn ghost', title: 'Boundaries at 90 %, kind at 85 %, exact values at 70 %', onclick: () => { for (const f of st.fields) ACC.t[f.id] = f.default; ACC.preview = null; render(); }}, 'Recommended levels')))),
    st.gold ? null : note('There is no answer-key run yet, so the page cannot say how often Jev is right. Make one with: talos enrich jev gold --set 1 --unit context'),
    h('h3', {class: 'acc-h'}, 'The four boundaries: the costly mistakes'),
    h('div', {class: 'acc-list'}, bounds.map(f => accFieldCard(st, f, cards))),
    kinds.length ? [h('h3', {class: 'acc-h'}, 'Kind: how you read mail'), h('div', {class: 'acc-list'}, kinds.map(f => accFieldCard(st, f, cards)))] : null,
    h('h3', {class: 'acc-h'}, 'Exact values'),
    h('div', {class: 'acc-list'}, exact.map(f => accFieldCard(st, f, cards))),
    pending);
}

// ---------------------------------------------------------------- unlocking the labelling (Tune › Jobs & fruit)
// Low-hanging fruit (talos.unlock): sender groups of undecided machine mail, ranked by what one answer
// for a representative would unlock. "Label these 10" makes a small answer key of them and opens it;
// "Let Claude label these" writes it blind to a file and shows the import command (no model is called);
// "Apply to the groups" gives the finished answers to every message, a dry run first. Improvement jobs:
// focused Jev runs per field, ranked by the messages they could improve, each with its commands; the
// page runs nothing, and a job marked done or dismissed is no longer shown. Both come from a cache the
// server refreshes behind the page, so a load never scans the archive.
const UNLOCK_FIELD = {kind: 'kind', topic: 'topic', value: 'value', sender_kind: 'sender', keep: 'keep'};
const UNLOCK = {msg: null, jobsHidden: false};
const cmdBlock = (text, cmd) => h('div', {class: 'ustep'}, h('div', {class: 'small'}, text), cmd ? h('pre', {class: 'cmds'}, cmd) : null);
function unlockBar(n, max) {
  return h('span', {class: 'ubar', title: `${fmt(n)} messages`}, h('span', {class: 'ubar-t'}, h('span', {class: 'ubar-f', style: `width:${Math.max(2, 100 * n / Math.max(1, max))}%`})), h('b', null, fmt(n)));
}
// While the server computes an answer behind the page, look again in a few seconds (once per path).
const UNLOCK_POLL = new Set();
// It asks quietly and redraws only once a new answer has arrived, never on every look: a
// computation can take half a minute, and redrawing each time made the page restless.
function pollWhileRefreshing(d, path) {
  if (!(d.stale || d.refreshing) || UNLOCK_POLL.has(path)) return;
  UNLOCK_POLL.add(path);
  const view = S.view;
  // Soon at first (most answers are counted again in a second or two), then less and less often.
  let wait = 1500;
  const look = () => {
    if (S.view !== view) { UNLOCK_POLL.delete(path); return; }
    fetchJSON(path).then(fresh => {
      if (fresh.stale || fresh.refreshing) { wait = Math.min(8000, wait * 2); setTimeout(look, wait); return; }
      UNLOCK_POLL.delete(path);
      CACHE.set(path, {data: fresh, at: Date.now(), json: JSON.stringify(fresh)});
      if (S.view === view) render(true);
    }).catch(() => UNLOCK_POLL.delete(path));
  };
  setTimeout(look, wait);
}
function cacheFoot(d, refresh) {
  return h('div', {class: 'ufoot small muted'},
    d.computed_at ? `Computed ${when(d.computed_at)}${d.seconds != null ? ` in ${d.seconds} s` : ''}` : '',
    d.stale || d.refreshing ? h('span', {class: 'pill md'}, 'refreshing behind the page') : null,
    h('button', {class: 'linkbtn', onclick: refresh}, 'Refresh now'));
}
function exportBox(x) {
  const c = x.commands || {};
  return h('div', {class: 'ubox'},
    h('b', null, `Answer key ${x.set_id}: ${fmt(x.count || '')} items written blind for Claude`),
    h('dl', {class: 'kv small'}, h('dt', null, 'Items'), h('dd', null, h('code', null, x.items)),
      h('dt', null, 'Options'), h('dd', null, h('code', null, x.options)),
      h('dt', null, 'Claude writes'), h('dd', null, h('code', null, x.labels))),
    cmdBlock('Then import Claude’s labels:', c.import),
    cmdBlock('And give them to the groups (a dry run first): ' + (c.apply || ''), null),
    h('div', {class: 'small muted'}, 'No model was called. Nothing is labelled until the import.'));
}
function unlockSetRow(st, msg) {
  const complete = st.done >= st.items, claude = st.claude_done >= st.items;
  const who = complete ? OWNER.id : claude ? 'claude' : null;
  const out = h('div', {class: 'uset-msg'});
  const apply = async () => {
    fill(out, h('span', {class: 'muted small'}, 'Counting (a dry run, nothing is written)…'));
    try {
      const d = await post(`/api/unlock/sets/${st.id}/apply`, {dry_run: true, labeller: who});
      fill(out, h('div', {class: 'ubox'},
        h('div', null, `A dry run: ${fmt(d.written)} values from ${who === 'claude' ? 'Claude’s' : 'your'} answers, and ${fmt(d.sides_written)} sides (sender, keep), for ${fmt(d.messages)} messages in ${d.items.length} groups.`),
        d.kept_human ? h('div', {class: 'small muted'}, `${fmt(d.kept_human)} messages keep a decision of your own.`) : null,
        d.items.filter(i => i.mixed).length ? h('div', {class: 'small muted'}, `${d.items.filter(i => i.mixed).length} group(s) ticked mixed: nothing given to them.`) : null,
        h('div', {class: 'uacts'},
          h('button', {class: 'btn sm primary', onclick: async () => {
            try { const r = await post(`/api/unlock/sets/${st.id}/apply`, {dry_run: false, labeller: who});
              fill(out, h('div', {class: 'ubox ok'}, `Given to the groups: ${fmt(r.written + r.sides_written)} values for ${fmt(r.messages)} messages.`));
              setTimeout(() => render(true), 1200);
            } catch (e) { fill(out, h('div', {class: 'err'}, errText(e))); }
          }}, 'Apply to the groups'),
          h('button', {class: 'btn sm', onclick: () => fill(out)}, 'Cancel'))));
    } catch (e) { fill(out, h('div', {class: 'err'}, errText(e))); }
  };
  const exp = async () => {
    try { fill(out, exportBox(await post(`/api/unlock/sets/${st.id}/export`, {}))); } catch (e) { fill(out, h('div', {class: 'err'}, errText(e))); }
  };
  return h('div', {class: 'uset'},
    h('div', {class: 'uset-h'},
      h('span', null, h('b', null, `Answer key ${st.id}`), h('span', {class: 'small muted'}, ` · ${fmt(st.messages)} messages · ${st.fields.join(', ')}`)),
      h('span', {class: 'small'}, `${st.done}/${st.items} labelled`, st.claude_done ? ` · Claude ${st.claude_done}/${st.items}` : ''),
      st.applied ? h('span', {class: 'pill ok', title: `By ${st.applied.labeller}, ${fmt(st.applied.written)} values`}, `applied ${when(st.applied.at)}`) : null,
      h('span', {class: 'uacts'},
        h('button', {class: 'btn sm', onclick: () => { GOLD.set = st.id; GOLD.pos = null; GOLD.summary = null; go('gold'); }}, st.done ? 'Continue' : 'Open'),
        h('button', {class: 'btn sm', title: 'Write the items blind to a file for Claude, with the import command', onclick: exp}, 'Export for Claude'),
        h('button', {class: 'btn sm' + (who && !st.applied ? ' primary' : ''), disabled: !who, title: who ? 'A dry run first' : 'Every item needs every field first',
          onclick: apply}, st.applied ? 'Apply again' : 'Apply to the groups'))),
    msg && msg.set_id === st.id ? exportBox(msg) : null, out);
}
function fruitCard(d) {
  pollWhileRefreshing(d, '/api/unlock/fruit');
  const top = d.top || [];
  const max = Math.max(1, ...top.map(g => g.messages));
  const status = h('div', {'aria-live': 'polite'});
  const make = async claude => {
    fill(status, h('span', {class: 'muted small'}, 'Drawing the answer key…'));
    try {
      const r = await post('/api/unlock/sets', {top: 10, claude});
      if (!claude) { GOLD.set = r.set_id; GOLD.pos = null; GOLD.summary = null; go('gold'); return; }
      UNLOCK.msg = {...r.export, set_id: r.set_id};
      render(true);
    } catch (e) { fill(status, h('div', {class: 'err'}, errText(e))); }
  };
  return card('Low-hanging fruit', top.length ?
      `Label one message per group and ${fmt(d.top_messages)} undecided messages get their values. ${fmt(d.undecided)} machine-side messages are undecided; ${fmt(d.grouped)} of them are in ${fmt(d.groups)} sender groups.` :
      'Sender groups of undecided machine mail, ranked by what one answer would unlock.',
    top.length ? h('div', {class: 'uacts'},
      h('button', {class: 'btn sm primary', title: 'A small answer key of these groups’ representatives, opened for you', onclick: () => make(false)}, `Label these ${top.length}`),
      h('button', {class: 'btn sm', title: 'The same answer key, written blind to a file for Claude; no model is called', onclick: () => make(true)}, 'Let Claude label these')) : null,
    status,
    top.length ? h('table', {class: 't ufruit'},
      h('thead', null, h('tr', null, h('th', null, 'Sender · system'), h('th', null, 'Unlocks'), h('th', null, 'Missing'), h('th', null, 'For example'))),
      h('tbody', null, top.map(g => h('tr', {class: 'click', tabindex: '0', title: 'Open its representative',
          onclick: () => openMessage(g.rep), onkeydown: e => { if (e.key === 'Enter') openMessage(g.rep); }},
        h('td', {class: 'nm'}, acctDot(g.account), ' ', g.sender, g.system ? h('span', {class: 'pill ac', style: 'margin-left:6px'}, g.system) : null, g.name ? h('small', null, g.name) : null),
        h('td', null, unlockBar(g.messages, max)),
        h('td', null, h('div', {class: 'umiss'}, Object.keys(UNLOCK_FIELD).filter(f => g.missing[f]).map(f => [f, g.missing[f]]).map(([f, n]) => h('span', {class: 'pill' + (n >= g.messages / 2 ? ' md' : ''), title: `${fmt(n)} of ${fmt(g.messages)} have no ${f}`}, `${UNLOCK_FIELD[f] || f} ${fmt(n)}`)))),
        h('td', {class: 'small muted uex'}, g.examples.map(e => h('div', null, e.subject || '(no subject)'))))))) :
      empty('Nothing to unlock', 'No sender group of five or more undecided machine messages is left outside an answer key.'),
    UNLOCK.msg && !(d.sets || []).some(s => s.id === UNLOCK.msg.set_id) ? exportBox(UNLOCK.msg) : null,
    (d.sets || []).length ? h('div', {class: 'usets'}, h('h4', null, 'Your unlock answer keys'), d.sets.map(st => unlockSetRow(st, UNLOCK.msg))) : null,
    cacheFoot(d, async () => { CACHE.clear(); await api('/api/unlock/fruit?refresh=1'); render(true); }));
}
const JOB_FIELD_TEXT = {kind: 'Kind', topic: 'Topic', value: 'Value', origin: 'Origin', route: 'Route', ask: 'Asks me', sender_kind: 'Sender (people or machine)', keep: 'Worth keeping'};
function jobRow(j, max) {
  const mark = async state => { try { await post('/api/unlock/jobs/state', {key: j.key, state}); render(true); } catch (e) { alert(errText(e)); } };
  const c = j.confusions;
  const facts = j.kind === 'claude' ? [`${j.groups} sender groups under Low-hanging fruit`, 'no API cost here: a Claude session labels the file'] : j.combined ? [
    `${fmt(j.cases)} cases instead of ${fmt(j.separate_cases)}`, `${fmt(j.asks)} field questions`,
    `about ${fmt(j.tokens_per_case)} tokens a case (${fmt(j.separate_tokens_per_case)} one field at a time)`,
    `$${Number(j.cost_usd || 0).toFixed(2)} instead of $${Number(j.separate_cost_usd || 0).toFixed(2)}`,
    `about ${j.minutes} min`, j.unchecked && j.unchecked.length ? `answer key first: ${j.unchecked.join(', ')}` : null].filter(Boolean) : [
    `${fmt(j.undecided)} undecided`,
    j.near_band ? `${fmt(j.near)} just under the level (${j.near_band[0].toFixed(2)}–${j.near_band[1].toFixed(2)})` : null,
    `${fmt(j.cases)} cases`, `about ${fmt(j.tokens_per_case)} tokens a case (${j.tokens_how})`,
    `$${Number(j.cost_usd || 0).toFixed(2)}`, j.minutes != null ? `about ${j.minutes} min` : null].filter(Boolean);
  return h('div', {class: 'ujob' + (j.state ? ' done' : '')},
    h('div', {class: 'ujob-h'},
      h('div', null, h('b', null, j.kind === 'claude' || j.combined ? j.label : `Focused Jev run: ${JOB_FIELD_TEXT[j.field] || j.field}`),
        h('span', {class: 'pill' + (j.kind === 'claude' ? ' ac' : ''), style: 'margin-left:6px'}, j.kind === 'claude' ? 'Claude' : 'Jev'),
        j.field === 'keep' ? h('span', {class: 'small muted'}, ' · asked as value (keep is its side)') : null,
        j.combined ? h('span', {class: 'small muted'}, ' · ' + j.fields.map(f => JOB_FIELD_TEXT[f] || f).join(', ') + ': each message asked once, only what it is unsure of') : null,
        j.state ? h('span', {class: 'pill ok', style: 'margin-left:6px'}, j.state) : null),
      h('div', null, unlockBar(j.messages || 0, max), h('span', {class: 'small muted'}, ' could improve'))),
    j.error ? h('div', {class: 'err small'}, j.error) : h('div', {class: 'small sec'}, facts.join(' · ')),
    c && c.groups && c.groups.length ? h('div', {class: 'small'}, 'Confused on the answer key: ',
      c.groups.map((g, i) => [i ? '; ' : '', h('b', null, g.labels.join(' vs ')), ` (${g.items})`]),
      c.items ? h('span', {class: 'muted'}, ` · Jev right on ${c.right} of ${c.items}`) : null) : null,
    h('details', {class: 'ujob-d'}, h('summary', null, 'The commands (you run them; the dry run first)'),
      (j.steps || []).map(s => cmdBlock(s.text, s.command))),
    h('div', {class: 'uacts'},
      j.state ? h('button', {class: 'btn sm', onclick: () => mark(null)}, 'Show again') : [
        h('button', {class: 'btn sm', title: 'You ran it: take it off the list', onclick: () => mark('done')}, 'Done'),
        h('button', {class: 'btn sm ghost', title: 'Not worth it: take it off the list', onclick: () => mark('dismissed')}, 'Dismiss')]));
}
function jobsCard(d) {
  pollWhileRefreshing(d, '/api/unlock/jobs' + (UNLOCK.jobsHidden ? '?hidden=1' : ''));
  const jobs = d.jobs || [];
  const max = Math.max(1, ...jobs.map(j => j.messages || 0));
  return card('Improvement jobs', d.note || 'Focused Jev (or Claude) jobs that would decide or firm up values, ranked by the messages they could improve. Nothing runs from here.',
    d.hidden ? h('button', {class: 'btn sm', 'aria-pressed': String(UNLOCK.jobsHidden), onclick: () => { UNLOCK.jobsHidden = !UNLOCK.jobsHidden; render(true); }},
      UNLOCK.jobsHidden ? 'Hide done' : `${d.hidden} done or dismissed`) : null,
    jobs.length ? h('div', {class: 'ujobs'}, jobs.map(j => jobRow(j, max))) : empty('No jobs', d.note || 'Nothing to improve right now.'),
    cacheFoot(d, async () => { CACHE.clear(); await api('/api/unlock/jobs?refresh=1'); render(true); }));
}
Object.assign(WIDGET_DATA, {
  fruit: () => api('/api/unlock/fruit'),
  jobs: () => api('/api/unlock/jobs' + (UNLOCK.jobsHidden ? '?hidden=1' : '')),
});
Object.assign(WIDGET_VIEW, {fruit: fruitCard, jobs: jobsCard});

// ---- Tune › Jobs & fruit: the two widgets that used to close the Overview, on a page of their own.
async function viewJobs() {
  const [fruit, jobs] = await Promise.all([WIDGET_DATA.fruit().catch(e => ({__error: errText(e)})), WIDGET_DATA.jobs().catch(e => ({__error: errText(e)}))]);
  const or = (d, title, draw) => d.__error ? card(title, null, null, h('div', {class: 'err'}, d.__error)) : draw(d);
  return h('div', null,
    header(withHelp('Jobs & fruit', 'fruit'), 'What would decide the most undecided mail: sender groups to label, and focused runs worth making'),
    h('div', {class: 'vstack', style: 'margin-top:16px'}, or(fruit, 'Low-hanging fruit', fruitCard), or(jobs, 'Improvement jobs', jobsCard)));
}

// ---------------------------------------------------------------- the Studio (Tune › Studio)
// docs/studio.md. One card at a time: a group of mail Jev is unsure of, Jev's guess already ticked on
// each line. Enter confirms; a number (or typed letters, then Enter) ticks another value; 0 says "not
// this"; ↑↓ move between lines; → skips; ← takes the last decision back; "." narrows it to the one
// message shown. The answer goes on the whole group, and the counter shows what it settled. The next
// cards are fetched while the owner decides (the server builds one in about two seconds).
const ST = {queue: [], done: [], loading: false, seen: 0, line: 0, picks: {}, filter: '', scope: 'group', busy: false,
  since: new Date().toISOString(), stats: null, opts: {}, levels: [], cells: [], pool: null, root: null, box: null,
  side: null, count: null, shown: 0, burst: null, note: ''};
const stLevelMeta = k => (ST.levels.find(x => x.key === k) || {label: k || '', meaning: ''});
const stLevelTag = (k, conf) => k ? h('span', {class: 'lv lv-' + k, title: stLevelMeta(k).meaning + (conf != null ? ` (${Math.round(conf * 100)}%)` : '')}, stLevelMeta(k).label) : null;
const stLabel = (f, v) => v == null ? '' : ((ST.opts[f] || []).find(o => o.value === v) || {}).label || String(v).replace(/_/g, ' ');
const stSide = s => s === 'machine' ? 'machine mail' : 'people’s mail';
const stMotion = () => !(document.documentElement.dataset.motion === 'off' || document.documentElement.dataset.motion === 'reduced'
  || (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches));

async function stFill() {
  if (ST.loading || ST.queue.length >= 3) return;
  ST.loading = true;
  try {
    const p = new URLSearchParams({n: ST.queue.length ? '2' : '1', position: String(ST.seen + ST.queue.length)});
    ST.queue.concat(ST.done.slice(-1)).forEach(c => p.append('exclude', c.key));
    const d = await fetchJSON('/api/studio/next?' + p);
    ST.opts = d.options || ST.opts; ST.levels = d.levels || ST.levels; ST.cells = d.cells || ST.cells; ST.pool = d.pool || ST.pool;
    const known = new Set(ST.queue.map(c => c.key));
    const fresh = (d.cards || []).filter(c => !known.has(c.key));
    const wasEmpty = !ST.queue.length;
    ST.queue.push(...fresh);
    ST.loading = false;
    if (wasEmpty && ST.queue.length) stReset();
    if (wasEmpty) stDraw();
    else stDrawSide();
    if (!fresh.length && ST.pool && ST.pool.refreshing && !ST.queue.length) setTimeout(() => { if (ST.root && ST.root.isConnected) stFill(); }, 3000);
    else if (ST.queue.length < 3 && fresh.length) stFill();
  } catch (e) {
    ST.loading = false;
    ST.note = errText(e);
    stDraw();
  }
}
async function stLoadStats() {
  try { ST.stats = await fetchJSON('/api/studio/stats?since=' + encodeURIComponent(ST.since)); } catch (e) { ST.stats = ST.stats || null; }
  stDrawCount(); stDrawSide();
}
// A new card's picks: Jev's value ticked on every line the owner's own and the rules' values do not decide;
// the focus on the first line still open (a check card: the line it checks).
function stReset() {
  const c = ST.queue[0];
  ST.picks = {}; ST.filter = ''; ST.scope = 'group';
  if (!c) return;
  c.lines.forEach(ln => { if (ln.value && !['human', 'rule'].includes(ln.source)) ST.picks[ln.field] = ln.value; });
  const focus = c.kind === 'check' && c.cell ? c.lines.findIndex(ln => ln.field === c.cell.field) : c.lines.findIndex(ln => !ln.done);
  ST.line = Math.max(0, focus);
}
// A short list (people or machine, kind, value) shows every value, Jev's first; a long one (topic)
// Jev's value and the ones it weighed, and typing finds the rest.
function stChoices(ln) {
  const out = [], all = (ST.opts[ln.field] || []).map(o => o.value);
  const short = all.length && all.length <= 12;
  const picked = ST.picks[ln.field];
  [ln.value, picked, ...(ln.alternatives || []), ...(short ? all : [])].forEach(v => { if (v && !out.includes(v) && out.length < (short ? 12 : 6)) out.push(v); });
  if (ST.filter && ST.queue[0] && ST.queue[0].lines[ST.line] === ln) {
    const q = ST.filter.toLowerCase();
    return (ST.opts[ln.field] || []).filter(o => [o.label, o.value, o.family].some(x => String(x || '').toLowerCase().includes(q)))
      .slice(0, 9).map(o => o.value);
  }
  return out;
}
function stPick(i, v) {
  const c = ST.queue[0];
  if (!c) return;
  const ln = c.lines[i];
  ST.picks[ln.field] = ST.picks[ln.field] === v && v !== ln.value ? ln.value || undefined : v;
  ST.filter = '';
  // on to the next line that is still open
  const next = c.lines.findIndex((x, k) => k > i && !x.done);
  if (next >= 0) ST.line = next;
  stDrawCard();
}
function stFocus(i) { const c = ST.queue[0]; if (!c) return; ST.line = Math.max(0, Math.min(c.lines.length - 1, i)); ST.filter = ''; stDrawCard(); }

async function stDecide() {
  const c = ST.queue[0];
  if (!c || ST.busy) return;
  const lines = Object.entries(ST.picks).filter(([, v]) => v !== undefined).map(([field, value]) => ({field, value}));
  if (!lines.length) return stSkip();
  ST.busy = true;
  stDrawCard();
  try {
    const res = await post('/api/studio', {action: 'decide', key: c.key, rep_id: c.rep.id, card: c.kind, scope: ST.scope, lines});
    ST.done.push(c); ST.queue.shift(); ST.seen++;
    ST.burst = {settled: res.settled, lifts: res.lifts || []};
    (res.lifts || []).forEach(lf => flash(`Jev’s ${stLabel(lf.field, lf.value) || lf.value} at ${stLevelMeta(lf.level).label} was right ${lf.agreed} of ${lf.verdicts} times you checked: ${fmt(lf.messages)} more messages decided`));
    stReset();
  } catch (e) { flash(errText(e)); }
  ST.busy = false;
  stDraw(); stLoadStats(); stFill();
}
async function stSkip() {
  const c = ST.queue[0];
  if (!c || ST.busy) return;
  ST.busy = true;
  try { await post('/api/studio', {action: 'skip', key: c.key, rep_id: c.rep.id, card: c.kind}); } catch (e) { flash(errText(e)); }
  ST.done.push(c); ST.queue.shift(); ST.seen++; ST.burst = null;
  ST.busy = false;
  stReset(); stDraw(); stFill();
}
async function stUndo(id) {
  if (ST.busy) return;
  ST.busy = true;
  try {
    const res = await post('/api/studio', {action: 'undo', decision: id || null});
    const back = ST.done.length && ST.done[ST.done.length - 1].key === res.group ? ST.done.pop() : null;
    if (back && !id) { ST.queue.unshift(back); stReset(); }
    ST.burst = null;
    flash(res.scope === 'skip' ? 'Skip taken back' : `Taken back: ${fmt(res.settled)} messages are as they were`);
  } catch (e) { flash(errText(e)); }
  ST.busy = false;
  stDraw(); stLoadStats();
}
async function stUndoLift(id) {
  try { const r = await post('/api/studio', {action: 'undo_lift', lift: id}); flash(`${fmt(r.returned)} values back to Jev’s guesses`); } catch (e) { flash(errText(e)); }
  stLoadStats();
}

// The count rolls up to its new value (unless motion is off).
function stRoll(el, to) {
  const from = Number(el.dataset.v || 0);
  el.dataset.v = String(to);
  if (!stMotion() || from === to) { el.textContent = fmt(to); return; }
  const t0 = performance.now(), dur = 700;
  const step = t => { const k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3); el.textContent = fmt(Math.round(from + (to - from) * e)); if (k < 1) requestAnimationFrame(step); };
  requestAnimationFrame(step);
}
function stDrawCount() {
  if (!ST.count) return;
  const s = ST.stats, se = s && s.session, td = s && s.today, al = s && s.all;
  if (!ST.count.firstChild) {
    ST.count.append(h('div', {class: 'st-big'}, h('span', {class: 'st-num', 'data-v': '0'}, '0'), h('span', {class: 'st-unit'}, 'messages settled this session')),
      h('div', {class: 'st-sub small muted'}));
  }
  stRoll(ST.count.querySelector('.st-num'), se ? se.total : 0);
  fill(ST.count.querySelector('.st-sub'), se ? `${fmt(se.decisions)} ${se.decisions === 1 ? 'decision' : 'decisions'}: ${fmt(se.settled)} by you, ${fmt(se.lifted)} lifted` : '',
    td ? ` · today ${fmt(td.total)}` : '', al ? ` · in all ${fmt(al.total)}` : '');
}
function stLine(c, ln, i) {
  const on = i === ST.line, pick = ST.picks[ln.field];
  const choices = stChoices(ln);
  const filtering = on && !!ST.filter;
  const settled = ['human', 'rule'].includes(ln.source);
  const state = ln.value == null ? 'Jev has no guess' : settled ? (ln.source === 'human' ? 'you decided' : 'a rule decided')
    : ln.status === 'proposed' ? 'Jev’s guess' : 'accepted from Jev';
  return h('div', {class: 'st-line' + (on ? ' on' : '') + (ln.done && pick === undefined ? ' done' : ''), onclick: e => { if (!e.target.closest('button')) stFocus(i); }},
    h('div', {class: 'st-lh'}, h('b', null, ln.label),
      ln.value != null ? [h('span', {class: 'small muted'}, ` ${state} `), stLevelTag(ln.level, ln.confidence)] : h('span', {class: 'small muted'}, ' ' + state),
      c.messages > 1 && ln.sampled ? h('span', {class: 'small muted st-agree', title: 'Of the group’s newest messages (up to 400), how many already have this value settled'},
        `${fmt(ln.settled)} of ${fmt(ln.sampled)} settled`) : null,
      filtering ? h('span', {class: 'st-filter'}, ST.filter) : null),
    h('div', {class: 'st-opts', role: 'group', 'aria-label': ln.label},
      choices.map((v, k) => h('button', {class: 'st-opt' + (v === ln.value && !filtering ? ' cur' : ''), 'aria-pressed': String(pick === v),
        title: ((ST.opts[ln.field] || []).find(o => o.value === v) || {}).description || null,
        onclick: () => { stFocus(i); stPick(i, v); }},
        on && k < 9 ? h('span', {class: 'k'}, String(k + 1)) : null, stLabel(ln.field, v))),
      !filtering && ln.value != null && !settled ? h('button', {class: 'st-opt no', 'aria-pressed': String(pick === null), title: 'Not this: Jev’s value is rejected, nothing is written (0)',
        onclick: () => { stFocus(i); stPick(i, null); }}, on ? h('span', {class: 'k'}, '0') : null, 'Not this') : null,
      filtering && !choices.length ? h('span', {class: 'small muted'}, 'Nothing matches') : null,
      on && !filtering ? h('span', {class: 'small muted st-type'}, 'type to find another') : null));
}
function stCardNode(c) {
  const r = c.rep;
  const why = c.kind === 'check' && c.cell
    ? h('div', {class: 'st-why check'}, h('span', {class: 'pill ac'}, 'Checking Jev'), ' ',
        h('b', null, `${LABEL_OF[c.cell.field] || c.cell.field}: ${stLabel(c.cell.field, c.cell.value)}`), ' at ', stLevelTag(c.cell.level), ` on ${stSide(c.cell.side)}. `,
        c.cell.need ? `${c.cell.need} more like-for-like yes and ${fmt(c.cell.messages)} messages are settled.` : '')
    : h('div', {class: 'st-why'}, c.messages > 1 ? [`One decision for `, h('b', null, fmt(c.messages)), ` messages`] : 'One message');
  const grp = c.group === 'pattern' ? `${fmt(c.messages)} from ${r.from_address || '?'} like “${c.pattern || r.subject || ''}”`
    : c.group === 'thread' ? `${fmt(c.messages)} in this conversation` : 'this message alone';
  const years = c.first && c.last ? (day(c.first) === day(c.last) ? day(c.last) : `${day(c.first)} – ${day(c.last)}`) : '';
  return h('div', {class: 'st-card', 'aria-live': 'polite'},
    why,
    h('div', {class: 'st-from'}, acctMark(r.account_id), h('b', null, r.from_name || r.from_address || '?'), removedChip(r),
      r.from_name && r.from_address ? h('span', {class: 'small muted'}, r.from_address) : null,
      h('span', {class: 'small muted st-when'}, when(r.received_at)),
      h('button', {class: 'btn sm ghost', title: 'Read it in the pane', onclick: () => openMessage(r.id)}, 'Open')),
    h('div', {class: 'st-subj'}, r.subject || '(no subject)'),
    r.snippet ? h('div', {class: 'st-snip'}, r.snippet) : null,
    h('div', {class: 'st-group small'}, h('span', null, grp), years ? h('span', {class: 'muted'}, ` · ${years}`) : null,
      c.live != null ? (c.live ? h('span', {class: 'muted'}, ` · ${c.live === c.messages ? 'all' : fmt(c.live)} still in your folders`)
        : [' · ', h('span', {class: 'pill hi', title: 'Every message of the group is in Deleted Items or gone from the server'}, 'all removed')]) : null,
      (c.others || []).filter(x => x && x !== r.subject).length ? h('div', {class: 'muted st-others'}, 'Also: ', (c.others || []).filter(x => x && x !== r.subject).slice(0, 3).join(' · ')) : null),
    c.messages > 1 ? h('div', {class: 'st-scope'}, seg([['group', `All ${fmt(c.messages)}`], ['one', 'Just this one']], ST.scope, v => { ST.scope = v; stDrawCard(); }),
      h('span', {class: 'small muted'}, ' . switches')) : null,
    h('div', {class: 'st-lines'}, c.lines.map((ln, i) => stLine(c, ln, i))),
    h('div', {class: 'st-acts'},
      h('button', {class: 'btn primary', disabled: ST.busy, onclick: stDecide}, ST.busy ? `Settling ${fmt(ST.scope === 'one' ? 1 : c.messages)}…` : ['Confirm ', h('span', {class: 'kbd'}, '↵')]),
      h('button', {class: 'btn', disabled: ST.busy, onclick: stSkip}, 'Skip ', h('span', {class: 'kbd'}, '→')),
      h('button', {class: 'btn ghost', disabled: ST.busy || !ST.done.length, onclick: () => stUndo()}, 'Undo ', h('span', {class: 'kbd'}, '←')),
      h('span', {class: 'small muted st-keys'}, '↑↓ lines · 1–9 tick · 0 not this · letters find')));
}
const LABEL_OF = {sender_kind: 'People or machine', kind: 'Kind', topic: 'Topic', value: 'Value'};
function stBurstNode() {
  const b = ST.burst;
  if (!b) return null;
  const lifted = b.lifts.reduce((n, x) => n + x.messages, 0);
  return h('div', {class: 'st-burst' + (stMotion() ? ' go' : '')}, h('b', null, `+${fmt(b.settled + lifted)}`),
    h('span', null, lifted ? ` settled (${fmt(b.settled)} by you, ${fmt(lifted)} lifted)` : ' settled'));
}
function stDrawCard() {
  if (!ST.box) return;
  const c = ST.queue[0];
  if (!c) {
    fill(ST.box, ST.note ? h('div', {class: 'err'}, ST.note)
      : ST.loading || (ST.pool && (ST.pool.refreshing || ST.pool.stale)) ? h('div', {class: 'st-card st-wait'}, h('b', null, 'Gathering the groups…'), h('div', {class: 'small muted'}, 'Talos ranks every unsure group of mail by what one decision settles. The first time takes about ten seconds.'))
      : empty('No cards right now', 'Every group of mail Talos can offer is settled or skipped. New mail brings new cards.'));
    return;
  }
  fill(ST.box, stBurstNode(), stCardNode(c));
  ST.burst = null;
}
function stDrawSide() {
  if (!ST.side) return;
  const s = ST.stats || {};
  const cal = (s.calibration || []).filter(x => x.status === 'proposed').slice(0, 8);
  const lifts = s.lifts || [];
  const recent = (s.recent || []).slice(0, 8);
  fill(ST.side,
    card('Close to a lift', 'Where your yes to Jev’s guess settles its whole kind: say yes often enough on check cards and the rest follow.', null,
      cal.length ? h('div', {class: 'st-cells'}, cal.map(x => {
        const need = x.need, got = x.a, pct = need == null ? 0 : Math.round(100 * x.n / (x.n + need));
        return h('div', {class: 'st-cell'}, h('div', {class: 'st-cellh'}, h('b', null, `${LABEL_OF[x.field] || x.field}: ${stLabel(x.field, x.value)}`), ' ', stLevelTag(x.level), h('span', {class: 'small muted'}, ` ${stSide(x.side)}`),
            x.lifted ? h('span', {class: 'pill ok', style: 'margin-left:6px'}, 'lifted') : null),
          h('div', {class: 'st-bar', title: `${got} of ${x.n} right`}, h('i', {style: `width:${x.lifted ? 100 : pct}%`})),
          h('div', {class: 'small muted'}, `Right ${got} of ${x.n} times` + (x.lifted ? '' : need == null ? ': too often wrong to lift' : need === 0 ? ': lifts with the next yes' : `: ${need} more yes to lift`)));
      })) : h('div', {class: 'small muted'}, 'Nothing checked yet. Every line you confirm or correct counts here.'),
      (ST.cells || []).length && !cal.length ? h('div', {class: 'small muted', style: 'margin-top:6px'}, `Biggest: ${stLabel(ST.cells[0].field, ST.cells[0].value)} at ${stLevelMeta(ST.cells[0].level).label}, ${fmt(ST.cells[0].messages)} messages.`) : null),
    lifts.length ? card('Lifted', 'Jev’s guesses you proved right, accepted with the confidence your checks showed.', null,
      lifts.map(lf => h('div', {class: 'st-rec'}, h('span', null, h('b', null, `+${fmt(lf.messages)}`), ` ${LABEL_OF[lf.field] || lf.field}: ${stLabel(lf.field, lf.value)} at ${stLevelMeta(lf.level).label}, ${stSide(lf.side)} · ${lf.agreed} of ${lf.verdicts}`),
        h('button', {class: 'linkbtn', onclick: () => stUndoLift(lf.id)}, 'Undo')))) : null,
    card('Your last decisions', null, null,
      recent.length ? recent.map(d => h('div', {class: 'st-rec'}, h('span', {class: 'st-rec-t'}, d.scope === 'skip' ? h('span', {class: 'muted'}, 'skipped ') : h('b', null, `+${fmt(d.settled)} `), d.subject || '(no subject)'),
        h('button', {class: 'linkbtn', onclick: () => stUndo(d.id)}, 'Undo'))) : h('div', {class: 'small muted'}, 'Your decisions appear here, each with its undo.')),
    card(withHelp('How sure', 'sureness'), 'Every value carries one of these, in the pane and in Mail’s Sureness filter.', null,
      h('div', {class: 'st-levels'}, (ST.levels.length ? ST.levels : (s.levels || [])).map(l => h('div', null, stLevelTag(l.key), h('span', {class: 'small muted'}, ` ${l.key === 'fact' ? '100%' : l.key === 'guess' ? 'under 30%' : 'from ' + Math.round(l.from * 100) + '%'} · ${l.meaning}`))))));
}
function stDraw() { stDrawCount(); stDrawCard(); stDrawSide(); }
async function viewStudio() {
  ST.box = h('div', {class: 'st-main'});
  ST.side = h('div', {class: 'st-side vstack'});
  ST.count = h('div', {class: 'st-count'});
  ST.root = h('div', {class: 'studio'},
    header(withHelp('Studio', 'studio'), 'Decide once, settle many. Jev’s guess is ticked on every line: confirm it, or tick what is right.', ST.count),
    h('div', {class: 'st-grid'}, ST.box, ST.side));
  if (ST.queue.length) stReset();
  stDraw();
  stLoadStats();
  stFill();
  return ST.root;
}
document.addEventListener('keydown', e => {
  if (S.view !== 'studio' || !ST.root || !ST.root.isConnected || PANE.open || menuEl || e.defaultPrevented) return;
  if (e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]')) return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
  const c = ST.queue[0], k = e.key;
  const act = fn => { e.preventDefault(); fn(); };
  if (k === 'ArrowLeft') return act(() => stUndo());
  if (!c) return;
  const ln = c.lines[ST.line];
  if (k === 'Enter') return act(() => { if (ST.filter) { const v = stChoices(ln)[0]; if (v) stPick(ST.line, v); } else stDecide(); });
  if (k === 'ArrowRight') return act(stSkip);
  if (k === 'ArrowDown' || (k === 'Tab' && !e.shiftKey)) return act(() => stFocus(ST.line + 1));
  if (k === 'ArrowUp' || (k === 'Tab' && e.shiftKey)) return act(() => stFocus(ST.line - 1));
  if (k === 'Escape') return act(() => { ST.filter = ''; stDrawCard(); });
  if (k === '.' && c.messages > 1) return act(() => { ST.scope = ST.scope === 'group' ? 'one' : 'group'; stDrawCard(); });
  if (k === '0' && !ST.filter && ln && ln.value != null && !['human', 'rule'].includes(ln.source)) return act(() => stPick(ST.line, null));
  if (/^[1-9]$/.test(k)) return act(() => { const v = stChoices(ln)[Number(k) - 1]; if (v) stPick(ST.line, v); });
  if (k === 'Backspace') return act(() => { if (ST.filter) { ST.filter = ST.filter.slice(0, -1); stDrawCard(); } });
  if (k.length === 1 && (/\p{L}/u.test(k) || (ST.filter && /[\s&/'’-]/.test(k)))) return act(() => { ST.filter += k; stDrawCard(); });
});

// ---------------------------------------------------------------- clusters (Operations › Clusters)
// Large groups of machine mail, for cleanup (talos.clusters): sender and system per account, with
// their status (dead: nothing in 12 months; quiet: under one a month; active), where they sit now and
// where the structure plan puts them. A row opens examples; "Prepare cleanup" makes planned changesets
// (Gmail) and never commits them; Microsoft 365 is refused: its write-back is for agreed uses only.
const CLU = {status: '', acc: '', sort: null, dir: 1, all: false};
const cluPlace = p => p === '\\Inbox' ? 'Inbox' : p === '(none)' ? 'no kind yet' : p;
const cluPill = st => h('span', {class: 'pill ' + (st === 'dead' ? 'hi' : st === 'quiet' ? 'md' : 'ok')}, st);
const CLU_COLS = [
  ['sender', 'Sender · system', c => `${c.sender} ${c.system || ''}`],
  ['count', 'Mail', c => c.count], ['first', 'First', c => c.first || ''], ['last', 'Last', c => c.last || ''],
  ['recent', '90 d', c => c.recent], ['status', 'Status', c => ({dead: 0, quiet: 1, active: 2})[c.status]]];
async function viewClusters() {
  const d = await api('/api/clusters');
  pollWhileRefreshing(d, '/api/clusters');
  const all = d.clusters || [];
  const accs = [...new Set(all.map(c => c.account))].sort(byAccount);
  let rows = all.filter(c => (!CLU.status || c.status === CLU.status) && (!CLU.acc || c.account === CLU.acc));
  if (CLU.sort) {
    const f = CLU_COLS.find(x => x[0] === CLU.sort)[2];
    rows = [...rows].sort((a, b) => { const x = f(a), y = f(b); return (x < y ? -1 : x > y ? 1 : 0) * CLU.dir || b.count - a.count; });
  }
  const shown = CLU.all ? rows : rows.slice(0, 150);
  const s = d.summary || {accounts: {}};
  const sortBy = k => { if (CLU.sort === k) { if (CLU.dir === -1) { CLU.sort = null; CLU.dir = 1; } else CLU.dir = -1; } else { CLU.sort = k; CLU.dir = ['count', 'recent', 'last', 'first'].includes(k) ? -1 : 1; } render(true); };
  const th = ([k, label]) => h('th', {class: ['count', 'recent'].includes(k) ? 'r' : null, 'aria-sort': CLU.sort === k ? (CLU.dir > 0 ? 'ascending' : 'descending') : 'none'},
    h('button', {class: 'thbtn', onclick: () => sortBy(k)}, label, CLU.sort === k ? (CLU.dir > 0 ? ' ▲' : ' ▼') : ''));
  const perAcc = Object.entries(s.accounts || {}).sort((a, b) => byAccount(a[0], b[0]));
  return h('div', null,
    header(withHelp('Clusters', 'clusters'), 'Large groups of machine mail, by sender and system, for cleanup',
      h('div', {style: 'display:flex;gap:8px;flex-wrap:wrap'},
        seg([['', 'All'], ...accs.map(a => [a, acctName(a)])], CLU.acc, v => { CLU.acc = v; render(true); }),
        seg([['', 'All'], ['dead', 'Dead'], ['quiet', 'Quiet'], ['active', 'Active']], CLU.status, v => { CLU.status = v; render(true); }))),
    h('p', {class: 'lead'}, 'Machine mail grouped by sender, and by system for a shared sender (itrobot@ by its [tag]). Dead: nothing in 12 months. Quiet: under one a month. Active: the rest. Sorted dead first, by size; click a column to sort by it.'),
    note('Dead-system mail stays searchable in Talos: the vault keeps every original, whatever happens in the mailbox. Cleaning up only changes where the mail sits on the server, and only through changesets you commit yourself.'),
    h('div', {class: 'kpis', style: 'border:0;margin:16px 0 14px;padding:0'},
      h('div', {class: 'kpi'}, h('div', {class: 'l'}, 'Mail in dead clusters'), h('div', {class: 'v'}, fmt(s.dead_messages)), h('div', {class: 'n'}, `${fmt(s.dead_clusters)} clusters`)),
      h('div', {class: 'kpi'}, h('div', {class: 'l'}, 'Of it in an inbox'), h('div', {class: 'v'}, fmt(s.dead_in_inbox)), h('div', {class: 'n'}, 'would leave it')),
      perAcc.map(([a, v]) => h('div', {class: 'kpi'}, h('div', {class: 'l'}, `${acctName(a)} inbox now → after`), h('div', {class: 'v'}, `${fmt(v.inbox_now)} → ${fmt(v.inbox_after)}`),
        h('div', {class: 'n'}, `${fmt(v.dead_messages)} dead mail in ${fmt(v.dead_clusters)} cluster${v.dead_clusters === 1 ? '' : 's'}`)))),
    perAcc.length ? h('div', {class: 'grid g2'}, perAcc.map(([a, v]) => card(`${acctName(a)}: where the dead mail sits`,
      d.providers[a] === 'gmail' ? 'Archiving takes it out of the inbox only; its labels stay, and it gets its Talos/Automated/ label. Trash takes it out of every label.' :
        'Planned only: the work account\'s write-back is for agreed uses only. Archiving would move it out of these folders.', null,
      v.places.length ? h('table', {class: 't'}, h('thead', null, h('tr', null, h('th', null, 'Label or folder'), h('th', {class: 'r'}, 'Mail now'), h('th', {class: 'r'}, 'Dead mail'), h('th', {class: 'r'}, 'After'))),
        h('tbody', null, v.places.map(p => h('tr', null, h('td', {class: 'nm'}, cluPlace(p.place)), h('td', {class: 'r'}, fmt(p.now)), h('td', {class: 'r'}, fmt(p.dead)),
          h('td', {class: 'r'}, d.providers[a] === 'gmail' && p.place !== '\\Inbox' ? `${fmt(p.now)} (trash: ${fmt(p.now - p.dead)})` : fmt(p.now - p.dead)))))) :
        empty('No dead mail', 'Nothing here has been silent for 12 months.')))) : null,
    h('section', {class: 'card', style: 'margin-top:16px'},
      h('div', {class: 'card-h'}, h('div', null, h('h2', null, `${fmt(rows.length)} clusters`), h('p', null, `Clusters of ${fmt(20)} messages or more; ${fmt((d.small || {}).clusters || 0)} smaller ones hold ${fmt((d.small || {}).messages || 0)} messages. Click one for examples and cleanup.`)),
        h('div', {class: 'small muted'}, d.computed_at ? `Computed ${when(d.computed_at)} in ${d.seconds} s ` : '', d.stale || d.refreshing ? h('span', {class: 'pill md'}, 'refreshing') : null,
          h('button', {class: 'linkbtn', onclick: async () => { CACHE.clear(); await api('/api/clusters?refresh=1'); render(true); }}, 'Refresh now'))),
      rows.length ? h('div', {class: 'tscroll'}, h('table', {class: 't uclu'},
        h('thead', null, h('tr', null, CLU_COLS.map(th), h('th', null, 'Kind'), h('th', null, 'Now in'), h('th', null, 'Plan puts it'))),
        h('tbody', null, shown.map(c => h('tr', {class: 'click', tabindex: '0', 'data-pane-key': 'cl:' + c.key, onclick: () => openCluster(c, d), onkeydown: e => { if (e.key === 'Enter') openCluster(c, d); }},
          h('td', {class: 'nm'}, acctDot(c.account), ' ', c.sender, c.system ? h('span', {class: 'pill ac', style: 'margin-left:6px'}, c.system) : null, h('small', null, c.name || c.examples[0] || '')),
          h('td', {class: 'r'}, fmt(c.count)), h('td', {class: 'small'}, day(c.first)), h('td', {class: 'small'}, day(c.last)),
          h('td', {class: 'r'}, fmt(c.recent)), h('td', null, cluPill(c.status)),
          h('td', {class: 'small'}, c.kinds.slice(0, 2).map(([k, n]) => h('div', null, `${cluPlace(k)} ${fmt(n)}`))),
          h('td', {class: 'small'}, c.places.slice(0, 2).map(([k, n]) => h('div', null, `${cluPlace(k)} ${fmt(n)}`)), c.in_inbox ? h('div', {class: 'muted'}, `${fmt(c.in_inbox)} in the inbox`) : null),
          h('td', {class: 'small'}, c.plan.slice(0, 2).map(([k, n]) => h('div', null, `${k.replace('Talos/', '')} ${fmt(n)}`)))))))) :
        empty('No clusters', 'No machine mail matches these filters.'),
      rows.length > shown.length ? h('button', {class: 'linkbtn', onclick: () => { CLU.all = true; render(true); }}, `Show all ${fmt(rows.length)}`) : null));
}
function openCluster(c, d) {
  return paneLoad({key: 'cl:' + c.key, title: 'Cluster'}, async () => {
    const ex = await api('/api/clusters/examples?' + new URLSearchParams({key: c.key, limit: '25'}));
    return {node: clusterDetail(c, d, ex), label: c.sender};
  });
}
function clusterDetail(c, d, ex) {
  const gmail = d.providers[c.account] === 'gmail';
  let action = 'archive';
  const out = h('div', {'aria-live': 'polite'});
  const radio = (v, label, text) => h('label', {class: 'uradio'}, h('input', {type: 'radio', name: 'clu-act', value: v, checked: action === v, onchange: () => { action = v; }}),
    h('span', null, h('b', null, label), h('span', {class: 'small muted'}, ' ' + text)));
  const prepare = async () => {
    fill(out, h('span', {class: 'muted small'}, 'Preparing (planned only)…'));
    try {
      const r = await post('/api/clusters/cleanup', {key: c.key, action});
      fill(out, h('div', {class: 'ubox ok'}, h('div', null, r.changesets.length ? `${r.changesets.length} planned changeset${r.changesets.length > 1 ? 's' : ''}, not committed:` : 'Nothing new to plan: this mail is already in an open changeset.'),
        r.changesets.map(x => h('div', null, h('button', {class: 'linkbtn', onclick: () => openChangeset(x.id)}, `#${x.id} ${x.title}`))),
        h('div', {class: 'small muted'}, r.note)));
    } catch (e) {
      let body = null;
      try { body = JSON.parse(/^\d+ ([\s\S]*)$/.exec(String(e.message))[1]); } catch (x) {}
      fill(out, h('div', {class: body && body.refused ? 'ubox' : 'err'}, errText(e)));
    }
  };
  return h('div', null,
    h('div', {class: 'meta', style: 'margin:0 0 6px'}, acctDot(c.account), acctName(c.account), '·', cluPill(c.status)),
    h('h2', null, c.sender, c.system ? ' · ' + c.system : ''),
    c.name ? h('p', {class: 'small muted'}, c.name) : null,
    h('dl', {class: 'kv'},
      h('dt', null, 'Mail'), h('dd', null, `${fmt(c.count)} · ${day(c.first)} – ${day(c.last)} · ${fmt(c.recent)} in the last 90 days, ${fmt(c.last_year)} in 12 months`),
      h('dt', null, 'Kind'), h('dd', null, c.kinds.map(([k, n]) => `${cluPlace(k)} ${fmt(n)}`).join(', ')),
      h('dt', null, 'Type'), h('dd', null, c.types.map(([k, n]) => `${k === '(none)' ? 'no type yet' : k} ${fmt(n)}`).join(', ')),
      h('dt', null, 'Now in'), h('dd', null, c.places.map(([k, n]) => `${cluPlace(k)} ${fmt(n)}`).join(', ') || '—', c.in_inbox ? ` · ${fmt(c.in_inbox)} in the inbox` : ''),
      h('dt', null, 'Plan puts it'), h('dd', null, c.plan.map(([k, n]) => `${k} ${fmt(n)}`).join(', '))),
    h('div', {class: 'dsec'}, h('h4', null, 'Prepare cleanup'),
      gmail ? [
        radio('archive', 'Archive', `(recommended${c.status === 'dead' ? ' for a dead system' : ''}): out of the inbox, with a Talos/Automated/ label (the plan’s, else ${d.cleanup_label}).`),
        radio('trash', 'Trash', d.trash_note),
        h('div', {class: 'uacts'}, h('button', {class: 'btn sm primary', onclick: prepare}, 'Prepare cleanup')),
        h('div', {class: 'small muted'}, 'Makes planned changesets only. You dry-run and commit each one yourself on the Changesets page.')] :
        [note(d.consent), h('div', {class: 'uacts'}, h('button', {class: 'btn sm', onclick: prepare}, 'Show what it would do'))],
      out),
    h('div', {class: 'dsec'}, h('h4', null, `Examples · ${fmt(ex.total)}, newest first`),
      h('table', {class: 't'}, h('tbody', null, ex.rows.map(m => h('tr', {class: 'click', tabindex: '0', 'data-pane-key': 'm:' + m.id, onclick: () => openMessage(m.id), onkeydown: e => { if (e.key === 'Enter') openMessage(m.id); }},
        h('td', {style: 'width:84px', class: 'small'}, day(m.received_at)), h('td', {class: 'nm'}, m.subject || '(no subject)', h('small', null, [m.kind, m.type].filter(Boolean).join(' · ')))))))));
}

// ---------------------------------------------------------------- discovery (Operations › Discovery)
// The drafted systems, candidate binders and the owner's own setup (talos.discovery, TALOS_HOME/config/discovery/*.json),
// for the owner to accept or reject quickly. A row opens its draft in the reading pane (key 'dv:<id>').
// Accepting makes a binder: a system, an area, a topic … (one of the same kind and name that exists
// already, such as the eleven from the vault, is enriched instead: the draft as a note, its empty
// attrs filled). A setup item becomes a line in a note on "My setup". Rejecting stores the
// decision only, or keeps a retired system as a binder with lifecycle retired.
// Keys: a accept, r reject, e edit before accepting, x select, j/k or ↓/↑ move, Enter opens.
const DISC = {tab: 'systems', status: '', cat: '', undecided: false, q: '', sel: new Set(), data: null, edit: null, armed: null};
const DISC_TABS = [['systems', 'Systems'], ['candidates', 'Candidates'], ['my-setup', 'My setup']];
const DISC_TAB_NAME = Object.fromEntries(DISC_TABS);
const DISC_STATUS = {active: ['active', 'pill ok'], unclear: ['unclear', 'pill md'], legacy: ['legacy', 'pill se'],
  probably_retired: ['probably retired', 'pill hi'], retired: ['retired', 'pill hi'], not_adopted: ['not adopted', 'pill']};
const DISC_EVIDENCE = {
  work_mail_from_system: 'From its own senders', work_mail_subject_mentions: 'Other work mail naming it', work_mail_sent_by_him: 'Sent by you',
  teams_messages: 'Teams messages', personal_gmail: 'Personal Gmail', last_12_months: 'In the last 12 months', last_ops_subject: 'Last operational subject',
  assigned_messages: 'Messages assigned to it', last_12m: 'In the last 12 months', mail: 'Mail', teams: 'Teams messages', gmail: 'Personal Gmail',
  mail_mentions: 'Mail naming it', first: 'First seen', last: 'Last seen', vendor_mail: 'Vendor mail', services: 'Services', repos: 'Repositories'};
const SETUP_KIND = {application: 'App', cli: 'CLI tool', service: 'Service', repo: 'Repository', device: 'Device', agent: 'Agent'};
const discName = it => (it.correction && it.correction.name) || it.name;
const discStatus = it => (it.correction && it.correction.status) || it.status;
const discLine = it => (it.correction && it.correction.description) || ({systems: it.payload.used_for, candidates: it.payload.why, 'my-setup': it.payload.what})[it.source] || '';
const discCategory = it => it.source === 'systems' ? it.payload.category : it.source === 'candidates' ? (KIND_LABEL[it.kind] || it.kind) : it.payload.group;
const discSeen = it => { const p = it.payload, ev = p.evidence || {}; return [p.first_seen || ev.first || null, p.last_seen || ev.last || null]; };
const discEvLabel = k => DISC_EVIDENCE[k] || k.replace(/_/g, ' ');
function discStatusPill(st) {
  if (!st) return null;
  const [label, cls] = DISC_STATUS[st] || [st.replace(/_/g, ' '), 'pill'];
  const tip = (((DISC.data || {}).sources || {}).systems || {meta: {}}).meta.status_values || {};
  return h('span', {class: cls, title: tip[st] || (st === 'retired' ? 'marked retired by you' : null)}, label);
}
function discDecision(it) {
  const c = it.correction || {};
  if (!it.decision) return h('span', {class: 'small muted'}, 'undecided');
  if (it.decision === 'accept') return h('span', {class: 'pill ok', title: it.object_name ? `Binder: ${it.object_name}` : null}, 'accepted');
  if (c.keep_as_binder) return h('span', {class: 'pill se', title: 'Rejected as a current system; kept as a binder with lifecycle retired'}, 'kept as retired');
  if (c.status === 'retired') return h('span', {class: 'pill', title: 'Rejected and marked retired; no binder'}, 'marked retired');
  return h('span', {class: 'pill'}, 'rejected');
}
// The evidence in a few words for the row, the full counts in its tooltip.
function discEvidence(it) {
  const ev = it.payload.evidence || {};
  const evOrder = Object.keys(DISC_EVIDENCE), evRank = k => evOrder.includes(k) ? evOrder.indexOf(k) : evOrder.length;
  const nums = Object.entries(ev).filter(([, v]) => typeof v === 'number').sort((a, b) => evRank(a[0]) - evRank(b[0]));
  if (!nums.length) return h('span', {class: 'muted'}, '—');
  const tip = nums.map(([k, v]) => `${discEvLabel(k)}: ${fmt(v)}`).join('\n');
  if (it.source === 'systems') {
    const mail = (ev.work_mail_from_system || 0) + (ev.work_mail_subject_mentions || 0) + (ev.work_mail_sent_by_him || 0);
    return h('span', {title: tip}, `${fmt(mail)} mail`, ev.teams_messages ? h('span', {class: 'muted'}, ` · ${fmt(ev.teams_messages)} Teams`) : null);
  }
  const [k, v] = nums[0];
  return h('span', {title: tip}, fmt(v), h('span', {class: 'muted'}, ' ' + discEvLabel(k).toLowerCase()));
}
function discRows(all) {
  const q = DISC.q.trim().toLowerCase();
  return all.filter(it => (!DISC.status || discStatus(it) === DISC.status) && (!DISC.cat || discCategory(it) === DISC.cat)
    && (!DISC.undecided || !it.decision) && (!q || `${discName(it)} ${discLine(it)} ${it.item_key}`.toLowerCase().includes(q)));
}
const discVisible = () => DISC.data ? discRows(DISC.data.items.filter(it => it.source === DISC.tab)).map(it => it.id) : [];
const discItem = id => DISC.data && DISC.data.items.find(x => x.id === id);

async function viewDiscovery() {
  const d = DISC.data = await api('/api/discovery');
  if (!d.items.length) return h('div', null, header(withHelp('Discovery', 'systems'), 'Drafted systems, areas and topics, to accept or reject'),
    emptyPage('Nothing to review yet', h('span', null, 'Load the drafts first: ', h('code', null, 'uv run talos discovery load'))));
  const src = d.sources[DISC.tab];
  const all = d.items.filter(it => it.source === DISC.tab);
  const rows = discRows(all);
  const cats = [...new Set(all.map(discCategory).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  const isSys = DISC.tab === 'systems', isSetup = DISC.tab === 'my-setup';
  const setTab = v => { if (v === DISC.tab) return; Object.assign(DISC, {tab: v, status: '', cat: '', sel: new Set(), armed: null}); closePane(false); render(); };
  const lead = {
    systems: 'Accepting a system makes a binder of kind system, with the draft as its text. A system binder of the same name (the eleven from the vault) is enriched instead: the draft becomes a note on it. Rejecting stores your decision only.',
    candidates: 'Accepting makes a binder of the proposed kind (area, topic, system, project), nested under its suggested parent when that binder exists. Rejecting stores your decision only.',
    'my-setup': 'Accepted items become lines in a note per group on one binder, “My setup”. Rejected ones stay out.'}[DISC.tab];
  // Bulk actions on the systems ask twice: the first press says what will happen.
  const armed = (key, n, label, sure, run) => {
    const on = DISC.armed === key;
    return h('button', {class: 'btn sm' + (on ? ' primary' : ''), 'aria-live': 'polite', disabled: !n, onclick: async () => {
      if (!on) { DISC.armed = key; render(true); setTimeout(() => { if (DISC.armed === key) { DISC.armed = null; if (S.view === 'discovery') render(true); } }, 6000); return; }
      DISC.armed = null;
      await run();
    }}, on ? sure : label);
  };
  const bulk = async action => {
    try {
      const r = await post('/api/discovery/bulk', {action});
      flash(action === 'accept_active' ? `${fmt(r.count)} active systems accepted` : `${fmt(r.count)} systems rejected and marked retired`);
    } catch (e) { flash(errText(e)); }
    render(true);
  };
  const activeLeft = all.filter(it => it.status === 'active' && !it.decision).length;
  const retiredLeft = all.filter(it => it.status === 'probably_retired' && !it.decision).length;
  const sel = [...DISC.sel].filter(id => all.some(it => it.id === id));
  const allShown = rows.length > 0 && rows.every(it => DISC.sel.has(it.id));
  const selBox = it => h('input', {type: 'checkbox', class: 'switch', 'aria-label': 'Select ' + discName(it), checked: DISC.sel.has(it.id),
    onclick: e => e.stopPropagation(), onkeydown: e => { if (e.key === 'Enter') e.stopPropagation(); },
    onchange: e => { e.target.checked ? DISC.sel.add(it.id) : DISC.sel.delete(it.id); render(true); }});
  const [first, last] = [0, 1];
  const row = it => {
    const seen = discSeen(it);
    return h('tr', {class: 'click' + (it.decision ? ' ddone' : ''), tabindex: '0', 'data-pane-key': 'dv:' + it.id, 'data-id': it.id,
        onclick: () => openDisc(it.id), onkeydown: e => { if (e.key === 'Enter' && e.target === e.currentTarget) { e.preventDefault(); openDisc(it.id); } }},
      h('td', {class: 'dchk'}, selBox(it)),
      h('td', {class: 'nm'}, discName(it), h('small', {class: 'desc'}, discLine(it))),
      isSetup ? [h('td', {class: 'small'}, it.payload.group || '—'), h('td', {class: 'small'}, SETUP_KIND[it.kind] || it.kind)] : [
        h('td', {class: 'small'}, it.source === 'candidates' ? kindBadge(it.kind) : discCategory(it) || '—'),
        h('td', null, isSys ? discStatusPill(discStatus(it)) : it.payload.confidence ? h('span', {class: 'pill' + (it.payload.confidence === 'high' ? ' ok' : it.payload.confidence === 'medium' ? ' md' : '')}, it.payload.confidence) : null),
        h('td', {class: 'small'}, seen[first] || '—'), h('td', {class: 'small'}, seen[last] || '—'),
        h('td', {class: 'small r'}, discEvidence(it))],
      h('td', null, discDecision(it)));
  };
  const cols = isSetup ? ['Name · what it is', 'Group', 'Kind'] : ['Name · what it is', isSys ? 'Category' : 'Kind', isSys ? 'Status' : 'Confidence', 'First seen', 'Last seen', 'Evidence'];
  return h('div', null,
    header(withHelp('Discovery', 'systems'), 'Drafted from the archive on ' + (src.meta.generated || '?') + ': accept what is real, reject the rest',
      seg(DISC_TABS.map(([v, l]) => [v, `${l} (${fmt(d.sources[v].total)})`]), DISC.tab, setTab)),
    h('div', {class: 'dprog'},
      h('b', null, `${fmt(src.decided)} of ${fmt(src.total)} decided`),
      h('span', {class: 'ubar-t', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': String(src.total), 'aria-valuenow': String(src.decided), 'aria-label': 'Decided'},
        h('span', {class: 'ubar-f', style: `width:${src.total ? 100 * src.decided / src.total : 0}%`})),
      h('span', {class: 'small muted'}, `${fmt(src.accepted)} accepted · ${fmt(src.rejected)} rejected`)),
    h('p', {class: 'lead'}, lead),
    h('p', {class: 'small muted dkeys'}, 'Keys: ', h('kbd', null, 'a'), ' accept · ', h('kbd', null, 'r'), ' reject · ', h('kbd', null, 'e'), ' edit before accepting · ',
      h('kbd', null, 'x'), ' select · ', h('kbd', null, 'j'), '/', h('kbd', null, 'k'), ' or ', h('kbd', null, '↓'), '/', h('kbd', null, '↑'), ' move · ', h('kbd', null, 'Enter'), ' open'),
    isSys ? h('div', {class: 'dbulk'},
      h('div', null, armed('accept_active', activeLeft, `Accept all active systems (${fmt(activeLeft)})`, `Yes: make ${fmt(activeLeft)} system binders`, () => bulk('accept_active')),
        h('div', {class: 'small muted'}, 'Every undecided system with status active becomes a binder (or enriches the one of the same name).')),
      h('div', null, armed('reject_retired', retiredLeft, `Reject all probably-retired systems (${fmt(retiredLeft)})`, `Yes: mark ${fmt(retiredLeft)} retired, no binders`, () => bulk('reject_retired')),
        h('div', {class: 'small muted'}, h('b', null, 'Marks them retired and makes no binders. '), 'To keep one for history and the dead-mail cleanup, open it and choose Keep as retired system.'))) : null,
    h('div', {class: 'fbar'},
      h('input', {class: 'search', type: 'search', id: 'disc-q', placeholder: 'Find by name or description', value: DISC.q, 'aria-label': 'Find',
        oninput: e => { DISC.q = e.target.value; render(true); }}),
      isSys ? seg([['', 'Any status'], ...Object.keys(DISC_STATUS).filter(k => all.some(it => discStatus(it) === k)).map(k => [k, DISC_STATUS[k][0]])], DISC.status, v => { DISC.status = v; render(true); }) : null,
      h('select', {class: 'sel', 'aria-label': isSys ? 'Category' : isSetup ? 'Group' : 'Kind', onchange: e => { DISC.cat = e.target.value; render(true); }},
        h('option', {value: ''}, isSys ? 'Every category' : isSetup ? 'Every group' : 'Every kind'), cats.map(c => h('option', {value: c, selected: DISC.cat === c}, c))),
      h('button', {class: 'btn sm', 'aria-pressed': String(DISC.undecided), onclick: () => { DISC.undecided = !DISC.undecided; render(true); }}, 'Undecided only')),
    sel.length ? h('div', {class: 'dsel', role: 'region', 'aria-label': 'Selected rows'},
      h('b', null, `${fmt(sel.length)} selected`),
      h('button', {class: 'btn sm primary', onclick: () => discDecide(sel, 'accept')}, 'Accept them'),
      h('button', {class: 'btn sm', onclick: () => discDecide(sel, 'reject')}, 'Reject them'),
      h('button', {class: 'btn sm ghost', onclick: () => { DISC.sel.clear(); render(true); }}, 'Clear')) : null,
    h('section', {class: 'card'},
      rows.length ? h('div', {class: 'tscroll'}, h('table', {class: 't dtab'},
        h('thead', null, h('tr', null,
          h('th', {class: 'dchk'}, h('input', {type: 'checkbox', class: 'switch', 'aria-label': 'Select every row shown', checked: allShown,
            onchange: e => { rows.forEach(it => e.target.checked ? DISC.sel.add(it.id) : DISC.sel.delete(it.id)); render(true); }})),
          cols.map(c => h('th', {class: c === 'Evidence' ? 'r' : null}, c)), h('th', null, 'Decision'))),
        h('tbody', null, rows.map(row)))) : empty('Nothing matches', 'Clear a filter or the search.')));
}

function openDisc(id, opts = {}) {
  const it = discItem(id);
  if (!it) return;
  if (DISC.edit !== id) DISC.edit = null;
  return openPane(discDetail(it), {key: 'dv:' + id, title: DISC_TAB_NAME[it.source], label: discName(it), ...opts});
}
function discEdit(id) {
  DISC.edit = id;
  openDisc(id);
  const f = PANE.body && PANE.body.querySelector('#disc-name');
  if (f) { f.focus(); f.select(); }
}
function discMove(curId, dir) {
  const vis = discVisible();
  if (!vis.length) return;
  const i = curId == null ? -1 : vis.indexOf(curId);
  const n = i < 0 ? (dir > 0 ? 0 : vis.length - 1) : Math.min(vis.length - 1, Math.max(0, i + dir));
  const row = byPaneKey('dv:' + vis[n]);
  if (!row) return;
  row.focus({preventScroll: true});
  row.scrollIntoView({block: 'nearest'});
  if (PANE.open && paneSplit() && !PANE.max) openDisc(vis[n], {origin: row, focus: false});
}
// Decide, and move on at once: the next undecided row takes the focus (and the pane, when it is open
// beside the list) before the server answers, so a run of a and r goes down the list as fast as the owner
// types. The decisions themselves go to the server one after another, in the order given.
let discQueue = Promise.resolve();
const discPending = new Set();
function discDecide(ids, decision, extra = {}, advance = false) {
  if (!ids.length) return;
  const one = ids.length === 1 ? ids[0] : null;
  if (advance && one != null) {
    const vis = discVisible(), i = vis.indexOf(one);
    const next = vis.slice(i + 1).find(id => !(discItem(id) || {}).decision && !discPending.has(id) && id !== one) ?? vis[i + 1];
    const row = next != null ? byPaneKey('dv:' + next) : null;
    if (row) {
      const paneHere = PANE.open && PANE.cur && String(PANE.cur.key || '').startsWith('dv:');
      const inPane = paneHere && PANE.el.contains(document.activeElement);
      if (paneHere) openDisc(next, {origin: row, focus: inPane});
      if (!inPane) row.focus({preventScroll: true});
      row.scrollIntoView({block: 'nearest'});
    }
  }
  ids.forEach(id => discPending.add(id));
  discQueue = discQueue.then(async () => {
    let res;
    try { res = await post('/api/discovery/decide', {ids, decision, ...extra}); } catch (e) { flash(errText(e)); return; }
    if (DISC.edit != null && ids.includes(DISC.edit)) DISC.edit = null;
    if (!one) DISC.sel.clear();
    const name = one ? discName(res.items[0]) : `${fmt(ids.length)} items`;
    flash(decision === 'accept' ? `Accepted: ${name}` : decision === 'reject' ? (extra.keep_retired ? `Kept as a retired system: ${name}` : `Rejected: ${name}`) : `Undecided again: ${name}`);
    if (S.view !== 'discovery') return;
    await render(true);
    // The pane shows whatever it shows now, with the fresh data.
    if (PANE.open && PANE.cur && String(PANE.cur.key || '').startsWith('dv:')) {
      const id = Number(PANE.cur.key.slice(3));
      if (discItem(id)) openDisc(id, {replace: true, focus: false});
    }
  }).finally(() => ids.forEach(id => discPending.delete(id)));
  return discQueue;
}

function discDetail(it) {
  const p = it.payload, c = it.correction || {}, isSys = it.source === 'systems', isSetup = it.source === 'my-setup';
  const kv = pairs => h('dl', {class: 'dkv'}, pairs.filter(([, v]) => v != null && v !== '' && !(Array.isArray(v) && !v.length)).map(([k, v]) => [h('dt', null, k), h('dd', null, v)]));
  const list = xs => h('ul', {class: 'dlist'}, xs.map(x => h('li', null, x)));
  const sec = (title, ...body) => h('div', {class: 'dsec'}, h('h4', null, title), ...body);
  const [first, last] = discSeen(it);
  const st = discStatus(it);
  const kbd = k => h('kbd', {class: 'dk'}, k);
  const acts = h('div', {class: 'uacts dacts'},
    h('button', {class: 'btn primary', onclick: () => discDecide([it.id], 'accept', {}, true)}, it.decision === 'accept' ? 'Accepted ✓' : 'Accept', kbd('a')),
    h('button', {class: 'btn', onclick: () => discDecide([it.id], 'reject', {}, true)}, 'Reject', kbd('r')),
    isSys ? h('button', {class: 'btn', title: 'Reject it as a current system, but keep a binder with lifecycle retired: for history and the dead-mail cleanup',
      onclick: () => discDecide([it.id], 'reject', {keep_retired: true}, true)}, 'Keep as retired system') : null,
    DISC.edit === it.id ? null : h('button', {class: 'btn ghost', onclick: () => discEdit(it.id)}, 'Edit before accepting', kbd('e')),
    it.decision ? h('button', {class: 'btn ghost', title: 'Back to undecided. A binder it made stays; archive it on its page.', onclick: () => discDecide([it.id], null)}, 'Undo') : null);
  const outcome = it.decision ? h('div', {class: 'ubox' + (it.decision === 'accept' || c.keep_as_binder ? ' ok' : '')},
    discDecision(it), ' ', it.decided_at ? h('span', {class: 'small muted'}, when(it.decided_at)) : null,
    it.object_id ? h('div', {style: 'margin-top:6px'}, isSetup ? 'A line in the note “' + (p.group || '') + '” on ' : 'Binder: ',
      h('button', {class: 'kbadge kbtn ' + kindCls(it.object_kind), title: 'Open the binder', onclick: () => go('objects', it.object_id)}, it.object_name || '#' + it.object_id)) :
      it.decision === 'reject' ? h('div', {class: 'small muted', style: 'margin-top:6px'}, c.status === 'retired' ? 'Marked retired. No binder was made.' : 'Only your decision is stored. No binder was made.') : null) : null;
  let form = null;
  if (DISC.edit === it.id) {
    const name = h('input', {class: 'field', id: 'disc-name', value: discName(it), autocomplete: 'off'});
    const kind = isSetup ? null : h('select', {class: 'sel', id: 'disc-kind'}, BINDER_KINDS.map(k => h('option', {value: k, selected: (c.kind || it.kind) === k}, KIND_LABEL[k])));
    const desc = h('textarea', {class: 'field', id: 'disc-desc', rows: '4'}, discLine(it));
    const status = isSys ? h('select', {class: 'sel', id: 'disc-status'}, Object.keys(DISC_STATUS).map(k => h('option', {value: k, selected: st === k}, DISC_STATUS[k][0]))) : null;
    const noteF = h('input', {class: 'field', id: 'disc-note', value: c.note || '', placeholder: 'Optional: a correction or note, kept on the binder', autocomplete: 'off'});
    const save = () => {
      const correction = {name: name.value, description: desc.value, note: noteF.value};
      if (kind) correction.kind = kind.value;
      if (status) correction.status = status.value;
      discDecide([it.id], 'accept', {correction}, true);
    };
    const lab = (text, el) => h('label', {class: 'dfield'}, h('span', {class: 'small muted'}, text), el);
    form = h('div', {class: 'ubox dedit', onkeydown: e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); save(); } }},
      h('h4', null, 'Edit before accepting'),
      lab('Name', name), kind ? lab('Kind', kind) : null, lab(isSetup ? 'What it is' : 'Description', desc), status ? lab('Status', status) : null, lab('Note', noteF),
      h('div', {class: 'uacts'}, h('button', {class: 'btn primary', onclick: save}, 'Accept with these changes'),
        h('button', {class: 'btn ghost', onclick: () => { DISC.edit = null; openDisc(it.id, {replace: true}); }}, 'Cancel'),
        h('span', {class: 'small muted'}, '⌘Enter accepts')));
  }
  const byKey = new Map((DISC.data ? DISC.data.items : []).filter(x => x.source === 'systems').map(x => [x.item_key, x]));
  // jsonb keeps no key order: the evidence in DISC_EVIDENCE's order, the rest after.
  const evOrder = Object.keys(DISC_EVIDENCE), evRank = k => evOrder.includes(k) ? evOrder.indexOf(k) : evOrder.length;
  const ev = Object.entries(p.evidence || {}).filter(([, v]) => v != null && v !== '').sort((a, b) => evRank(a[0]) - evRank(b[0]));
  return h('div', {class: 'ddetail'},
    h('div', {class: 'meta', style: 'margin:0 0 6px'},
      it.source === 'candidates' ? kindBadge(c.kind || it.kind) : h('span', null, discCategory(it) || ''),
      isSys ? discStatusPill(st) : null, isSetup ? h('span', null, SETUP_KIND[it.kind] || it.kind) : null,
      h('span', {class: 'small muted'}, it.item_key)),
    h('h2', null, discName(it)),
    discLine(it) ? h('p', {class: 'dline'}, discLine(it)) : null,
    acts, form, outcome,
    isSys ? [
      sec('How it is used', kv([['Category', p.category], ['Vendor', p.vendor], ['Status', st ? [discStatusPill(st), ' ', h('span', {class: 'small muted'}, (((DISC.data.sources.systems || {}).meta || {}).status_values || {})[st] || '')] : null],
        ['Seen', first || last ? `${first || '?'} – ${last || '?'}` : null], ['Last mail from its own senders', p.last_mail_from_own_senders]])),
      ev.length ? sec('Evidence', kv(ev.map(([k, v]) => [discEvLabel(k), typeof v === 'number' ? fmt(v) : String(v)]))) : null,
      (p.examples || []).length ? sec('Example subjects', list(p.examples.map(x => `“${x}”`))) : null,
      (p.related || []).length ? sec('Related systems', h('div', {class: 'chips'}, p.related.map(k => {
        const x = byKey.get(k);
        return x ? h('button', {class: 'chipbtn', onclick: () => openDisc(x.id)}, discName(x)) : h('span', {class: 'chipbtn'}, k);
      }))) : null,
      p.suggested_collector ? sec('How Talos could document it', h('p', {class: 'dline'}, p.suggested_collector)) : null,
      (p.open_questions || []).length ? sec('Open questions', list(p.open_questions)) : null,
      p.cleanup ? sec('Dead-mail cleanup', h('p', {class: 'small'}, `${fmt(p.cleanup.mail)} mails from these senders:`), list(p.cleanup.senders || [])) : null,
      p.noise ? sec('Noise', h('p', {class: 'small'}, `${fmt(p.noise.n)} ${p.noise.what || ''}`)) : null] : null,
    it.source === 'candidates' ? [
      sec('Where it fits', kv([['Kind', KIND_LABEL[c.kind || it.kind] || it.kind], ['Suggested parent', p.suggested_parent], ['Sphere', p.sphere], ['Confidence', p.confidence], ['Rank', p.rank]])),
      ev.length ? sec('Evidence', kv(ev.map(([k, v]) => [discEvLabel(k), typeof v === 'number' ? fmt(v) : String(v)]))) : null] : null,
    isSetup ? sec('On this Mac', kv([['Kind', SETUP_KIND[it.kind] || it.kind], ['Group', p.group], ['What', p.what], ['OS', p.os], ['Remote', p.remote]]),
      h('p', {class: 'small muted'}, `Accepting adds it to the note “${p.group || ''}” on the binder My setup.`)) : null);
}

document.addEventListener('keydown', e => {
  if (S.view !== 'discovery' || !DISC.data || menuEl || e.defaultPrevented) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const t = e.target;
  if (t && ((t.tagName === 'INPUT' && t.type !== 'checkbox') || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
  const inPane = PANE.open && PANE.el.contains(t);
  const row = t && t.closest ? t.closest('tr[data-pane-key^="dv:"]') : null;
  const paneId = PANE.open && PANE.cur && String(PANE.cur.key || '').startsWith('dv:') ? Number(PANE.cur.key.slice(3)) : null;
  const cur = row ? Number(row.dataset.id) : paneId;
  const k = e.key;
  const dir = k === 'j' || (k === 'ArrowDown' && !inPane) ? 1 : k === 'k' || (k === 'ArrowUp' && !inPane) ? -1 : 0;
  if (dir) { e.preventDefault(); discMove(cur, dir); return; }
  if (cur == null || !discItem(cur)) return;
  const act = fn => { e.preventDefault(); fn(); };
  if (k === 'a') return act(() => discDecide([cur], 'accept', {}, true));
  if (k === 'r') return act(() => discDecide([cur], 'reject', {}, true));
  if (k === 'e') return act(() => discEdit(cur));
  if (k === 'x') return act(() => { DISC.sel.has(cur) ? DISC.sel.delete(cur) : DISC.sel.add(cur); render(true); });
  if (k === 'Enter' && row && t === row) return act(() => openDisc(cur));
});

// ---------------------------------------------------------------- release notes
// CHANGELOG.md, read by talos.releases: the version in the rail, and a page of what changed. The
// rail marks a version you haven't looked at yet with a dot, remembered in this browser.
const RELEASE = {version: null, fresh: false};
const RELEASE_KIND = {Added: 'ok', Changed: 'info', Fixed: 'md', Removed: '', Deprecated: '', Security: 'hi'};
fetchJSON('/api/release-notes').then(d => {
  RELEASE.version = d.version;
  let seen = null;
  try { seen = localStorage.getItem('talos-seen-version'); } catch (e) {}
  RELEASE.fresh = seen !== d.version;
  renderRail();
}).catch(() => {});
async function viewReleases() {
  const d = await api('/api/release-notes');
  try { localStorage.setItem('talos-seen-version', d.version); } catch (e) {}
  if (RELEASE.fresh) { RELEASE.fresh = false; setTimeout(renderRail); }
  return h('div', null,
    header('Release notes', `You are on Talos ${d.version}. Versions follow Semantic Versioning: a new minor version adds features, a patch only fixes.`),
    d.releases.map(r => h('section', {class: 'card release'},
      h('div', {class: 'card-h'}, h('div', null, h('h2', null, r.version === 'Unreleased' ? 'Coming next' : 'Talos ' + r.version),
        h('p', null, r.version === 'Unreleased' ? 'Built, not released yet' : r.date || '')),
        r.version === d.version ? h('span', {class: 'pill ok'}, 'this version') : null),
      Object.entries(r.changes).filter(([, items]) => items.length).map(([kind, items]) => h('div', {class: 'rel-sec'},
        h('span', {class: 'pill ' + (RELEASE_KIND[kind] || '')}, kind),
        h('ul', null, items.map(t => h('li', null, mdInline(t)))))))));
}

// ---------------------------------------------------------------- the work space and Discover
// The work space (talos.space): the areas with the binders linked to them, what needs the owner, and
// the watchers (saved Messages selections that count what arrived since they last looked). Discover
// (talos.discover): patterns nobody asked about, and saved aggregations. A binder's page gets
// "Found in your mail", its neighbours and its watchers. Nothing here changes a mail server.

// A stored Messages selection (the view's query string) back into filters, and in words.
function filtersFromQuery(qs) {
  const f = blank();
  for (const [k, v] of new URLSearchParams(qs || '')) {
    if (k === 'dim') {
      const [d, val] = v.split(/:(.*)/s);
      if (DIM_FILTERS.includes(d)) f[d] = val; else f.dims = [...f.dims, v];
    } else if (Array.isArray(f[k])) f[k] = [...f[k], v];
    else f[k] = v;
  }
  return f;
}
function openQuery(qs) { S.f = filtersFromQuery(qs); go('messages'); }
function queryWords(qs) {
  const out = [];
  for (const [k, v] of new URLSearchParams(qs || '')) {
    if (k === 'q') out.push(`“${v}”`);
    else if (k === 'dim') { const [d, val] = v.split(/:(.*)/s); out.push(`${d}: ${String(val).replace(/_/g, ' ')}`); }
    else if (k === 'automated') out.push(v === 'true' ? 'automated' : 'people');
    else if (k === 'direction') out.push(v === 'in' ? 'received' : v === 'out' ? 'sent' : v);
    else if (k === 'exclude_accounts' || k === 'exclude_senders' || k === 'exclude_domains') continue;
    else out.push(`${k}: ${v}`);
  }
  return out.join(' · ') || 'everything';
}
// The current Messages selection, as a watcher or an aggregation stores it.
const currentQuery = () => filterParams(S.f).toString();

const binderChip = b => h('button', {class: 'kbadge kbtn ' + kindCls(b.kind) + (b.lifecycle === 'retired' ? ' retired' : ''),
  title: `${KIND_LABEL[b.kind] || b.kind}: ${b.name}` + (b.mentions != null ? ` · ${fmt(b.mentions)} mails mention it` : ''),
  onclick: () => go('objects', b.id)}, b.name);

// A signal line for an area: what is stuck first, then what is open, then how busy the mail is.
function areaSignal(a) {
  const bits = [];
  if (a.overdue) bits.push(h('span', {class: 'sig bad'}, `${fmt(a.overdue)} overdue`));
  if (a.blocked) bits.push(h('span', {class: 'sig bad'}, `${fmt(a.blocked)} blocked`));
  if (a.open) bits.push(h('span', {class: 'sig'}, `${fmt(a.open)} open work ${a.open === 1 ? 'item' : 'items'}`));
  if (a.recent) bits.push(h('span', {class: 'sig'}, `${fmt(a.recent)} mails this month`));
  return bits.length ? h('div', {class: 'asig'}, bits) : h('div', {class: 'asig muted'}, 'Quiet: no open work, no mail this month');
}

function watcherState(w) {
  if (w.paused) return h('span', {class: 'pill'}, 'Paused');
  if (w.fresh) return h('span', {class: 'pill ok'}, `${fmt(w.fresh)} new`);
  return h('span', {class: 'muted small'}, 'Watching');
}
function watcherRows(ws, {showBinder = true} = {}) {
  const act = async (w, path, body) => { try { const r = await post(`/api/watchers/${w.id}${path}`, body || {}); if (r && r.added != null) flash(`${fmt(r.added)} added to ${w.object_name}`); render(true); } catch (e) { alert(errText(e)); } };
  // The likely action shows (Take in, when there is news); Seen, Pause and Remove live in ⋯, so nothing is clipped.
  const menu = (w, anchor) => {
    if (menuEl) { closeMenu(true); if (menuReturn === anchor) return; }
    menuReturn = anchor;
    const item = (label, fn) => h('button', {class: 'menu-item', role: 'menuitem', tabindex: '-1', onclick: () => { closeMenu(); fn(); }}, label);
    menuEl = h('div', {class: 'menu', role: 'menu', 'aria-label': w.name, onkeydown: menuKeys}, h('div', {class: 'menu-h'}, w.name),
      item('Show in Mail', () => { act(w, '', {seen: true}); openQuery(w.query); }),
      w.fresh ? item('Mark what is new as seen', () => act(w, '', {seen: true})) : null,
      item(w.paused ? 'Resume' : 'Pause', () => act(w, '', {paused: !w.paused})),
      h('div', {class: 'menu-sep', role: 'separator'}),
      item('Remove…', () => { if (confirm(`Remove the watcher “${w.name}”? The mail stays as it is.`)) act(w, '/remove'); }));
    document.body.append(menuEl);
    anchor.setAttribute('aria-expanded', 'true');
    placeMenu(anchor);
    menuEl.querySelector('.menu-item').focus();
  };
  return h('div', {class: 'wlist'}, ws.map(w => {
    const more = h('button', {class: 'k-more wl-more', 'aria-haspopup': 'menu', 'aria-label': `More for ${w.name}`, onmousedown: e => { if (menuEl) e.stopPropagation(); }, onclick: () => menu(w, more)}, icon('more'));
    return h('div', {class: 'wrow' + (w.paused ? ' paused' : '')},
      h('div', {class: 'wr-m'}, h('button', {class: 'linkbtn wr-n', title: 'Show these in Mail', onclick: () => { act(w, '', {seen: true}); openQuery(w.query); }}, w.name),
        h('span', {class: 'wr-q'}, queryWords(w.query) + (w.made_by === 'claude' ? ' · made by Claude' : ''))),
      showBinder ? h('span', {class: 'wr-b'}, w.object_id ? binderChip({id: w.object_id, kind: w.object_kind, name: w.object_name}) : h('span', {class: 'muted small'}, 'No binder')) : null,
      h('span', {class: 'wr-c num', title: 'Found so far'}, fmt(w.total)),
      h('span', {class: 'wr-s'}, watcherState(w)),
      h('span', {class: 'wr-a'},
        w.object_id && w.fresh ? abtn('act', 'takein', `Take in ${fmt(w.fresh)}`, () => act(w, '/take-in'), {color: 'var(--accent)', title: `Add the ${fmt(w.fresh)} new to ${w.object_name}, then mark them seen`}) : null,
        w.fresh ? abtn('act', 'check', 'Seen', () => act(w, '', {seen: true}), {color: 'var(--good-ink)', title: 'Mark what is new as seen'}) : null,
        more));
  }));
}

// A watcher is made from a selection: the current Messages filters, a binder's terms, or a search.
async function newWatcher({query = '', name = '', objectId = null} = {}) {
  let homes = [];
  try { homes = await api('/api/work/homes'); } catch (e) { /* the binder is optional */ }
  const nm = h('input', {class: 'field', id: 'w-name', value: name, placeholder: 'What it looks for, in a few words', maxlength: '200'});
  const q = h('input', {class: 'field', id: 'w-q', value: query ? '' : '', placeholder: 'Search words, as in Messages'});
  const home = homeSelect(homes, objectId || '', {id: 'w-home'}, h('option', {value: ''}, 'No binder'));
  const msg = h('div');
  const save = async () => {
    const qs = query || (q.value.trim() ? new URLSearchParams({q: q.value.trim()}).toString() : '');
    if (!nm.value.trim()) nm.value = q.value.trim();
    try {
      await post('/api/watchers', {name: nm.value, query: qs, object_id: home.value || null});
      closePane(false); flash('Watcher saved'); render(true);
    } catch (e) { msg.replaceChildren(h('span', {class: 'err'}, errText(e))); }
  };
  openPane(h('div', {class: 'formcol'},
    h('p', {class: 'small muted'}, 'A watcher keeps looking: it counts what matches and tells you what arrived since you last looked. With a binder, you can take its new mail in with one click.'),
    h('label', {class: 'flabel', for: 'w-name'}, 'Name'), nm,
    query ? [h('div', {class: 'flabel'}, 'Looks for'), h('div', {class: 'qwords'}, queryWords(query))] : [h('label', {class: 'flabel', for: 'w-q'}, 'Search words'), q],
    h('label', {class: 'flabel', for: 'w-home'}, 'For binder'), home,
    h('div', {class: 'rrow', style: 'margin-top:12px'}, h('button', {class: 'btn primary', onclick: save}, 'Save watcher')), msg),
    {key: 'new-watcher', title: 'New watcher'});
  setTimeout(() => (name ? home : nm).focus(), 50);
}

async function newAggregation() {
  const query = currentQuery();
  const nm = h('input', {class: 'field', id: 'ag-name', placeholder: 'A name for it', maxlength: '200'});
  const by = h('select', {class: 'sel', id: 'ag-by'}, [['domain', 'Sender domain'], ['sender', 'Sender'], ['year', 'Year'], ['month', 'Month'], ['account', 'Account']].map(([v, l]) => h('option', {value: v}, l)));
  const msg = h('div');
  const save = async () => {
    try { await post('/api/aggregations', {name: nm.value, query, group_by: by.value}); closePane(false); flash('Saved to Discover'); }
    catch (e) { msg.replaceChildren(h('span', {class: 'err'}, errText(e))); }
  };
  openPane(h('div', {class: 'formcol'},
    h('p', {class: 'small muted'}, 'An aggregation counts this selection in groups, kept up to date on Discover.'),
    h('div', {class: 'flabel'}, 'Counts'), h('div', {class: 'qwords'}, queryWords(query)),
    h('label', {class: 'flabel', for: 'ag-name'}, 'Name'), nm,
    h('label', {class: 'flabel', for: 'ag-by'}, 'Grouped by'), by,
    h('div', {class: 'rrow', style: 'margin-top:12px'}, h('button', {class: 'btn primary', onclick: save}, 'Save to Discover')), msg),
    {key: 'new-aggregation', title: 'New aggregation'});
  setTimeout(() => nm.focus(), 50);
}

// A new binder, optionally in an area and with its search terms.
async function newBinder({name = '', kind = 'project', terms = []} = {}) {
  let homes = [];
  try { homes = await api('/api/work/homes'); } catch (e) { /* the area is optional */ }
  const nm = h('input', {class: 'field', id: 'b-name', value: name, maxlength: '200'});
  const k = h('select', {class: 'sel', id: 'b-kind'}, ['project', 'system', 'topic', 'area', 'personal_project'].map(x => h('option', {value: x}, KIND_LABEL[x])));
  k.value = kind;
  const area = h('select', {class: 'sel', id: 'b-area'}, h('option', {value: ''}, 'No area yet'), homes.filter(o => o.kind === 'area').map(o => h('option', {value: o.id}, o.name)));
  const tm = h('input', {class: 'field', id: 'b-terms', value: terms.join(', '), placeholder: 'Words to look for in the mail, comma separated (the name when empty)'});
  const msg = h('div');
  const save = async () => {
    try {
      const o = await post('/api/objects', {kind: k.value, name: nm.value});
      if (area.value) await post(`/api/objects/${area.value}/members`, {action: 'add', ids: [o.id]});
      const t = tm.value.split(',').map(x => x.trim()).filter(Boolean);
      if (t.length) await post(`/api/space/binders/${o.id}/terms`, {terms: t});
      closePane(false); go('objects', o.id);
    } catch (e) { msg.replaceChildren(h('span', {class: 'err'}, errText(e))); }
  };
  openPane(h('div', {class: 'formcol'},
    h('label', {class: 'flabel', for: 'b-name'}, 'Name'), nm,
    h('label', {class: 'flabel', for: 'b-kind'}, 'Kind'), k,
    h('label', {class: 'flabel', for: 'b-area'}, 'In area'), area,
    h('label', {class: 'flabel', for: 'b-terms'}, 'Search terms'), tm,
    h('div', {class: 'rrow', style: 'margin-top:12px'}, h('button', {class: 'btn primary', onclick: save}, 'Make the binder')), msg),
    {key: 'new-binder', title: 'New binder'});
  setTimeout(() => nm.focus(), 50);
}

async function viewSpace() {
  const d = await api('/api/space');
  pollWhileRefreshing(d, '/api/space');
  const counts = a => {
    const by = {};
    a.binders.forEach(b => { by[b.kind] = (by[b.kind] || 0) + 1; });
    return KIND_ORDER.filter(k => by[k]).map(k => `${by[k]} ${String(by[k] === 1 ? KIND_LABEL[k] : KIND_PLURAL[k] || KIND_LABEL[k]).toLowerCase()}`).join(' · ') || 'nothing linked yet';
  };
  const areaCard = a => h('section', {class: 'card acard kind-area'},
    h('div', {class: 'acard-top'}, h('span', {class: 'small muted'}, counts(a))),
    h('h2', null, h('button', {class: 'linkbtn aname', onclick: () => go('objects', a.id)}, a.name)),
    a.binders.length ? h('div', {class: 'chips'}, a.binders.map(binderChip)) : h('div', {class: 'small muted'}, 'Link projects and systems to it from their pages.'),
    areaSignal(a));
  return h('div', null,
    header(withHelp('Areas', 'areas'), 'Everything you run, in one picture', h('div', {class: 'rrow'},
      h('button', {class: 'btn', onclick: () => newWatcher()}, 'New watcher'),
      h('button', {class: 'btn primary', onclick: () => newBinder()}, 'New binder'))),
    h('div', {class: 'spacemap'}, d.areas.map(areaCard),
      d.loose.length ? h('section', {class: 'card acard loose'},
        h('div', {class: 'acard-top'}, h('span', {class: 'kbadge'}, 'Not in an area'), h('span', {class: 'small muted'}, `${d.loose.length} binders`)),
        h('h2', null, 'Still to place'),
        h('div', {class: 'chips'}, d.loose.map(binderChip)),
        h('div', {class: 'asig muted'}, 'Open one and add it to an area under "Part of", or make it part of one when you create it.')) : null),
    h('div', {style: 'margin-top:18px'},
      card(withHelp('Watchers', 'watchers'), 'Saved searches that keep looking, and say what arrived since you last looked. What needs you from here shows on Today.', h('button', {class: 'btn sm', onclick: () => newWatcher()}, 'New watcher'),
        d.watchers.length ? watcherRows(d.watchers) : empty('No watchers yet', 'Make one here, from a binder (it looks for the binder\'s terms), or with "Watch" in Mail, which keeps what you filtered.'))),
    d.refreshing ? h('div', {class: 'loading-line', style: 'margin-top:10px'}, 'Counting the mail per binder…') : null);
}

// ---- Discover
const DISC_OPEN = new Set();
async function viewDiscover() {
  const [d, aggd] = await Promise.all([api('/api/discover'), api('/api/aggregations')]);
  pollWhileRefreshing(d, '/api/discover');
  pollWhileRefreshing(aggd, '/api/aggregations');
  const aggs = aggd.rows || [];
  const itemAct = (ins, it) => {
    const acts = [];
    if (it.message_id) acts.push(h('button', {class: 'btn ghost sm', onclick: () => openMessage(it.message_id)}, 'Open'));
    if (it.query) acts.push(h('button', {class: 'btn ghost sm', onclick: () => openQuery(it.query)}, 'Mails'));
    if (ins.key === 'no-binder') acts.push(h('button', {class: 'btn sm', onclick: () => newBinder({name: it.name, terms: [it.label]})}, 'Make a binder'));
    if (ins.key === 'quiet' || ins.key === 'renewals') acts.push(h('button', {class: 'btn ghost sm', title: 'Save as a watcher, to see when it comes back', onclick: () => newWatcher({query: it.query, name: it.label})}, 'Watch'));
    return acts;
  };
  const insCard = ins => {
    const open = DISC_OPEN.has(ins.key), shown = open ? ins.items : ins.items.slice(0, 4);
    return h('section', {class: 'card ins' + (ins.count ? '' : ' quiet')},
      h('span', {class: 'ins-tag'}, ins.tag),
      h('h2', null, ins.title),
      h('p', {class: 'small muted'}, ins.text),
      shown.length ? h('div', {class: 'ins-items'}, shown.map(it => h('div', {class: 'ins-item'},
        h('div', {class: 'ins-main'}, h('b', null, it.label), h('small', null, [it.note, it.detail].filter(Boolean).join(' · '))),
        h('div', {class: 'rrow'}, itemAct(ins, it))))) : null,
      ins.items.length > 4 ? h('button', {class: 'linkbtn', onclick: () => { open ? DISC_OPEN.delete(ins.key) : DISC_OPEN.add(ins.key); render(true); }}, open ? '▴ Fewer' : `Show all ${ins.items.length}`) : null);
  };
  const aggRow = a => {
    const open = DISC_OPEN.has('agg' + a.id);
    const max = Math.max(1, ...a.groups.map(g => g.n));
    return [h('tr', {class: 'click', onclick: () => { open ? DISC_OPEN.delete('agg' + a.id) : DISC_OPEN.add('agg' + a.id); render(true); }},
        h('td', {class: 'nm'}, (open ? '▾ ' : '▸ ') + a.name, h('small', null, a.description || queryWords(a.query))),
        h('td', {class: 'r'}, fmt(a.total), h('small', {class: 'muted'}, ` in ${fmt(a.count)} ${a.group_by === 'year' ? 'years' : a.group_by === 'month' ? 'months' : a.group_by === 'account' ? 'accounts' : a.group_by === 'sender' ? 'senders' : 'domains'}`)),
        h('td', null, a.made_by === 'claude' ? 'Claude' : 'You'),
        h('td', {class: 'r act'}, h('button', {class: 'btn ghost sm', onclick: e => { e.stopPropagation(); openQuery(a.query); }}, 'Mails'),
          h('button', {class: 'btn ghost sm', onclick: async e => { e.stopPropagation(); if (confirm(`Remove “${a.name}” from Discover?`)) { try { await post(`/api/aggregations/${a.id}/remove`, {}); render(true); } catch (x) { alert(errText(x)); } } }}, 'Remove'))),
      open ? h('tr', {class: 'agg-open'}, h('td', {colspan: '4'}, h('div', {class: 'hbars'}, a.groups.map(g => h('button', {class: 'hbar', title: `${g.key}: ${fmt(g.n)} — show them`, onclick: () => openQuery(g.query)},
        h('span', {class: 'lab'}, g.key), h('span', {class: 'track'}, h('span', {class: 'fill', style: `display:block;width:${100 * g.n / max}%;background:var(--accent)`})), h('span', {class: 'val'}, fmt(g.n))))),
        a.count > a.groups.length ? h('div', {class: 'small muted'}, `The largest ${a.groups.length} of ${fmt(a.count)}.`) : null)) : null];
  };
  return h('div', null,
    header(withHelp('Discover', 'discover'), 'Patterns in your mail that nobody asked about, and counts you keep', h('div', {class: 'rrow'},
      d.computed_at ? h('span', {class: 'small muted'}, 'Found ' + when(d.computed_at)) : null,
      h('button', {class: 'btn sm', onclick: async () => { CACHE.clear(); await api('/api/discover?refresh=1'); render(true); }}, 'Look again'))),
    d.insights.length ? h('div', {class: 'ins-grid'}, d.insights.map(insCard)) : empty('Looking…', 'The first look through the archive takes a few seconds.'),
    card(withHelp('Saved aggregations', 'aggregations'), 'Selections from Messages counted in groups, kept up to date: made by you (“Aggregate” in Messages) or by Claude', null,
      aggs.length ? h('div', {class: 'tw'}, h('table', {class: 't'},
        h('thead', null, h('tr', null, h('th', null, 'Name'), h('th', {class: 'r'}, 'Now'), h('th', null, 'Made by'), h('th', null))),
        h('tbody', null, aggs.map(aggRow)))) : empty('None yet', 'Filter Messages, then press Aggregate.')));
}

// ---- on a binder's page: its search terms, the mail that mentions them, neighbours and watchers
function binderSpace(o) {
  if (!BINDER_KINDS.includes(o.kind) || o.kind === 'area') return null;
  const box = h('div', {class: 'grid g-hero', style: 'margin-top:16px'}, h('section', {class: 'card'}, h('div', {class: 'muted small'}, 'Looking through the mail…')));
  api(`/api/space/binders/${o.id}`).then(d => {
    if (S.obj !== o.id) return;
    const f = d.found;
    const act = async (tid, action) => { try { await post(`/api/space/binders/${o.id}/found`, {thread_id: tid, action}); render(true); } catch (e) { alert(errText(e)); } };
    const editTerms = async () => {
      const v = prompt('Words to look for in the mail, comma separated (empty: the name)', f.terms.join(', '));
      if (v === null) return;
      try { await post(`/api/space/binders/${o.id}/terms`, {terms: v.split(',').map(x => x.trim()).filter(Boolean)}); render(true); } catch (e) { alert(errText(e)); }
    };
    fill(box,
      card(withHelp('Found in your mail', 'found-in-mail'), `Threads that mention it and are not in it yet, people's first · ${fmt(f.threads)} threads in all`,
        h('div', {class: 'rrow'}, h('button', {class: 'btn ghost sm', onclick: () => openQuery(f.query)}, 'All in Messages')),
        h('div', {class: 'termrow'}, h('span', {class: 'small muted'}, 'Looks for'), f.terms.map(t => h('span', {class: 'chip'}, t)),
          h('button', {class: 'linkbtn', onclick: editTerms}, 'Change')),
        f.rows.length ? h('div', {class: 'ins-items'}, f.rows.map(r => h('div', {class: 'ins-item'},
          h('div', {class: 'ins-main'}, h('button', {class: 'linkbtn', onclick: () => openMessage(r.message_id)}, r.subject || '(no subject)'),
            h('small', null, [acctName(r.account_id), r.from, r.mine ? 'you took part' : r.people ? null : 'automated', `${fmt(r.n)} ${r.n === 1 ? 'mail' : 'mails'}`, day(r.last)].filter(Boolean).join(' · '))),
          h('div', {class: 'rrow'}, h('button', {class: 'btn sm', title: 'Add the thread to this binder', onclick: () => act(r.thread_id, 'add')}, 'Add'),
            h('button', {class: 'btn ghost sm', title: 'Not about this binder: keep it out', onclick: () => act(r.thread_id, 'exclude')}, 'Not this'))))) :
          f.threads ? empty('Nothing new', 'Every thread that mentions it is in the binder or kept out. Change the words to look wider.')
            : empty('No mail mentions it', `Nothing in the archive says ${f.terms.map(t => '“' + t + '”').join(' or ')}. Change the words: the name of a product, a vendor or a domain works best.`)),
      h('div', {class: 'vstack'},
        card(withHelp('Neighbours', 'neighbours'), 'Linked to it, or sharing threads with it', null,
          d.neighbours.length ? h('div', {class: 'nbrs'}, d.neighbours.map(n => h('div', {class: 'nbr'}, binderChip(n), h('span', {class: 'small muted'}, n.why)))) :
            h('div', {class: 'small muted'}, 'None yet: no links, and no threads shared with another binder.')),
        card(withHelp('Watchers', 'watchers'), 'Searches that keep filling it', h('button', {class: 'btn sm', onclick: () => newWatcher({query: f.query, name: `Mentions of ${f.terms[0]}`, objectId: o.id})}, 'Add a watcher'),
          d.watchers.length ? watcherRows(d.watchers, {showBinder: false}) : h('div', {class: 'small muted'}, 'None yet. A watcher counts new mail about it, and takes it in when you say so.'))));
  }).catch(e => fill(box, h('section', {class: 'card'}, h('div', {class: 'err'}, errText(e)))));
  return box;
}

// ---------------------------------------------------------------- the calendar
// Day, work week and week, like a mail client's calendar (talos.calendars, docs/calendar.md). The
// calendars in the sidebar are the legend: each has one of the eight series colours, a switch that
// shows or hides it, and one calendar is the default for a new entry. An entry opens as a form wherever
// its calendar can be written (Talos's own, and Microsoft 365, iCloud or Google once writing is set up:
// talos.calwrite); elsewhere it opens as details.
const CAL = {mode: 'workweek', day: null, scroll: null, allOpen: false};
const ALLDAY_SHOWN = 2;
const CAL_MODES = [['day', 'Day'], ['workweek', 'Work week'], ['week', 'Week']];
try { const m = localStorage.getItem('talos-cal-mode'); if (CAL_MODES.some(([v]) => v === m)) CAL.mode = m; } catch (e) {}
const HOUR_PX = 48, CAL_START_HOUR = 7;
const calColor = n => `var(--series-${n >= 1 && n <= 8 ? n : 1})`;
const parseDay = iso => { const [y, m, d] = iso.split('-').map(Number); return new Date(y, m - 1, d); };
const isoDay = d => d.toLocaleDateString('sv-SE');
const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const monday = d => addDays(d, -((d.getDay() + 6) % 7));
const hhmm = d => d.toLocaleTimeString('sv-SE', {hour: '2-digit', minute: '2-digit'});
function isoWeek(d) {
  const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  t.setUTCDate(t.getUTCDate() + 4 - (t.getUTCDay() || 7));
  return Math.ceil(((t - Date.UTC(t.getUTCFullYear(), 0, 1)) / 864e5 + 1) / 7);
}
// The days a mode shows around the anchor day.
function calDays(mode, anchor) {
  if (mode === 'day') return [anchor];
  const first = monday(anchor);
  return Array.from({length: mode === 'week' ? 7 : 5}, (_, i) => addDays(first, i));
}
function calTitle(days) {
  const a = days[0], b = days[days.length - 1];
  const opt = {day: 'numeric', month: 'short'};
  if (days.length === 1) return a.toLocaleDateString('en-GB', {weekday: 'long', day: 'numeric', month: 'long', year: 'numeric'});
  return `${a.toLocaleDateString('en-GB', opt)} – ${b.toLocaleDateString('en-GB', opt)} ${b.getFullYear()}`;
}
function calMove(n) {
  const step = CAL.mode === 'day' ? 1 : 7;
  CAL.day = isoDay(addDays(parseDay(CAL.day || todayISO()), n * step));
  render(true);
}
function calToday() { CAL.day = todayISO(); CAL.scroll = null; render(true); }

// Timed entries of one day, clipped to it, laid out side by side where they overlap: each cluster of
// overlapping entries shares the width, an entry taking the first column free at its start.
function calLayoutDay(entries, day) {
  const d0 = day.getTime(), d1 = addDays(day, 1).getTime();
  const items = entries.map(e => ({e, s: new Date(e.start).getTime(), t: new Date(e.end).getTime()}))
    .filter(x => x.s < d1 && (x.t > d0 || (x.t === x.s && x.s >= d0)))
    .map(x => ({e: x.e, s: Math.max(0, (x.s - d0) / 6e4), t: Math.min(1440, (x.t - d0) / 6e4)}))
    .sort((a, b) => a.s - b.s || b.t - a.t);
  let cluster = [], cols = [], end = -1;
  const flush = () => { for (const it of cluster) it.n = cols.length; cluster = []; cols = []; end = -1; };
  for (const it of items) {
    const vis = Math.max(it.t, it.s + 20);  // a short entry still takes room enough to read
    if (cluster.length && it.s >= end) flush();
    let c = cols.findIndex(x => x <= it.s);
    if (c < 0) { c = cols.length; cols.push(vis); } else cols[c] = vis;
    it.col = c; cluster.push(it); end = Math.max(end, vis);
  }
  flush();
  return items;
}

async function viewCalendar() {
  if (!CAL.day) CAL.day = todayISO();
  if (innerWidth < 820 && !CAL.phone) { CAL.phone = true; CAL.mode = 'day'; }  // a work week does not fit a phone
  const anchor = parseDay(CAL.day), days = calDays(CAL.mode, anchor);
  const d = await api(`/api/calendar?start=${isoDay(days[0])}&end=${isoDay(days[days.length - 1])}`);
  const cals = d.calendars, byId = Object.fromEntries(cals.map(c => [c.id, c]));
  const shown = d.entries.filter(e => (byId[e.calendar_id] || {}).visible);
  const today = todayISO();
  const showsToday = days.some(x => isoDay(x) === today);
  const right = [
    h('button', {class: 'btn sm', disabled: showsToday && CAL.day === today, title: 'Go to today', onclick: calToday}, 'Today'),
    h('div', {class: 'seg', role: 'group', 'aria-label': 'Move'},
      h('button', {'aria-label': CAL.mode === 'day' ? 'Previous day' : 'Previous week', title: 'Back', onclick: () => calMove(-1)}, '‹'),
      h('button', {'aria-label': CAL.mode === 'day' ? 'Next day' : 'Next week', title: 'Forward', onclick: () => calMove(1)}, '›')),
    seg(CAL_MODES, CAL.mode, v => { CAL.mode = v; try { localStorage.setItem('talos-cal-mode', v); } catch (e) {} render(true); }),
    h('button', {class: 'btn sm primary', onclick: () => newCalEntry(cals)}, 'New entry')];
  const view = h('div', {class: 'cal'},
    header('Calendar', `Week ${isoWeek(days[0])}${days.length > 1 && isoWeek(days[days.length - 1]) !== isoWeek(days[0]) ? '–' + isoWeek(days[days.length - 1]) : ''} · ${calTitle(days)}`, right),
    h('div', {class: 'cal-wrap'}, calSidebar(cals, d.synced, d.syncs), calGrid(days, shown, byId, cals)));
  calLive(d);
  return view;
}
// Live: the 5-minute sync keeps the copy fresh; a page opened on an older copy asks for a new one
// behind it (at most every CAL_FRESH_MS) and redraws in place when it is in.
const CAL_FRESH_MS = 3 * 60 * 1000;
function calLive(d) {
  const last = Math.max(0, ...Object.values(d.syncs || {}).map(s => Date.parse(s.at) || 0));
  if (Date.now() - last < CAL_FRESH_MS || Date.now() - (CAL.synced || 0) < CAL_FRESH_MS || CAL.syncing) return;
  CAL.syncing = true;
  post('/api/calendar/sync', {}).catch(() => {}).finally(() => {
    CAL.syncing = false; CAL.synced = Date.now();
    if (S.view === 'calendar') render(true);
  });
}

// ---- the sidebar: the calendars, which are also the legend.
// The sources, in the sidebar's order, and where each is changed when Talos cannot.
const CAL_SOURCES = [['talos', 'Talos'], ['m365', 'Microsoft 365'], ['icloud', 'iCloud'], ['google', 'Google']];
function calSidebar(cals, synced, syncs) {
  const talos = cals.filter(c => c.source === 'talos');
  const writable = cals.filter(c => c.writable !== false && (c.source === 'talos' || c.writable));
  const toggle = async (c, on) => { try { await post(`/api/calendar/calendars/${c.id}`, {visible: on}); } catch (e) { flash(errText(e)); } render(true); };
  const row = c => h('div', {class: 'cal-row' + (c.visible ? '' : ' off'), style: `--cc:${calColor(c.color)}`},
    h('input', {type: 'checkbox', class: 'cal-check', id: 'calv-' + c.id, checked: c.visible, 'aria-label': `Show ${c.name}`, onchange: e => toggle(c, e.target.checked)}),
    h('label', {class: 'cal-name', for: 'calv-' + c.id}, c.name),
    c.is_default ? h('span', {class: 'pill ac', title: 'New entries go here'}, 'default') : null,
    h('button', {class: 'cal-set', title: `Colour and settings for ${c.name}`, 'aria-label': `Settings for ${c.name}`, onclick: () => calSettings(c, cals)}, '⋯'));
  const def = h('select', {class: 'sel', id: 'cal-default', onchange: async e => {
    try { await post(`/api/calendar/calendars/${e.target.value}`, {is_default: true}); } catch (x) { flash(errText(x)); } render(true); }},
    CAL_SOURCES.map(([k, label]) => { const l = writable.filter(c => c.source === k);
      return l.length ? h('optgroup', {label}, l.map(c => h('option', {value: c.id}, c.name))) : null; }));
  def.value = String((cals.find(c => c.is_default) || talos[0] || {}).id || '');
  const refresh = async btn => {
    btn.disabled = true; fill(btn, 'Refreshing…');
    try {
      const r = await post('/api/calendar/sync', {});
      const bad = Object.entries(r.accounts || {}).filter(([, v]) => v.error);
      flash(bad.length ? `A calendar could not be read: ${bad[0][1].error}` : 'The calendars are up to date.');
    } catch (e) { flash(errText(e)); }
    CAL.synced = Date.now();
    render(true);
  };
  // A source's group says why it is read-only, when all of it is (not signed in, no permission yet).
  const group = ([k, label]) => {
    const l = cals.filter(c => c.source === k);
    if (!l.length) return null;
    // The note the owner can act on (a permission, a sign-in), not "this calendar is read-only", which only says so.
    const notes = [...new Set(l.filter(c => !c.writable && c.write_note && c.write_note !== 'this calendar is read-only').map(c => c.write_note))];
    const allRO = k !== 'talos' && l.every(c => !c.writable);
    const s = (syncs || {})[k];
    return [h('h4', null, label, allRO ? h('span', {class: 'small muted', style: 'font-weight:400', title: notes.join('; ')}, ' · read-only') : null),
      allRO && notes.length ? h('div', {class: 'small muted cal-note'}, notes[0]) : null,
      l.map(row),
      k === 'talos' ? h('button', {class: 'linkbtn cal-new', onclick: () => calSettings(null, cals)}, '+ New calendar') : null,
      s && s.failed && s.failed.length ? h('div', {class: 'small muted'}, `Not readable: ${s.failed.join(', ')}`) : null];
  };
  const last = Object.values(syncs || {}).map(s => s.at).sort().pop() || (synced && synced.at);
  return h('aside', {class: 'cal-side', 'aria-label': 'Calendars'},
    CAL_SOURCES.map(group),
    cals.some(c => c.source !== 'talos') ? null : h('div', {class: 'small muted'}, 'Your other calendars are not copied yet. Refresh reads them.'),
    h('div', {class: 'cal-foot'},
      h('label', {class: 'flabel', for: 'cal-default'}, 'New entries go to'), def,
      h('div', {class: 'small muted'}, last ? `Calendars copied ${when(last)}` : 'Not copied yet'),
      h('button', {class: 'btn ghost sm', onclick: e => refresh(e.currentTarget)}, 'Refresh')));
}

// ---- the grid: a row of all-day entries, then the hours; the current time as a line on today.
function calGrid(days, entries, byId, cals) {
  const today = todayISO();
  const allDay = entries.filter(e => e.all_day), timed = entries.filter(e => !e.all_day);
  const cols = `grid-template-columns:52px repeat(${days.length}, minmax(0, 1fr))`;
  const chip = e => {
    const c = byId[e.calendar_id] || {};
    return h('button', {class: 'cal-chip' + (e.cancelled ? ' cancelled' : ''), style: `--cc:${calColor(c.color)}`, 'data-pane-key': 'cal:' + e.id,
      title: `${e.title} · ${c.name || ''}`, onclick: () => openCalEntry(e, cals)}, e.title);
  };
  const head = h('div', {class: 'cal-head', style: cols}, h('div'),
    days.map(x => h('div', {class: 'cal-dh' + (isoDay(x) === today ? ' today' : ''), role: 'button', tabindex: '0', title: 'Show this day',
      onclick: () => { CAL.day = isoDay(x); CAL.mode = 'day'; render(true); }, onkeydown: e => { if (e.key === 'Enter') e.currentTarget.click(); }},
      h('span', {class: 'cal-wd'}, x.toLocaleDateString('en-GB', {weekday: 'short'})), h('span', {class: 'cal-dn'}, x.getDate()))));
  const allRow = h('div', {class: 'cal-allday', style: cols}, h('div', {class: 'cal-gut small muted'}, 'all day'),
    days.map(x => {
      // Two entries a day, the cancelled ones last; the rest behind "+N more", which opens the whole row.
      const iso = isoDay(x), list = allDay.filter(e => e.start_date <= iso && e.end_date >= iso).sort((a, b) => a.cancelled - b.cancelled);
      const shown = CAL.allOpen ? list : list.slice(0, ALLDAY_SHOWN);
      return h('div', {class: 'cal-adc'}, shown.map(chip),
        list.length > ALLDAY_SHOWN ? h('button', {class: 'cal-more', onclick: () => { CAL.allOpen = !CAL.allOpen; render(true); }},
          CAL.allOpen ? 'Show fewer' : `+${list.length - shown.length} more`) : null);
    }));
  const hours = h('div', {class: 'cal-gutter'}, Array.from({length: 24}, (_, i) => h('div', {class: 'cal-hr', style: `height:${HOUR_PX}px`}, i ? String(i).padStart(2, '0') + ':00' : '')));
  const nowMin = (() => { const n = new Date(); return n.getHours() * 60 + n.getMinutes(); })();
  const dayCol = x => {
    const iso = isoDay(x);
    const col = h('div', {class: 'cal-col' + (iso === today ? ' today' : ''), style: `height:${24 * HOUR_PX}px`,
      title: 'Click to make an entry here',
      onclick: e => {
        if (e.target !== e.currentTarget) return;
        const y = e.clientY - e.currentTarget.getBoundingClientRect().top;
        const min = Math.max(0, Math.min(1410, Math.floor(y / HOUR_PX * 2) * 30));
        const s = new Date(x.getFullYear(), x.getMonth(), x.getDate(), 0, min);
        newCalEntry(cals, {start: s, end: new Date(s.getTime() + 36e5)});
      }});
    for (const it of calLayoutDay(timed, x)) {
      const c = byId[it.e.calendar_id] || {};
      const top = it.s / 60 * HOUR_PX, height = Math.max(18, (it.t - it.s) / 60 * HOUR_PX - 2);
      const w = 100 / it.n;
      const s0 = new Date(it.e.start), s1 = new Date(it.e.end);
      col.append(h('button', {class: 'cal-ev' + (it.e.cancelled ? ' cancelled' : '') + (it.e.show_as === 'free' ? ' free' : '') + (it.e.show_as === 'tentative' ? ' tentative' : ''),
        style: `--cc:${calColor(c.color)};top:${top}px;height:${height}px;left:calc(${it.col * w}% + 1px);width:calc(${w}% - 3px)`,
        'data-pane-key': 'cal:' + it.e.id, title: `${hhmm(s0)}–${hhmm(s1)} ${it.e.title}${it.e.location ? ' · ' + it.e.location : ''} · ${c.name || ''}`,
        onclick: () => openCalEntry(it.e, cals)},
        h('span', {class: 'cal-evt'}, it.e.title),
        height >= 34 ? h('span', {class: 'cal-evm'}, `${hhmm(s0)}–${hhmm(s1)}${it.e.location ? ' · ' + it.e.location : ''}`) : null));
    }
    if (iso === today) col.append(h('div', {class: 'cal-now', style: `top:${nowMin / 60 * HOUR_PX}px`, 'aria-hidden': 'true'}));
    return col;
  };
  const body = h('div', {class: 'cal-body', onscroll: e => { CAL.scroll = e.currentTarget.scrollTop; }},
    h('div', {class: 'cal-hours', style: cols}, hours, days.map(dayCol)));
  // The hours open at the morning (or where the owner left them) once the grid is in the page.
  setTimeout(() => { body.scrollTop = CAL.scroll != null ? CAL.scroll : CAL_START_HOUR * HOUR_PX - 10; }, 0);
  const legend = h('div', {class: 'legend cal-legend'}, [...new Set(entries.map(e => e.calendar_id))].map(id => byId[id]).filter(Boolean)
    .map(c => h('span', null, h('span', {class: 'dot', style: `background:${calColor(c.color)}`}), c.name)));
  return h('div', {class: 'cal-main'}, legend, h('div', {class: 'cal-grid'}, head, allRow, body));
}

// ---- an entry in the pane: a form for Talos's own, details for the Microsoft 365 copy.
function openCalEntry(e, cals) {
  const cal = cals.find(c => c.id === e.calendar_id) || {};
  openPane(e.read_only ? calDetails(e, cal) : calForm(e, cals), {key: 'cal:' + e.id, title: e.read_only ? 'Calendar entry' : 'Edit entry', label: e.title});
}
function newCalEntry(cals, pre) {
  const s = pre && pre.start || (() => { const n = new Date(); n.setMinutes(n.getMinutes() < 30 ? 30 : 60, 0, 0); return n; })();
  const e = pre && pre.end || new Date(s.getTime() + 36e5);
  openPane(calForm(null, cals, {start: s, end: e}), {key: 'cal:new', title: 'New entry'});
  const t = document.getElementById('ce-title');
  if (t) t.focus();
}
function calWhen(e) {
  if (e.all_day) {
    const a = parseDay(e.start_date), b = parseDay(e.end_date), o = {weekday: 'short', day: 'numeric', month: 'short', year: 'numeric'};
    return e.start_date === e.end_date ? `${a.toLocaleDateString('en-GB', o)}, all day` : `${a.toLocaleDateString('en-GB', o)} – ${b.toLocaleDateString('en-GB', o)}, all day`;
  }
  const a = new Date(e.start), b = new Date(e.end), o = {weekday: 'short', day: 'numeric', month: 'short', year: 'numeric'};
  return isoDay(a) === isoDay(b) ? `${a.toLocaleDateString('en-GB', o)}, ${hhmm(a)}–${hhmm(b)}` : `${a.toLocaleDateString('en-GB', o)} ${hhmm(a)} – ${b.toLocaleDateString('en-GB', o)} ${hhmm(b)}`;
}
const CAL_ALERTS = [['', 'None'], ['0', 'At the start'], ['5', '5 minutes before'], ['10', '10 minutes before'], ['15', '15 minutes before'],
                    ['30', '30 minutes before'], ['60', '1 hour before'], ['120', '2 hours before'], ['1440', '1 day before'], ['2880', '2 days before'],
                    ['10080', '1 week before']];
const SHOW_AS_LABEL = {free: 'Free', tentative: 'Tentative', busy: 'Busy', oof: 'Away', workingElsewhere: 'Working elsewhere'};
const RESPONSE_LABEL = {accepted: 'accepted', declined: 'declined', tentativelyAccepted: 'tentative', none: 'no answer', notResponded: 'no answer', organizer: 'organiser'};
// Outlook's and Teams' own links only: the entry is a copy of data from outside, so any other address is not offered.
const outlookLink = u => { const x = safeLink(u); return x && /^https:\/\/(outlook\.office365\.com|outlook\.office\.com|outlook\.live\.com)\//i.test(x) ? x : null; };
const googleLink = u => { const x = safeLink(u); return x && /^https:\/\/(www|calendar)\.google\.com\//i.test(x) ? x : null; };
const meetLink = u => { const x = safeLink(u); return x && /^https:\/\/meet\.google\.com\//i.test(x) ? x : null; };
const teamsLink = u => { const x = safeLink(u); return x && /^https:\/\/teams\.microsoft\.com\//i.test(x) ? x : null; };
function calDetails(e, cal) {
  const kv = (k, v) => v ? [h('dt', null, k), h('dd', null, v)] : null;
  const people = (e.attendees || []).map(a => h('div', {class: 'small'}, a.name || a.address, a.response && a.response !== 'none' ? h('span', {class: 'muted'}, ` · ${RESPONSE_LABEL[a.response] || a.response}`) : null));
  const g = e.source === 'google';
  const web = g ? googleLink(e.web_link) : outlookLink(e.web_link), join = g ? meetLink(e.join_url) : teamsLink(e.join_url);
  const app = {m365: 'Outlook', icloud: 'Calendar on your Mac or iPhone', google: 'Google Calendar'}[e.source] || 'its own calendar';
  return h('div', {class: 'wdrawer'},
    h('div', {class: 'meta', style: 'margin:0 0 6px'}, h('span', {class: 'cal-tag', style: `--cc:${calColor(cal.color)}`}, cal.name || 'Calendar'),
      e.cancelled ? h('span', {class: 'pill hi'}, 'cancelled') : null),
    h('h2', {class: e.cancelled ? 'struck' : null}, e.title),
    h('dl', {class: 'cal-dl'},
      kv('When', calWhen(e)), kv('Where', e.location), kv('Show as', SHOW_AS_LABEL[e.show_as] || e.show_as),
      kv('Organiser', e.organizer && (e.organizer.name || e.organizer.address)), kv('Your answer', e.response && e.response !== 'none' ? RESPONSE_LABEL[e.response] || e.response : null)),
    e.body ? h('div', {class: 'dsec'}, h('h4', null, 'Text'), h('div', {class: 'body-text'}, e.body)) : null,
    people.length ? h('div', {class: 'dsec'}, h('h4', null, `People · ${e.attendee_count || people.length}`), people) : null,
    h('div', {class: 'dactions', style: 'margin-top:12px'},
      join ? h('a', {class: 'btn primary', href: join, target: '_blank', rel: 'noopener noreferrer'}, g ? 'Join in Meet' : 'Join in Teams') : null,
      web ? h('a', {class: 'btn', href: web, target: '_blank', rel: 'noopener noreferrer'}, g ? 'Open in Google Calendar' : 'Open in Outlook') : null,
      h('button', {class: 'btn ghost', onclick: () => closePane()}, 'Close')),
    note(`Not editable here: ${e.read_only_reason || `change it in ${app}`}. Talos picks up changes within five minutes, or at once with Refresh.`));
}
function calForm(e, cals, pre) {
  const isNew = !e;
  const talos = cals.filter(c => c.source === 'talos');
  const allDay0 = !!(e && e.all_day);
  const s0 = e ? (e.all_day ? parseDay(e.start_date) : new Date(e.start)) : pre.start;
  const e0 = e ? (e.all_day ? parseDay(e.end_date) : new Date(e.end)) : pre.end;
  const title = h('input', {class: 'field wtitle', id: 'ce-title', value: e ? e.title : (pre.title || ''), maxlength: '500', autocomplete: 'off'});
  const workId = e ? e.work_item_id : pre.work_item_id;
  // Where it can go: a new entry to any calendar Talos can write; a Talos entry between Talos calendars;
  // an entry of a real calendar stays in it (moving is done in that calendar's own app).
  const mine = e ? cals.find(c => c.id === e.calendar_id) : null;
  const choices = !e ? cals.filter(c => c.source === 'talos' || c.writable) : mine && mine.source !== 'talos' ? [mine] : talos;
  const cal = h('select', {class: 'sel', id: 'ce-cal', disabled: choices.length < 2},
    CAL_SOURCES.map(([k, label]) => { const l = choices.filter(c => c.source === k);
      return l.length ? h('optgroup', {label}, l.map(c => h('option', {value: c.id}, c.name))) : null; }));
  cal.value = String(e ? e.calendar_id : (choices.find(c => c.is_default) || choices[0] || {}).id);
  const chosen = () => cals.find(x => String(x.id) === cal.value) || {};
  const swatch = h('span', {class: 'cal-sw', 'aria-hidden': 'true'});
  const where = h('span', {class: 'small muted', style: 'align-self:center'});
  const alertRow = h('div', {class: 'cal-alert-row'});
  const paint = () => {
    const c = chosen();
    swatch.style.background = calColor(c.color || 1);
    alertRow.hidden = c.source === 'talos';
    fill(where, c.source && c.source !== 'talos' ? `Saving writes to ${CAL_SOURCES.find(([k]) => k === c.source)[1]} at once.` : '');
  };
  cal.addEventListener('change', paint);
  const allDay = h('input', {type: 'checkbox', class: 'switch', id: 'ce-allday', checked: allDay0});
  const sd = h('input', {class: 'field wdate', type: 'date', id: 'ce-sd', value: isoDay(s0)});
  const st = h('input', {class: 'field wdate', type: 'time', id: 'ce-st', step: '900', value: hhmm(s0)});
  const ed = h('input', {class: 'field wdate', type: 'date', id: 'ce-ed', value: isoDay(e0)});
  const et = h('input', {class: 'field wdate', type: 'time', id: 'ce-et', step: '900', value: hhmm(e0)});
  const loc = h('input', {class: 'field', id: 'ce-loc', value: e ? e.location : '', maxlength: '500', autocomplete: 'off'});
  const showAs = h('select', {class: 'sel', id: 'ce-show'}, Object.entries(SHOW_AS_LABEL).map(([v, l]) => h('option', {value: v}, l)));
  showAs.value = e ? e.show_as : 'busy';
  const body = h('textarea', {class: 'field wbody', id: 'ce-body', rows: '6', placeholder: 'Notes', disabled: !!(e && e.body_partial)}, e ? e.body : '');
  // The alert fires in the real calendar (Outlook, iCloud, Google), on the owner's phone and computer.
  const alert = h('select', {class: 'sel', id: 'ce-alert'}, CAL_ALERTS.map(([v, l]) => h('option', {value: v}, l)));
  alert.value = e && e.reminder_minutes != null ? String(e.reminder_minutes) : (e ? '' : '15');
  if (![...alert.options].some(o => o.value === alert.value)) alert.append(h('option', {value: alert.value}, `${alert.value} minutes before`));
  alert.value = e && e.reminder_minutes != null ? String(e.reminder_minutes) : (e ? '' : '15');
  fill(alertRow, h('label', {class: 'flabel', for: 'ce-alert'}, 'Alert'), h('div', {class: 'wfield'}, alert));
  paint();
  const times = () => {
    st.hidden = et.hidden = allDay.checked;
    // An all-day entry made timed gets working hours rather than midnight to midnight.
    if (!allDay.checked && st.value === et.value && sd.value === ed.value) { st.value = '09:00'; et.value = '10:00'; }
    keepSpan();
  };
  // Moving the start keeps the length: the end moves with it.
  const at = (dEl, tEl) => new Date(`${dEl.value}T${allDay.checked ? '00:00' : tEl.value || '00:00'}`);
  let span = at(ed, et) - at(sd, st);
  allDay.addEventListener('change', times); st.hidden = et.hidden = allDay.checked;
  const shift = () => { const s = at(sd, st); if (isNaN(s)) return; const n = new Date(s.getTime() + Math.max(0, span)); ed.value = isoDay(n); et.value = hhmm(n); };
  sd.addEventListener('change', shift); st.addEventListener('change', shift);
  const keepSpan = () => { const x = at(ed, et) - at(sd, st); if (!isNaN(x)) span = x; };
  ed.addEventListener('change', keepSpan); et.addEventListener('change', keepSpan);
  const err = h('div', {class: 'err', role: 'alert'});
  const saveBtn = h('button', {class: 'btn primary', onclick: () => save()}, isNew ? 'Create' : 'Save');
  const save = async () => {
    err.replaceChildren();
    if (!title.value.trim()) { err.replaceChildren('An entry needs a title.'); title.focus(); return; }
    if (!sd.value || !ed.value || (!allDay.checked && (!st.value || !et.value))) { err.replaceChildren('Give a start and an end.'); return; }
    const payload = {calendar_id: Number(cal.value), title: title.value.trim(), all_day: allDay.checked, location: loc.value, show_as: showAs.value,
                     ...(body.disabled ? {} : {body: body.value}), ...(alertRow.hidden ? {} : {reminder_minutes: alert.value === '' ? null : Number(alert.value)}),
                     start: allDay.checked ? sd.value : at(sd, st).toISOString(), end: allDay.checked ? ed.value : at(ed, et).toISOString(),
                     ...(isNew && workId ? {work_item_id: workId} : {})};
    saveBtn.disabled = true;
    let res;
    try { res = await post(isNew ? '/api/calendar/entries' : `/api/calendar/entries/${e.id}`, payload); }
    catch (x) { saveBtn.disabled = false; err.replaceChildren(errText(x)); return; }
    // Show the week it landed in, and the entry in the pane.
    CAL.day = res.all_day ? res.start_date : isoDay(new Date(res.start));
    render(true);
    openPane(calForm(res, cals), {key: 'cal:' + res.id, title: 'Edit entry', label: res.title, replace: true});
    flash(isNew ? 'Entry created.' : 'Saved.');
  };
  // A Talos entry goes to Talos's trash (Undo brings it back); a real one to Outlook's Deleted Items or
  // Google's trash, restored from there. iCloud has no trash for one event, so it is not offered.
  const remove = async () => {
    const real = e.source && e.source !== 'talos';
    const bin = e.source === 'google' ? "Google Calendar's trash" : "Outlook's Deleted Items";
    if (real && !confirm(`Remove “${e.title}”? It goes to ${bin}; bring it back from there if you need to.`)) return;
    try { await post(`/api/calendar/entries/${e.id}`, {removed: true}); } catch (x) { flash(errText(x)); return; }
    closePane(false);
    render(true);
    if (real) { flash(`Removed; it is in ${bin}.`); return; }
    flash(['Entry removed. ', h('button', {class: 'linkbtn', onclick: async () => {
      try { await post(`/api/calendar/entries/${e.id}`, {removed: false}); } catch (x) { flash(errText(x)); return; }
      render(true); flash('Entry restored.');
    }}, 'Undo')]);
  };
  const row = (label, forId, ...el) => [h('label', {class: 'flabel', for: forId}, label), h('div', {class: 'wfield'}, h('span', {class: 'rrow'}, ...el))];
  return h('div', {class: 'wdrawer', onkeydown: x => { if (x.key === 'Enter' && (x.metaKey || x.ctrlKey)) { x.preventDefault(); save(); } }},
    h('h2', null, isNew ? 'New entry' : e.title),
    workId ? h('div', {class: 'small muted'}, 'Split off from ', h('button', {class: 'linkbtn', onclick: () => openWork(workId)}, pre && pre.work_title ? pre.work_title : 'a work item')) : null,
    h('div', {class: 'wform'},
      row('Title', 'ce-title', title),
      row('Calendar', 'ce-cal', swatch, cal),
      row('All day', 'ce-allday', allDay),
      row('Start', 'ce-sd', sd, st),
      row('End', 'ce-ed', ed, et),
      row('Where', 'ce-loc', loc),
      row('Show as', 'ce-show', showAs),
      alertRow),
    h('div', {class: 'dsec'}, h('h4', null, 'Notes'), body,
      e && e.body_partial ? h('div', {class: 'small muted'}, 'Outlook gives Talos only the start of these notes, so they are changed in Outlook.') : null),
    err,
    h('div', {class: 'dactions', style: 'margin-top:12px'}, saveBtn, h('button', {class: 'btn ghost', onclick: () => closePane()}, isNew ? 'Cancel' : 'Close'),
      isNew || e.removable === false ? null : h('button', {class: 'btn ghost', title: e.source && e.source !== 'talos' ? 'Remove it from the calendar (to its trash)' : 'Remove this entry (it goes to the trash; Undo brings it back)', onclick: remove}, 'Remove'),
      where,
      h('span', {class: 'small muted', style: 'align-self:center'}, '⌘/Ctrl + Enter saves')));
}

// ---- a calendar's settings (or a new Talos calendar): its name, its colour, shown or not, the default.
function calSettings(c, cals) {
  const isNew = !c;
  const talos = isNew || c.source === 'talos';
  let color = c ? c.color : ([1, 2, 3, 4, 5, 6, 7, 8].find(n => !cals.some(x => x.color === n)) || 1);
  const name = h('input', {class: 'field', id: 'cs-name', value: c ? c.name : '', maxlength: '120', disabled: !talos, autocomplete: 'off'});
  const swatches = h('div', {class: 'cal-swatches', role: 'radiogroup', 'aria-label': 'Colour'});
  const drawSw = () => fill(swatches, [1, 2, 3, 4, 5, 6, 7, 8].map(n => h('button', {class: 'cal-swb', role: 'radio', 'aria-checked': String(n === color),
    'aria-label': `Colour ${n}`, style: `--cc:${calColor(n)}`, onclick: () => { color = n; drawSw(); }},
    cals.filter(x => x.color === n && (!c || x.id !== c.id)).length ? h('span', {class: 'cal-used', title: 'Used by ' + cals.filter(x => x.color === n && (!c || x.id !== c.id)).map(x => x.name).join(', ')}, '•') : null)));
  drawSw();
  const err = h('div', {class: 'err', role: 'alert'});
  const save = async () => {
    err.replaceChildren();
    try {
      if (isNew) await post('/api/calendar/calendars', {name: name.value, color});
      else await post(`/api/calendar/calendars/${c.id}`, {color, ...(talos && name.value !== c.name ? {name: name.value} : {})});
    } catch (x) { err.replaceChildren(errText(x)); return; }
    closePane(false);
    render(true);
    flash(isNew ? 'Calendar made.' : 'Saved.');
  };
  return openPane(h('div', {class: 'wdrawer'},
    h('h2', null, isNew ? 'New calendar' : c.name),
    h('div', {class: 'wform'},
      h('label', {class: 'flabel', for: 'cs-name'}, 'Name'), h('div', {class: 'wfield'}, name,
        talos ? null : h('div', {class: 'small muted'}, 'It keeps the name it has in its own calendar app.')),
      h('span', {class: 'flabel'}, 'Colour'), h('div', {class: 'wfield'}, swatches, h('div', {class: 'small muted'}, 'A dot marks a colour another calendar has.'))),
    err,
    h('div', {class: 'dactions', style: 'margin-top:12px'}, h('button', {class: 'btn primary', onclick: save}, isNew ? 'Make it' : 'Save'),
      h('button', {class: 'btn ghost', onclick: () => closePane()}, 'Cancel'))),
  {key: 'cals:' + (c ? c.id : 'new'), title: isNew ? 'New calendar' : 'Calendar'});
}

// ---------------------------------------------------------------- the timeline (Work › Timeline)
// A Gantt over the binders (talos.timeline, docs/timeline.md): each binder a row with its span, its
// work items below as bars from start to due (a diamond when there is only a due date, a fading bar
// when there is only a start). Above them the load: the calendar's meeting hours per day and the
// items running, to see whether too much is on at once. An item opens in the work item pane, where
// "Make a calendar entry" splits a part of it off into the calendar.
const TL = {zoom: 'months', from: null, kind: '', binder: '', done: false, focus: false, undated: false, closed: new Set()};
const TL_ZOOMS = [['weeks', 'Weeks'], ['months', 'Months'], ['year', 'Year']];
const TL_SCALE = {weeks: {px: 30, days: 56, back: 7}, months: {px: 7, days: 182, back: 28}, year: {px: 2.6, days: 364, back: 56}};
try {
  const x = JSON.parse(localStorage.getItem('talos-timeline') || '{}');
  if (TL_SCALE[x.zoom]) TL.zoom = x.zoom;
  for (const k of ['kind', 'binder']) if (typeof x[k] === 'string') TL[k] = x[k];
  for (const k of ['done', 'focus', 'undated']) if (typeof x[k] === 'boolean') TL[k] = x[k];
  if (Array.isArray(x.closed)) TL.closed = new Set(x.closed.map(String));
} catch (e) {}
const tlSave = () => { try { localStorage.setItem('talos-timeline', JSON.stringify({zoom: TL.zoom, kind: TL.kind, binder: TL.binder, done: TL.done, focus: TL.focus, undated: TL.undated, closed: [...TL.closed]})); } catch (e) {} };
const TL_LABEL = 280;
const dayNo = iso => Math.round((parseDay(iso) - parseDay('2000-01-03')) / 864e5);  // whole days, safe across summer time

async function viewTimeline() {
  const sc = TL_SCALE[TL.zoom];
  if (!TL.from) TL.from = isoDay(addDays(monday(new Date()), -sc.back));
  const from = parseDay(TL.from), to = addDays(from, sc.days - 1);
  const p = new URLSearchParams({start: isoDay(from), end: isoDay(to)});
  if (TL.kind) p.set('kind', TL.kind);
  if (TL.binder) p.set('binder', TL.binder);
  if (TL.done) p.set('done', '1');
  if (TL.focus) p.set('focus', '1');
  const [d, homes] = await Promise.all([api('/api/timeline?' + p), api('/api/work/homes')]);
  const change = patch => { Object.assign(TL, patch); tlSave(); render(true); };
  const move = n => change({from: isoDay(addDays(from, n * Math.round(sc.days / 2)))});
  const kinds = h('select', {class: 'sel' + (TL.kind ? ' on' : ''), 'aria-label': 'Kind', onchange: e => change({kind: e.target.value})},
    h('option', {value: ''}, 'every kind'), BINDER_KINDS.map(k => h('option', {value: k}, KIND_PLURAL[k] || k)));
  kinds.value = TL.kind;
  const binder = homeSelect(homes, TL.binder, {'aria-label': 'Binder', class: 'sel' + (TL.binder ? ' on' : ''), onchange: e => change({binder: e.target.value})},
    [h('option', {value: ''}, 'every binder')]);
  const shownBinders = d.binders.filter(b => b.items.length || !b.derived || (TL.undated && b.undated.length));
  const filtered = TL.kind || TL.binder || TL.done || TL.focus;
  const todayIn = today => today >= isoDay(from) && today <= isoDay(to);
  return h('div', {class: 'tline'},
    header('Timeline', `${calTitle([from, to])} · ${fmt(shownBinders.length)} binder${shownBinders.length === 1 ? '' : 's'}` +
      (d.undated ? ` · ${fmt(d.undated)} work item${d.undated === 1 ? '' : 's'} without dates` : ''), [
      h('button', {class: 'btn sm', disabled: todayIn(todayISO()) && TL.from === isoDay(addDays(monday(new Date()), -sc.back)), title: 'Go to today',
        onclick: () => change({from: isoDay(addDays(monday(new Date()), -sc.back))})}, 'Today'),
      h('div', {class: 'seg', role: 'group', 'aria-label': 'Move'},
        h('button', {'aria-label': 'Earlier', title: 'Earlier', onclick: () => move(-1)}, '‹'),
        h('button', {'aria-label': 'Later', title: 'Later', onclick: () => move(1)}, '›')),
      // A new zoom opens around today, the way the page first opens.
      seg(TL_ZOOMS, TL.zoom, v => change({zoom: v, from: isoDay(addDays(monday(new Date()), -TL_SCALE[v].back))}))]),
    h('div', {class: 'fbar'},
      h('label', {class: 'fsel'}, h('span', {class: 'flabel'}, 'Kind'), kinds),
      h('label', {class: 'fsel'}, h('span', {class: 'flabel'}, 'Binder'), binder),
      h('label', {class: 'rrow small'}, h('input', {type: 'checkbox', checked: TL.focus, onchange: e => change({focus: e.target.checked})}), 'Focus only'),
      h('label', {class: 'rrow small'}, h('input', {type: 'checkbox', checked: TL.done, onchange: e => change({done: e.target.checked})}), 'Show done'),
      h('label', {class: 'rrow small'}, h('input', {type: 'checkbox', checked: TL.undated, onchange: e => change({undated: e.target.checked})}), 'Show items without dates'),
      filtered ? h('button', {class: 'btn ghost sm', onclick: () => change({kind: '', binder: '', done: false, focus: false})}, 'Reset') : null),
    shownBinders.length ? tlChart(d, shownBinders, from, sc) :
      emptyPage('Nothing on the timeline here', 'Give a work item a start or a due date (or a binder its own dates), and it shows up here. "Show items without dates" lists the ones waiting for a date.'),
    h('div', {class: 'legend'},
      h('span', null, h('span', {class: 'tl-key bar'}), 'start to due'),
      h('span', null, h('span', {class: 'tl-key mile'}), 'due date only'),
      h('span', null, h('span', {class: 'tl-key open'}), 'start only'),
      h('span', null, h('span', {class: 'tl-key ent'}), 'calendar entry split off'),
      h('span', null, h('span', {class: 'tl-key heat'}), 'meeting hours per day; red from 6 h'),
      STATUSES.filter(st => st !== 'done' || TL.done).map(st => h('span', null, h('span', {class: 'dot', style: `background:${STATUS_DOT[st]}`}), STATUS_LABEL[st]))));
}

function tlChart(d, binders, from, sc) {
  const px = sc.px, width = sc.days * px, base = dayNo(isoDay(from));
  const x = iso => (dayNo(iso) - base) * px;
  const track = (...kids) => h('div', {class: 'tl-track', style: `width:${width}px`}, ...kids);
  // The scale: months, and weeks (or days) under them.
  const months = [], ticks = [];
  for (let i = 0; i < sc.days; i++) {
    const day = addDays(from, i);
    if ((i === 0 && (new Date(day.getFullYear(), day.getMonth() + 1, 0).getDate() - day.getDate() + 1) * px >= 64) || day.getDate() === 1) months.push(h('span', {class: 'tl-mon', style: `left:${i * px}px`}, day.toLocaleDateString('en-GB', {month: 'short', year: i === 0 || day.getMonth() === 0 ? 'numeric' : undefined})));
    if (TL.zoom === 'weeks') ticks.push(h('span', {class: 'tl-tick' + (day.getDay() % 6 === 0 ? ' we' : ''), style: `left:${i * px}px;width:${px}px`}, day.getDate()));
    else if (day.getDay() === 1 && TL.zoom === 'months') ticks.push(h('span', {class: 'tl-tick', style: `left:${i * px}px`}, 'w' + isoWeek(day)));
  }
  const scale = h('div', {class: 'tl-row tl-scale'}, h('div', {class: 'tl-lab'}), track(h('div', {class: 'tl-mons'}, months), h('div', {class: 'tl-ticks'}, ticks)));
  // The load: meeting hours as a heat strip, and the items running as small columns.
  const maxRun = Math.max(1, ...d.load.map(l => l.active));
  const tip = l => `${parseDay(l.day).toLocaleDateString('en-GB', {weekday: 'short', day: 'numeric', month: 'short'})}: ${l.busy_hours} h in meetings, ${l.active} item${l.active === 1 ? '' : 's'} running${l.due ? `, ${l.due} due` : ''}`;
  const heat = d.load.map(l => h('span', {class: 'tl-heat' + (l.busy_hours >= 6 ? ' full' : ''), title: tip(l),
    style: `left:${x(l.day)}px;width:${Math.max(px, 1)}px;--a:${Math.min(1, l.busy_hours / 8)}`}));
  const runs = d.load.map(l => l.active || l.due ? h('span', {class: 'tl-run', title: tip(l),
    style: `left:${x(l.day)}px;width:${Math.max(px - 1, 1)}px;height:${Math.round(l.active / maxRun * 100)}%`}, l.due && TL.zoom === 'weeks' ? h('i', {class: 'tl-due'}) : null) : null);
  const load = [
    h('div', {class: 'tl-row tl-load'}, h('div', {class: 'tl-lab'}, h('b', null, 'Meetings'), h('span', {class: 'small muted'}, 'hours per day, visible calendars')), track(heat)),
    h('div', {class: 'tl-row tl-load'}, h('div', {class: 'tl-lab'}, h('b', null, 'Running'), h('span', {class: 'small muted'}, `work items with a start and due · most ${maxRun}`)), track(runs))];
  // A bar from a to b (dates, b inclusive), clipped to the window; the cut ends are marked.
  const span = (a, b, cls, attrs, ...kids) => {
    const l = Math.max(0, x(a)), r = Math.min(width, x(b) + px);
    if (r <= 0 || l >= width) return null;
    return h('button', {class: cls + (x(a) < 0 ? ' cut-l' : '') + (x(b) + px > width ? ' cut-r' : ''), style: `left:${l}px;width:${Math.max(r - l, 3)}px`, ...attrs}, ...kids);
  };
  const itemRow = (it, kind) => {
    const title = `${it.title} · ${STATUS_LABEL[it.status]}${it.start_on ? ' · from ' + it.start_on : ''}${it.due ? ' · due ' + it.due : ''}`;
    const open = {onclick: () => openWork(it.id), title, 'data-pane-key': 'w:' + it.id};
    const col = `--sc:${STATUS_DOT[it.status] || 'var(--axis)'}`;
    let mark;
    if (it.milestone) mark = x(it.due) >= 0 && x(it.due) < width ? h('button', {class: 'tl-mile', style: `left:${x(it.due) + px / 2}px;${col}`, ...open}) : null;
    else if (it.open_ended) mark = span(it.start_on, isoDay(addDays(from, sc.days - 1)), 'tl-bar open' + (it.focus ? ' focus' : ''), {...open}, TL.zoom !== 'year' ? h('span', null, it.title) : null);
    else mark = span(it.start_on, it.due, 'tl-bar' + (it.focus ? ' focus' : '') + (it.status === 'done' ? ' done' : ''), {...open}, TL.zoom !== 'year' ? h('span', null, it.title) : null);
    if (mark && !it.milestone) mark.style.cssText += ';' + col;
    const ents = (it.entries || []).map(e => { const iso = isoDay(new Date(e.start)); return x(iso) >= 0 && x(iso) < width ?
      h('button', {class: 'tl-ent', style: `left:${x(iso) + px / 2}px;--cc:${calColor(e.color)}`, title: `Calendar: ${e.title}, ${calWhen({...e, start_date: iso, end_date: iso})}`,
        onclick: () => { CAL.day = iso; CAL.scroll = null; go('calendar'); }}) : null; });
    return h('div', {class: 'tl-row tl-item', 'data-pane-key': 'w:' + it.id},
      h('div', {class: 'tl-lab'}, h('button', {class: 'tl-name', onclick: () => openWork(it.id), title},
        h('span', {class: 'dot', style: `background:${STATUS_DOT[it.status]}`}), h('span', {class: 'tl-t'}, it.title), it.focus ? focusChip(it) : null)),
      track(mark, ents));
  };
  const rows = binders.flatMap(b => {
    const key = String(b.id), closed = TL.closed.has(key);
    const toggle = () => { closed ? TL.closed.delete(key) : TL.closed.add(key); tlSave(); render(true); };
    const bar = b.span_start ? span(b.span_start, b.span_end || b.span_start, 'tl-gbar' + (b.derived ? ' derived' : '') + (b.kind ? ' ' + kindCls(b.kind) : ''),
      {title: `${b.name}: ${b.span_start} – ${b.span_end || '…'}${b.derived ? ' (from its items)' : ' (its own dates)'}`, onclick: () => tlBinderPane(b)}) : null;
    const head = h('div', {class: 'tl-row tl-group' + (b.kind ? ' ' + kindCls(b.kind) : '')},
      h('div', {class: 'tl-lab'},
        h('button', {class: 'tl-fold', 'aria-expanded': String(!closed), 'aria-label': (closed ? 'Open ' : 'Fold ') + b.name, onclick: toggle}, closed ? '▸' : '▾'),
        h('button', {class: 'tl-name tl-bname', title: b.id ? 'Dates and details' : 'Work items without a home', onclick: () => b.id ? tlBinderPane(b) : toggle()},
          b.kind ? kindBadge(b.kind, KIND_LABEL[b.kind] || b.kind) : null, h('span', {class: 'tl-t'}, b.name)),
        h('span', {class: 'small muted tl-n'}, `${b.items.length}${b.outside ? ` +${b.outside}` : ''}`)),
      track(bar));
    if (closed) return [head];
    const undated = TL.undated ? b.undated.map(it => h('div', {class: 'tl-row tl-item tl-undated', 'data-pane-key': 'w:' + it.id},
      h('div', {class: 'tl-lab'}, h('button', {class: 'tl-name', onclick: () => openWork(it.id), title: 'No dates yet: open it to give it a start or a due date'},
        h('span', {class: 'dot', style: `background:${STATUS_DOT[it.status]}`}), h('span', {class: 'tl-t'}, it.title))),
      track(h('span', {class: 'small muted tl-nodate'}, 'no dates')))) : [];
    return [head, ...b.items.map(it => itemRow(it, b.kind)), ...undated];
  });
  const t = todayISO();
  const now = x(t) >= 0 && x(t) < width ? h('div', {class: 'tl-today', style: `left:${TL_LABEL + x(t) + px / 2}px`, title: 'Today'}) : null;
  const weekend = TL.zoom === 'year' ? '' : `--we:${5 * px}px;--wk:${7 * px}px;`;
  return h('div', {class: 'tl-scroll'}, h('div', {class: 'tl-inner' + (TL.zoom === 'weeks' ? ' shade' : ''), style: `${weekend}width:${TL_LABEL + width}px;--lab:${TL_LABEL}px`},
    scale, load, rows, now));
}

// ---- the work item pane's Calendar section: entries split off from it, and making one. The entry is an
// ordinary calendar entry (in the default Talos calendar unless another is chosen), linked back here.
function workCalendar(v) {
  const make = async () => {
    let d;
    try { d = await api(`/api/calendar?start=${todayISO()}&end=${todayISO()}`); } catch (e) { flash(errText(e)); return; }
    const day = v.start_on && v.start_on > todayISO() ? parseDay(v.start_on) : new Date();
    const s = v.start_on && v.start_on > todayISO() ? new Date(day.getFullYear(), day.getMonth(), day.getDate(), 9, 0)
      : (() => { const n = new Date(); n.setMinutes(n.getMinutes() < 30 ? 30 : 60, 0, 0); return n; })();
    openPane(calForm(null, d.calendars, {start: s, end: new Date(s.getTime() + 36e5), title: v.title, work_item_id: v.id, work_title: v.title}),
      {key: 'cal:new', title: 'New entry'});
    const t = document.getElementById('ce-title');
    if (t) { t.focus(); t.select(); }
  };
  const list = v.calendar || [];
  return h('div', {class: 'dsec'}, h('div', {class: 'rrow', style: 'justify-content:space-between;margin-bottom:8px'},
      h('h4', {style: 'margin:0'}, list.length ? `Calendar · ${list.length}` : 'Calendar'),
      h('button', {class: 'btn sm', title: 'Split a part of this off into the calendar, as an entry of its own', onclick: make}, 'Make a calendar entry')),
    list.length ? list.map(e => h('div', {class: 'imp-row', tabindex: '0', role: 'button', title: 'Show it in the calendar',
      onclick: () => { CAL.day = e.all_day ? e.starts_at.slice(0, 10) : isoDay(new Date(e.starts_at)); CAL.scroll = null; go('calendar'); }},
      h('span', {class: 'imp-l'}, h('span', {class: 'acct-dot', style: `background:${calColor(e.color)}`})),
      h('span', {class: 'imp-m'}, h('b', null, e.title), h('span', {class: 'small muted'}, e.calendar)),
      h('span', {class: 'imp-r small muted'}, e.all_day ? e.starts_at.slice(0, 10) : `${isoDay(new Date(e.starts_at))} ${hhmm(new Date(e.starts_at))}`)))
      : h('div', {class: 'small muted'}, 'None yet. An entry made here is linked to this item and shows on its row in the Timeline.'));
}

// A binder's own dates: where set, its bar is them; cleared, it spans its items.
function tlBinderPane(b) {
  const s = h('input', {class: 'field wdate', type: 'date', id: 'tlb-s', value: b.starts_on || ''});
  const e = h('input', {class: 'field wdate', type: 'date', id: 'tlb-e', value: b.ends_on || ''});
  const err = h('div', {class: 'err', role: 'alert'});
  const save = async (starts_on, ends_on) => {
    try { await post(`/api/objects/${b.id}`, {starts_on, ends_on}); } catch (x) { err.replaceChildren(errText(x)); return; }
    closePane(false); render(true); flash('Saved.');
  };
  openPane(h('div', {class: 'wdrawer'},
    h('div', {class: 'meta', style: 'margin:0 0 6px'}, b.kind ? kindBadge(b.kind, KIND_LABEL[b.kind] || b.kind) : null),
    h('h2', null, b.name),
    h('p', {class: 'small muted'}, b.derived ? `Its bar follows its work items${b.span_start ? ` (${b.span_start} – ${b.span_end})` : ''}. Give it its own dates to fix them.` : 'Its bar shows its own dates.'),
    h('div', {class: 'wform'},
      h('label', {class: 'flabel', for: 'tlb-s'}, 'Start'), h('div', {class: 'wfield'}, s),
      h('label', {class: 'flabel', for: 'tlb-e'}, 'End'), h('div', {class: 'wfield'}, e)),
    err,
    h('div', {class: 'dactions', style: 'margin-top:12px'},
      h('button', {class: 'btn primary', onclick: () => save(s.value || null, e.value || null)}, 'Save'),
      b.derived ? null : h('button', {class: 'btn ghost', onclick: () => save(null, null)}, 'Follow the items'),
      h('button', {class: 'btn ghost', onclick: () => go('objects', b.id)}, 'Open the binder'))),
  {key: 'tlb:' + b.id, title: 'Binder on the timeline'});
}

// ---------------------------------------------------------------- the answer key
// Blind labelling of the gold set (talos.gold; docs/enrichment-plan.md §7). One item at a time,
// in rounds of six: the message on the left with its context (the conversation, the pattern's
// other samples, or the Teams window), the fields on the right. Nothing Talos has decided about the
// message is shown (the server does not even send it) until the round is done; then a summary
// sets the owner's answers beside the rules' and the pre-pass's. Every answer is saved as it is given, so
// the screen can be closed at any point and resumed where it was left.
//
// Every field's choices are its dimension's closed list (rules/taxonomy.json): each value with its
// family, label and one-line description, in the file's order. The field in hand lists them under
// their family headings, the description beside each; the others show the chosen value and its
// description. Type (45) and topic (60) scroll in a list of about ten.
//
// Keys: 1–9 and 0 choose among the first ten shown (or toggle, for ask and route); letters filter the
// choices by value, label and description, and Enter takes the first; Enter goes on (to the next field,
// the next item, the round's summary); ? is "not sure", - is "skip"; ↑ and ↓ move between the fields;
// ← and → between the items.
// A set labels its own fields (gold_set.params.label_fields; the first six by default, kind for the
// sets after 26 September): GOLD_FIELDS is set from the set shown, in its order.
const GOLD_ALL = [
  {id: 'origin', label: 'Origin'}, {id: 'kind', label: 'Kind'}, {id: 'type', label: 'Type'}, {id: 'topic', label: 'Topic'},
  {id: 'ask', label: 'Asks me', many: true, none: 'Nothing is asked of you, and you promised nothing'},
  {id: 'value', label: 'Value'}, {id: 'route', label: 'Route', many: true, none: 'It belongs to no work category'}];
const GOLD_DEFAULT = ['origin', 'type', 'topic', 'ask', 'value', 'route'];
let GOLD_FIELDS = goldFieldsOf(GOLD_DEFAULT);
function goldFieldsOf(ids) {
  return (ids && ids.length ? ids : GOLD_DEFAULT).map(id => GOLD_ALL.find(f => f.id === id)).filter(Boolean);
}
const GOLD = {sets: null, set: null, meta: null, pos: null, item: null, field: 0, filter: '', shownAt: 0, base: 0,
              summary: null, root: null, top: null, grid: null, panel: null, note: null, tab: null, token: 0, saving: Promise.resolve()};
const goldNice = v => String(v).replace(/_/g, ' ');
const goldElapsed = () => GOLD.base + Math.round(performance.now() - GOLD.shownAt);
const goldAnswered = f => !!(GOLD.item && GOLD.item.labels[f]);
const goldComplete = () => GOLD_FIELDS.every(f => goldAnswered(f.id));
const goldMeta = (field, v, o = GOLD.meta.options) => (o[field] || []).find(x => x.value === v);
const goldLabel = (field, v, o) => { const m = goldMeta(field, v, o); return m ? m.label : goldNice(v); };

async function viewGold() {
  const token = ++GOLD.token;
  const {sets} = await fetchJSON('/api/gold');
  GOLD.sets = sets;
  if (!sets.length) {
    GOLD.root = null;
    return h('div', null, header('Answer key', 'The gold set: about 300 items you label blind, to test rules and models against'),
      card('No answer key yet', 'Draw one from the archive first', null,
        h('p', null, 'In a terminal:'), h('pre', {class: 'cmds'}, 'uv run talos enrich gold sample'),
        note('The sample is frozen: drawing again makes a new set and never changes one you have started.')));
  }
  if (!sets.some(x => x.id === GOLD.set)) { GOLD.set = sets[0].id; GOLD.pos = null; }
  GOLD.meta = await fetchJSON(`/api/gold/${GOLD.set}`);
  if (token !== GOLD.token) return h('div');
  const prog = GOLD.meta.progress;
  GOLD_FIELDS = goldFieldsOf(prog.label_fields);
  if (!GOLD.pos) GOLD.pos = prog.next || prog.total;
  GOLD.top = h('div', {class: 'head-r gold-prog'});
  GOLD.grid = h('div', {class: 'gold-grid'});
  GOLD.root = h('div', {class: 'gold', tabindex: '-1'},
    header(withHelp('Answer key', 'answer-key'), 'Label blind: Talos shows none of its own answers until a round of six is done', GOLD.top),
    GOLD.grid);
  goldProgress(prog);
  if (GOLD.summary) goldSummary(GOLD.summary); else goldShow(GOLD.pos, {quiet: true});
  return GOLD.root;
}

function goldProgress(p) {
  if (!GOLD.top) return;
  const s = p.session;
  const sel = GOLD.sets.length > 1 ? h('select', {class: 'sel', 'aria-label': 'Answer key', onchange: e => { GOLD.set = Number(e.target.value); GOLD.pos = null; GOLD.summary = null; render(); }},
    GOLD.sets.map(x => h('option', {value: x.id, selected: x.id === GOLD.set}, `${x.name} · ${x.done}/${x.items}`))) : null;
  const chk = GOLD.meta && GOLD.meta.check;
  fill(GOLD.top, sel,
    chk ? h('button', {class: 'btn', title: 'Not blind: Claude’s answers beside each field; agree or correct them',
      onclick: () => { CHECK.set = GOLD.set; go('goldcheck'); }}, `Check Claude’s labels · ${chk.checked}/${chk.total}`) : null,
    h('div', {class: 'gold-count'}, h('b', null, `${fmt(p.done)} of ${fmt(p.total)}`), ' done',
      h('span', {class: 'gbar', 'aria-hidden': 'true'}, h('span', {style: `width:${p.total ? 100 * p.done / p.total : 0}%`}))),
    h('span', {class: 'pill', title: `A new session starts after 30 minutes without an answer · ${s.count} so far`},
      `Session ${s.number}` + (s.active && s.items_done ? ` · ${s.items_done} done` : '')));
}

async function goldShow(pos, opts = {}) {
  const t = ++GOLD.token;
  GOLD.summary = null;
  let it;
  try { it = await fetchJSON(`/api/gold/${GOLD.set}/items/${pos}`); }
  catch (e) { fill(GOLD.grid, h('div', {class: 'err'}, errText(e))); return; }
  if (t !== GOLD.token || !GOLD.root) return;
  GOLD_FIELDS = goldFieldsOf(it.fields);
  Object.assign(GOLD, {pos, item: it, filter: '', shownAt: performance.now(), base: it.duration_ms || 0, tab: it.unit === 'window' ? 'ctx' : 'msg'});
  const first = GOLD_FIELDS.findIndex(f => !it.labels[f.id]);
  GOLD.field = first < 0 ? GOLD_FIELDS.length : first;
  GOLD.panel = h('aside', {class: 'card gold-panel', 'aria-label': 'Your answer'});
  const msg = h('section', {class: 'card gold-msg', 'aria-label': 'The message'});
  fill(GOLD.grid, h('div', {class: 'gold-round'}, goldStrip()), msg, GOLD.panel);
  goldMessage(msg);
  goldPanel();
  if (!opts.quiet) scrollTo(0, 0);
  // In a chat window the line this item is about can be far down: bring it into view.
  const anchor = GOLD.tab === 'ctx' ? msg.querySelector('.bubble.gold-anchor') : null;
  if (anchor && anchor.getBoundingClientRect().bottom > innerHeight) anchor.scrollIntoView({block: 'center'});
  GOLD.root.focus({preventScroll: true});
}

// The round: six steps, each done or not; any of them can be opened.
function goldStrip() {
  const it = GOLD.item, rounds = GOLD.meta.progress.rounds;
  return [h('span', {class: 'small muted'}, `Round ${it.round} of ${rounds}`),
    h('div', {class: 'gsteps', role: 'group', 'aria-label': 'Items in this round'}, it.round_items.map(r =>
      h('button', {class: 'gstep' + (r.done ? ' done' : '') + (r.position === it.position ? ' cur' : ''), 'aria-current': r.position === it.position ? 'step' : null,
        title: `Item ${r.position}` + (r.done ? ' · done' : ''), onclick: () => goldShow(r.position)}, r.done ? '✓' : String((r.position - 1) % 6 + 1)))),
    h('span', {class: 'small muted'}, `Item ${fmt(it.position)} of ${fmt(it.total)}`)];
}

// ---- the message and its context, from the blind API only (no values, no importance, no labels).
function goldMessage(box, it = GOLD.item, st = GOLD) {
  const m = it.message;
  if (!m) { fill(box, empty('The message is gone', 'It was removed from the archive.')); return; }
  const people = role => (m.participants || []).filter(p => p.role === role).map(p => p.name ? `${p.name} <${p.address}>` : p.address).join(', ');
  const ctxLabel = it.unit === 'thread' ? `Conversation · ${fmt(it.thread_messages || it.context.length)}`
    : it.unit === 'pattern' ? `Pattern · ${fmt((it.pattern && it.pattern.message_count) || it.context.length + 1)} messages`
    : it.unit === 'window' ? `Chat window · ${fmt(it.context.length)}` : null;
  const body = h('div', {class: 'gold-body'});
  const draw = () => {
    if (st.tab === 'ctx' && ctxLabel) fill(body, goldContext(it));
    else fill(body, isTeams(m) ? h('div', {class: 'body-text'}, m.text || '(no text)') : mailText(m), attachmentsSection(m));
  };
  const tabsBox = h('div', {class: 'gold-tabs'});
  const tabsFor = () => seg([['msg', 'This message'], ['ctx', ctxLabel]], st.tab, v => { st.tab = v; fill(tabsBox, tabsFor()); draw(); });
  if (ctxLabel) fill(tabsBox, tabsFor());
  fill(box, goldGroup(it),
    h('div', {class: 'meta t-meta', style: 'margin:0 0 6px'}, h('span', null, acctDot(m.account_id), ' ', acctName(m.account_id)),
      isTeams(m) ? h('span', null, CHAT_KIND[it.chat_type || m.chat_type] || 'Teams') : null,
      h('span', null, m.direction === 'in' ? 'received' : 'sent'), h('span', null, new Date(m.received_at || 0).toLocaleString('sv-SE'))),
    h('h2', {class: 'gold-subj'}, m.subject || (isTeams(m) ? 'Teams chat' : '(no subject)')),
    h('dl', {class: 'kv gold-kv'},
      h('dt', {class: 'muted'}, 'from'), h('dd', null, m.from_name ? `${m.from_name} <${m.from_address || ''}>` : (m.from_address || '—')),
      ['to', 'cc'].filter(r => people(r)).map(r => [h('dt', {class: 'muted'}, r), h('dd', null, people(r))])),
    ctxLabel ? tabsBox : null, body);
  draw();
}
// A sender-group item stands for every unsure message of one sender (and system): how many, and a
// few of their subjects with dates, one per subject template. Subjects only: never a value.
function goldGroup(it) {
  const g = it.group;
  if (!g) return null;
  const who = String(g.sender || '').split('@')[0];
  return h('div', {class: 'gold-group', role: 'note'},
    h('div', {class: 'gold-group-h'}, h('b', null, `Stands for ${fmt(g.messages)} messages from this sender`),
      h('span', {class: 'small muted'}, ' · ' + [who, g.system, `${fmt(g.messages)} messages`].filter(Boolean).join(' · '))),
    h('p', {class: 'small muted', style: 'margin:2px 0 6px'}, 'Your answer is given to all of them, unless you tick “Mixed group” beside your answer. Some of their subjects:'),
    h('ul', {class: 'gold-group-ex'}, (g.examples || []).map(x => h('li', null,
      h('span', {class: 'num small muted'}, x.received_at ? day(x.received_at) : '—'), ' ', x.subject || '(no subject)',
      x.messages > 1 ? h('span', {class: 'small muted'}, ` ×${fmt(x.messages)}`) : null))));
}
function goldContext(it) {
  if (it.unit === 'window') {
    const box = h('div', {class: 'chat', role: 'log', 'aria-label': 'The chat window'},
      it.before.length ? [h('div', {class: 'gold-before'}, chatLines(it.before, it.chat_type !== 'oneOnOne', {blind: true})),
        h('div', {class: 'chat-day', role: 'separator'}, h('span', null, 'The window'))] : null,
      chatLines(it.context, it.chat_type !== 'oneOnOne', {blind: true}));
    const a = box.querySelector(`.bubble[data-id="${it.message.id}"]`);
    if (a) { a.classList.add('gold-anchor'); a.setAttribute('title', 'The message this item is about'); }
    return box;
  }
  const head = it.unit === 'pattern' && it.pattern
    ? h('p', {class: 'small muted', style: 'margin:0 0 8px'}, `This sender with this subject template: ${fmt(it.pattern.message_count)} messages in ${fmt(it.pattern.thread_count)} threads, ${day(it.pattern.first_at)} – ${day(it.pattern.last_at)}. The other samples:`)
    : it.unit === 'thread' && it.thread_messages > it.context.length ? h('p', {class: 'small muted', style: 'margin:0 0 8px'}, `The ${fmt(it.context.length)} messages nearest this one, of ${fmt(it.thread_messages)}.`) : null;
  const rows = it.unit === 'pattern' ? [it.message, ...it.context] : it.context;
  return h('div', {class: 'tmail', style: 'margin-top:0'}, head, rows.map(m => goldMailCard(m, m.id === it.message.id, it.unit === 'pattern' || m.id === it.message.id)));
}
function goldMailCard(m, anchor, startOpen) {
  const card = h('article', {class: 'tm-card' + (isMine(m) ? ' mine' : '') + (anchor ? ' gold-anchor' : '')});
  const body = h('div', {class: 'tm-body'}, mailText(m), attachmentsSection(m));
  const who = isMine(m) ? 'me' : (m.from_name || m.from_address || '?');
  const head = h('button', {class: 'tm-head', 'aria-expanded': 'false', title: m.from_address || ''},
    h('span', {class: 'cav', style: `--pc:${isMine(m) ? 'var(--accent)' : personColor(m.from_address || m.from_name)}`, 'aria-hidden': 'true'}, initials(isMine(m) ? 'me' : who)),
    h('span', {class: 'tm-who'}, who),
    h('span', {class: 'tm-snip'}, (m.subject ? m.subject + ' — ' : '') + String(m.quote_stripped || m.body_text || '').slice(0, 160)),
    h('span', {class: 'tm-marks'}, anchor ? h('span', {class: 'pill ac'}, 'this item') : null, m.has_attachments ? h('span', {class: 'pill'}, 'att') : null),
    h('span', {class: 'tm-date', title: new Date(m.received_at || 0).toLocaleString('sv-SE')}, when(m.received_at)));
  const toggle = open => { card.classList.toggle('open', open); head.setAttribute('aria-expanded', String(open)); body.hidden = !open; };
  head.addEventListener('click', () => toggle(!card.classList.contains('open')));
  add(card, [head, body]);
  toggle(!!startOpen);
  return card;
}

// ---- the answer: one row per field. The field in hand lists its choices; the others show what was chosen.
// The choices in the order they are shown (and numbered): grouped by family in the file's order, or,
// while a filter is typed, the matches best first (label or value starting with it, containing it,
// then the description containing it).
function goldOptions(f, st = GOLD) {
  const o = st.meta.options;
  let list = (o[f.id] || []).map(x => ({...x, label: x.label || x.value}));
  const q = GOLD_FIELDS[st.field] === f ? st.filter.trim().toLowerCase() : '';  // the filter is the field in hand's
  if (q) {
    const rank = x => {
      const l = x.label.toLowerCase(), v = x.value.toLowerCase();
      return l.startsWith(q) || v.startsWith(q) ? 0 : l.includes(q) || v.includes(q) ? 1 : (x.description || '').toLowerCase().includes(q) ? 2 : 3;
    };
    list = list.map(x => [rank(x), x]).filter(([r]) => r < 3).sort((a, b) => a[0] - b[0]).map(([, x]) => x);
    if (f.id === 'topic' && !o.topic_closed && !list.some(x => x.value.toLowerCase() === q)) list.push({value: st.filter.trim(), label: `new: ${st.filter.trim()}`, fresh: true});
    return list;
  }
  const fams = [];
  for (const x of list) if (!fams.includes(x.family || '')) fams.push(x.family || '');
  // A Common group first, so the likeliest choices get the number keys: the values the owner uses most in
  // the answer key, then the ones the taxonomy marks common. Not about the item in hand, so still blind.
  const pos = new Map(list.map((x, i) => [x.value, i]));
  const common = list.filter(x => x.used || x.common)
    .sort((a, b) => (b.used || 0) - (a.used || 0) || (a.common || 99) - (b.common || 99) || pos.get(a.value) - pos.get(b.value))
    .slice(0, f.many ? 8 : 9).map(x => ({...x, family: 'Common', copy: true}));
  list = [...common, ...fams.flatMap(fam => list.filter(x => (x.family || '') === fam))];
  return f.many ? [{value: '', label: 'none', description: f.none, family: ''}, ...list] : list;
}
// One choice: its key (the first ten), its label and its description, on one or two lines.
function goldOptRow(f, i, x, k, pressed, showFamily, pick, mark) {
  return h('button', {class: 'gopt' + (x.fresh ? ' fresh' : ''), 'aria-pressed': String(!!pressed), title: x.description || x.value,
    onclick: pick || (() => { goldField(i, true); goldChoose(f, x.value); })},
    h('span', {class: 'k' + (k < 10 ? '' : ' blank'), 'aria-hidden': k < 10 ? null : 'true'}, k < 10 ? String((k + 1) % 10) : ''),
    h('span', {class: 'go-t'}, h('span', {class: 'go-l'}, x.label), x.description ? h('span', {class: 'go-d'}, ' ' + x.description) : null),
    mark || null, showFamily && x.family ? h('span', {class: 'go-f'}, x.family) : null);
}
function goldPanel() {
  if (!GOLD.panel || !GOLD.item) return;
  const it = GOLD.item;
  const rows = GOLD_FIELDS.map((f, i) => {
    const lab = it.labels[f.id], on = i === GOLD.field;
    const vals = lab ? lab.values : [];
    const names = vals.map(v => goldLabel(f.id, v)).join(', ');
    const state = !lab ? null : lab.status === 'skip' ? 'skipped' : lab.status === 'unsure' ? 'not sure' + (vals.length ? ': ' + names : '')
      : vals.length ? names : 'none';
    let body = null;
    if (on) {
      const opts = goldOptions(f), filtering = !!GOLD.filter.trim();
      const total = (GOLD.meta.options[f.id] || []).length;
      let fam = null;
      const list = [];
      opts.forEach((x, k) => {
        if (!filtering && x.value !== '' && (x.family || '') !== fam) {
          fam = x.family || '';
          if (fam) list.push(h('div', {class: 'gf-fam', 'aria-hidden': 'true'}, fam));
        }
        const pressed = lab && (x.value === '' ? lab.status === 'set' && !vals.length : vals.includes(x.value));
        list.push(goldOptRow(f, i, x, k, pressed, filtering));
      });
      body = [h('div', {class: 'gf-count small muted'}, filtering ? `${opts.filter(x => !x.fresh).length} of ${total} match` : `${total} choices · type to filter`),
        h('div', {class: 'gf-list', role: 'group', 'aria-label': f.label + ': the choices'}, list,
          !opts.length ? h('span', {class: 'small muted'}, f.id === 'topic' && !GOLD.meta.options.topic_closed ? 'Type a topic' : 'Nothing matches') : null)];
    } else if (lab && vals.length) {
      // What was chosen, with its description: always visible for the current choice.
      body = h('div', {class: 'gf-chosen'}, vals.map(v => { const m = goldMeta(f.id, v); return m && m.description ? h('div', {class: 'go-d'}, h('b', null, m.label), ' ' + m.description) : null; }));
    }
    return h('div', {class: 'gf' + (on ? ' on' : '') + (lab ? ' ok' : ''), onclick: e => { if (e.target.closest('button')) return; goldField(i); }},
      h('div', {class: 'gf-h'}, h('b', null, f.label), f.many ? h('span', {class: 'small muted'}, 'any number') : null,
        on && GOLD.filter ? h('span', {class: 'gf-filter'}, GOLD.filter) : null,
        h('span', {class: 'gf-state' + (lab && lab.status !== 'set' ? ' soft' : '')}, state || '')),
      body,
      h('div', {class: 'gf-x'},
        h('button', {class: 'gx', 'aria-pressed': String(!!lab && lab.status === 'unsure'), title: 'Not sure (?)', onclick: () => { goldField(i, true); goldMark(f, 'unsure'); }}, '? not sure'),
        h('button', {class: 'gx', 'aria-pressed': String(!!lab && lab.status === 'skip'), title: 'Skip this field (-)', onclick: () => { goldField(i, true); goldMark(f, 'skip'); }}, '– skip'),
        f.many && on ? h('button', {class: 'gx', title: 'Done with this field (Enter)', onclick: () => goldConfirm(f)}, 'Enter ↵') : null));
  });
  const noteLab = it.labels.note;
  GOLD.note = h('input', {class: 'search gold-note', type: 'text', placeholder: 'A note (optional)', value: noteLab ? noteLab.values[0] || '' : '', 'aria-label': 'Note',
    onchange: e => goldSave('note', e.target.value.trim() ? [e.target.value.trim()] : [], 'set'),
    onkeydown: e => {
      if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); goldNext(); }
      else if (e.key === 'Escape' || e.key === 'ArrowUp') { e.preventDefault(); e.target.blur(); goldField(GOLD_FIELDS.length - 1); }
    }});
  const ready = GOLD.field >= GOLD_FIELDS.length;
  const last = it.position % 6 === 0 || it.position === it.total;
  const mixed = it.group ? h('label', {class: 'gold-mixed', title: 'Ticked: your answer is for this message only, not for the whole group'},
    h('input', {type: 'checkbox', checked: !!it.labels.mixed, onchange: e => goldSave('mixed', e.target.checked ? ['mixed'] : [], 'set')}),
    h('span', null, h('b', null, 'Mixed group, don’t apply to all'), h('span', {class: 'small muted'}, ` · unticked, your answer goes to all ${fmt(it.group.messages)} messages`))) : null;
  fill(GOLD.panel,
    it.revealed ? h('div', {class: 'note gold-seen'}, 'You have seen the rules’ answers for this round. A change is kept, and marked as made after that.') : null,
    rows, mixed, GOLD.note,
    h('div', {class: 'gold-nav'},
      h('button', {class: 'btn', disabled: it.position <= 1, onclick: () => goldPrev()}, '‹ Back'),
      h('span', {class: 'sp'}),
      h('button', {class: 'btn primary' + (ready ? ' ready' : ''), disabled: !goldComplete(), onclick: () => goldNext()}, last ? 'Finish the round ›' : 'Next ›')),
    h('div', {class: 'gold-keys small muted'}, '1–9, 0 choose · letters filter (label or description) · Enter next · ? not sure · - skip · ↑↓ fields · ←→ items'));
}
function goldField(i, quiet) {
  if (i === GOLD.field && quiet) return;
  GOLD.field = Math.max(0, Math.min(GOLD_FIELDS.length, i));
  GOLD.filter = '';
  goldPanel();
}
// Save one answer: shown at once, sent in order behind it.
function goldSave(field, values, status) {
  const it = GOLD.item, pos = it.position, set = GOLD.set;
  if ((field === 'note' || field === 'mixed') && !values.length) delete it.labels[field]; else it.labels[field] = {values, status};
  const body = {field, values, status, duration_ms: goldElapsed()};
  GOLD.saving = GOLD.saving.then(() => post(`/api/gold/${set}/items/${pos}/labels`, body))
    .then(res => { if (res && res.progress) { GOLD.meta.progress = res.progress; goldProgress(res.progress); } })
    .catch(e => { flash('Not saved: ' + errText(e)); if (GOLD.item && GOLD.item.position === pos) goldShow(pos, {quiet: true}); });
  const r = it.round_items.find(x => x.position === pos);
  if (r) r.done = goldComplete();
  return GOLD.saving;
}
function goldAdvance() {
  const next = GOLD_FIELDS.findIndex((f, i) => i > GOLD.field && !goldAnswered(f.id));
  const any = GOLD_FIELDS.findIndex(f => !goldAnswered(f.id));
  GOLD.field = next >= 0 ? next : any >= 0 ? any : GOLD_FIELDS.length;
  GOLD.filter = '';
  goldPanel();
  refreshStrip();
}
function refreshStrip() {
  const strip = GOLD.grid && GOLD.grid.querySelector('.gold-round');
  if (strip) fill(strip, goldStrip());
}
function goldChoose(f, value) {
  const lab = GOLD.item.labels[f.id];
  if (!f.many) { goldSave(f.id, [value], 'set'); goldAdvance(); return; }
  if (value === '') { goldSave(f.id, [], 'set'); goldAdvance(); return; }
  const cur = new Set(lab && lab.status !== 'skip' ? lab.values : []);
  if (cur.has(value)) cur.delete(value); else cur.add(value);
  goldSave(f.id, [...cur], lab && lab.status === 'unsure' ? 'unsure' : 'set');
  GOLD.filter = '';
  goldPanel();
  refreshStrip();
}
function goldConfirm(f) {
  const lab = GOLD.item.labels[f.id];
  if (!lab) goldSave(f.id, [], 'set');
  goldAdvance();
}
function goldMark(f, status) {
  const lab = GOLD.item.labels[f.id];
  // Pressed again, "not sure" with a value becomes sure of it; otherwise a choice replaces it.
  if (lab && lab.status === status) { if (status === 'unsure' && (lab.values.length || f.many)) { goldSave(f.id, lab.values, 'set'); goldPanel(); } return; }
  goldSave(f.id, status === 'skip' ? [] : (lab ? lab.values : []), status);
  goldAdvance();
}
async function goldNext() {
  if (!goldComplete()) {
    goldField(GOLD_FIELDS.findIndex(f => !goldAnswered(f.id)));
    flash('Answer every field first; ? marks one not sure, - skips it.');
    return;
  }
  const it = GOLD.item;
  await GOLD.saving;
  if (it.position % 6 === 0 || it.position === it.total) return goldReveal(it.round);
  goldShow(it.position + 1);
}
function goldPrev() {
  if (GOLD.summary) { goldShow(GOLD.summary.last); return; }
  if (GOLD.item && GOLD.item.position > 1) goldShow(GOLD.item.position - 1);
}

// ---- after a round: the owner's answers beside the rules' (never before: the server refuses).
async function goldReveal(round) {
  let res;
  try { res = await post(`/api/gold/${GOLD.set}/rounds/${round}/reveal`, {}); }
  catch (e) { flash(errText(e)); return; }
  res.last = res.items[res.items.length - 1].position;
  GOLD.summary = res;
  GOLD.item = null;
  goldSummary(res);
  try { const meta = await fetchJSON(`/api/gold/${GOLD.set}`); GOLD.meta = meta; goldProgress(meta.progress); } catch (e) {}
}
function goldSummary(r) {
  const p = GOLD.meta.progress, allDone = p.done >= p.total;
  const mmss = s => `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, '0')} s`;
  const ans = (f, lab) => !lab ? '—' : lab.status === 'skip' ? 'skipped' : (lab.status === 'unsure' ? '? ' : '') + (lab.values.length ? lab.values.map(v => goldLabel(f, v)).join(', ') : 'none');
  const agree = Object.entries(r.agreement).filter(([, a]) => a.compared).map(([f, a]) => `${f} ${a.agree} of ${a.compared}`);
  const rule = (x, f) => {
    const v = x.rules[f];
    if (!v) return null;
    return h('span', {class: 'grule ' + (v.agrees === true ? 'yes' : v.agrees === false ? 'no' : ''), title: v.source},
      v.agrees === true ? '✓ ' : v.agrees === false ? '✗ ' : '', goldLabel(f, v.value), h('span', {class: 'small muted'}, ' · ' + v.source));
  };
  const next = allDone ? null : h('button', {class: 'btn primary', onclick: () => goldShow(p.next || r.last + 1)}, 'Next round ›');
  fill(GOLD.grid, h('section', {class: 'card gold-sum'},
    h('div', {class: 'card-h'}, h('div', null, h('h2', null, `Round ${r.round} done`),
      h('p', null, `${r.items.length} items · ${mmss(r.seconds)}` + (r.unsure ? ` · ${r.unsure} not sure` : '') + (r.skipped ? ` · ${r.skipped} skipped` : ''))),
      h('div', {class: 'head-r'}, h('button', {class: 'btn', onclick: () => goldShow(r.items[0].position)}, '‹ Back to this round'), next)),
    allDone ? note(`All ${fmt(p.total)} items are done. The scores: uv run talos enrich gold report`) : null,
    h('p', {class: 'small', style: 'margin:0 0 10px'}, h('b', null, 'Agreement preview: '), agree.length ? `your answers and the rules’ agree on ${agree.join(' · ')}.` : 'no rule gave a value for these items.',
      h('span', {class: 'muted'}, ' Under each answer: the rule’s value (✓ agrees, ✗ differs) and which rule or pre-pass signal gave it.')),
    h('div', {class: 'gsum-t'}, r.items.map(x => h('div', {class: 'gsum-row'},
      h('div', {class: 'gsum-m'}, h('span', {class: 'gsum-n'}, String(x.position)), h('div', {style: 'min-width:0'},
        h('b', {class: 'gsum-s'}, x.subject || (x.medium === 'email' ? '(no subject)' : 'Teams chat')), h('div', {class: 'small muted'}, x.from || ''),
        h('div', {class: 'small muted gsum-why'}, x.reason))),
      h('dl', {class: 'kv gsum-kv'}, GOLD_FIELDS.map(f => [h('dt', {class: 'muted'}, f.label), h('dd', null, ans(f.id, x.labels[f.id]),
        x.rules[f.id] ? h('div', null, rule(x, f.id)) : null)])))))));
  GOLD.root && GOLD.root.focus({preventScroll: true});
}

// ---- the check: Claude's labels, checked (talos.gold check_*). Not blind, by design: the owner checks a
// frozen random sample of the items Claude labelled, one at a time. The message is shown exactly
// as on the blind screen (goldMessage); beside each field, Claude's answer with its label and
// description, and whether Claude was not sure. Space agrees with the field in hand; Enter agrees
// with every field still open and goes on to the next item; a number or letters correct the field
// from the same choices as the blind screen. What they answer is saved as their own label. The
// blind screen never reads these endpoints, and the blind item API never carries Claude's labels.
const CHECK = {set: null, state: null, meta: null, rank: null, item: null, field: 0, filter: '', shownAt: 0, base: 0,
               root: null, top: null, grid: null, panel: null, note: null, tab: null, token: 0, busy: false, saving: Promise.resolve()};
const chkElapsed = () => CHECK.base + Math.round(performance.now() - CHECK.shownAt);
const chkMine = f => CHECK.item && CHECK.item.labels[f];
const chkTheirs = f => CHECK.item && CHECK.item.theirs.labels[f];
const chkWho = () => { const w = (CHECK.state && CHECK.state.labeller) || 'claude'; return w.charAt(0).toUpperCase() + w.slice(1); };
const chkSame = (a, b) => a.length === b.length && a.every(v => b.includes(v));
const chkAgreed = f => { const m = chkMine(f), t = chkTheirs(f); return !!(m && t && m.status === 'set' && chkSame(m.values, t.values)); };
const chkComplete = () => GOLD_FIELDS.every(f => chkMine(f.id));
const chkCanAgree = f => { const t = chkTheirs(f.id); return !!t && (!!f.many || t.values.length > 0); };
const chkAnswer = (f, lab) => !lab ? 'no answer' : lab.status === 'skip' ? 'skipped'
  : (lab.status === 'unsure' ? (lab.values.length ? 'not sure: ' : 'not sure') : '')
    + (lab.values.length ? lab.values.map(v => goldLabel(f, v, CHECK.meta.options)).join(', ') : lab.status === 'unsure' ? '' : 'none');

async function viewGoldCheck() {
  const token = ++CHECK.token;
  const {sets} = await fetchJSON('/api/gold');
  const head = right => header('Check Claude’s labels', 'Not blind: Claude’s answer is beside each field. Agree, or correct it; what you answer is saved as yours', right);
  const back = h('button', {class: 'btn', onclick: () => go('gold')}, '‹ Answer key');
  CHECK.root = null;
  if (!sets.length) return h('div', null, head(back), card('No answer key yet', 'Draw one first', null, h('pre', {class: 'cmds'}, 'uv run talos enrich gold sample')));
  if (!sets.some(x => x.id === CHECK.set)) CHECK.set = sets.some(x => x.id === GOLD.set) ? GOLD.set : sets[0].id;
  let res;
  try { res = await fetchJSON(`/api/gold/${CHECK.set}/check`); }
  catch (e) {
    return h('div', null, head(back), card('Nothing to check yet', 'Import Claude’s labels, then draw the items to check', null,
      h('p', null, 'In a terminal:'),
      h('pre', {class: 'cmds'}, `uv run talos enrich gold import-labels --set ${CHECK.set} --labeller claude FILE.jsonl\nuv run talos enrich gold check-sample --set ${CHECK.set} --n 25`)));
  }
  if (token !== CHECK.token) return h('div');
  Object.assign(CHECK, {state: res.state, meta: {options: res.options}, item: null});
  GOLD_FIELDS = goldFieldsOf(res.state.fields);
  CHECK.top = h('div', {class: 'head-r gold-prog'});
  CHECK.grid = h('div', {class: 'gold-grid'});
  CHECK.root = h('div', {class: 'gold gold-check', tabindex: '-1'}, head(CHECK.top), CHECK.grid);
  chkProgress();
  const want = S.obj && res.state.items.some(x => x.rank === S.obj) ? S.obj : null;
  if (want || res.state.next) chkShow(want || res.state.next, {quiet: true}); else chkSummary();
  return CHECK.root;
}

function chkProgress() {
  if (!CHECK.top) return;
  const st = CHECK.state;
  fill(CHECK.top,
    h('button', {class: 'btn', onclick: () => go('gold')}, '‹ Answer key'),
    h('button', {class: 'btn', title: 'What you kept and what you changed so far', onclick: () => chkSummary()}, 'Summary'),
    h('div', {class: 'gold-count'}, h('b', null, `${fmt(st.checked)} of ${fmt(st.total)}`), ' checked',
      h('span', {class: 'gbar', 'aria-hidden': 'true'}, h('span', {style: `width:${st.total ? 100 * st.checked / st.total : 0}%`}))));
}

async function chkShow(rank, opts = {}) {
  const t = ++CHECK.token;
  CHECK.busy = true;  // keys wait until the item is shown, so none lands on the one before
  let it;
  try { it = await fetchJSON(`/api/gold/${CHECK.set}/check/${rank}`); }
  catch (e) { CHECK.busy = false; fill(CHECK.grid, h('div', {class: 'err'}, errText(e))); return; }
  if (t !== CHECK.token || !CHECK.root) return;
  CHECK.busy = false;
  Object.assign(CHECK, {rank, item: it, filter: '', shownAt: performance.now(), base: it.duration_ms || 0, tab: it.unit === 'window' ? 'ctx' : 'msg'});
  S.obj = rank;
  try { history.replaceState(null, '', '#goldcheck/' + rank); } catch (e) {}
  const first = GOLD_FIELDS.findIndex(f => !it.labels[f.id]);
  CHECK.field = first < 0 ? GOLD_FIELDS.length : first;
  CHECK.panel = h('aside', {class: 'card gold-panel', 'aria-label': 'Claude’s answer and yours'});
  const msg = h('section', {class: 'card gold-msg', 'aria-label': 'The message'});
  fill(CHECK.grid, h('div', {class: 'gold-round'}, chkStrip()), msg, CHECK.panel);
  goldMessage(msg, it, CHECK);
  chkPanel();
  if (!opts.quiet) scrollTo(0, 0);
  const anchor = CHECK.tab === 'ctx' ? msg.querySelector('.bubble.gold-anchor') : null;
  if (anchor && anchor.getBoundingClientRect().bottom > innerHeight) anchor.scrollIntoView({block: 'center'});
  CHECK.root.focus({preventScroll: true});
}

// The sample: one step per item, checked or not; any of them can be opened.
function chkStrip() {
  const st = CHECK.state, it = CHECK.item;
  return [h('span', {class: 'small muted'}, `Item ${it.rank} of ${st.total}`),
    h('div', {class: 'gsteps', role: 'group', 'aria-label': 'Items to check'}, st.items.map(r =>
      h('button', {class: 'gstep' + (r.checked ? ' done' : '') + (r.rank === it.rank ? ' cur' : ''), 'aria-current': r.rank === it.rank ? 'step' : null,
        title: `Item ${r.rank} (answer key item ${r.position})` + (r.checked ? ' · checked' : ''), onclick: () => chkShow(r.rank)}, r.checked ? '✓' : String(r.rank)))),
    h('span', {class: 'small muted'}, `answer key item ${fmt(it.position)}`)];
}

// One row per field: Claude's answer always shown; the field in hand also lists the choices to correct it.
function chkPanel() {
  if (!CHECK.panel || !CHECK.item) return;
  const it = CHECK.item, who = chkWho(), o = CHECK.meta.options;
  const rows = GOLD_FIELDS.map((f, i) => {
    const mine = it.labels[f.id], theirs = it.theirs.labels[f.id], on = i === CHECK.field;
    const tv = theirs ? theirs.values : [];
    const agreed = chkAgreed(f.id);
    const state = !mine ? '' : agreed ? '✓ agreed' : 'yours: ' + chkAnswer(f.id, mine);
    const described = v => { const m = goldMeta(f.id, v, o); return h('div', {class: 'go-d'}, h('b', null, m ? m.label : goldNice(v)), m && m.description ? ' ' + m.description : ''); };
    const theirsBox = h('div', {class: 'gc-theirs' + (agreed ? ' kept' : mine ? ' changed' : '')},
      h('div', {class: 'gc-who'}, who, theirs && theirs.status === 'unsure' ? h('span', {class: 'pill gc-unsure', title: `${who} marked this field not sure`}, 'not sure') : null),
      !theirs ? h('div', {class: 'go-d muted'}, 'no answer')
        : tv.length ? tv.map(described)
        : h('div', {class: 'go-d'}, h('b', null, theirs.status === 'unsure' ? 'no value' : 'none'), f.none && theirs.status === 'set' ? ' ' + f.none : ''));
    let body = null;
    if (on) {
      const opts = goldOptions(f, CHECK), filtering = !!CHECK.filter.trim();
      const total = (o[f.id] || []).length, vals = mine ? mine.values : [];
      let fam = null;
      const list = [];
      opts.forEach((x, k) => {
        if (!filtering && x.value !== '' && (x.family || '') !== fam) {
          fam = x.family || '';
          if (fam) list.push(h('div', {class: 'gf-fam', 'aria-hidden': 'true'}, fam));
        }
        const pressed = mine && (x.value === '' ? mine.status === 'set' && !vals.length : vals.includes(x.value));
        const isTheirs = theirs && (x.value === '' ? theirs.status === 'set' && !tv.length : tv.includes(x.value));
        list.push(goldOptRow(f, i, x, k, pressed, filtering, () => { chkField(i, true); chkChoose(f, x.value); },
          isTheirs ? h('span', {class: 'pill gc-mark'}, who) : null));
      });
      body = [h('div', {class: 'gf-count small muted'}, filtering ? `${opts.filter(x => !x.fresh).length} of ${total} match` : `To correct it: ${total} choices · a number, or type to filter`),
        h('div', {class: 'gf-list', role: 'group', 'aria-label': f.label + ': the choices'}, list,
          !opts.length ? h('span', {class: 'small muted'}, 'Nothing matches') : null)];
    }
    return h('div', {class: 'gf' + (on ? ' on' : '') + (mine ? ' ok' : ''), 'data-field': f.id, onclick: e => { if (e.target.closest('button')) return; chkField(i); }},
      h('div', {class: 'gf-h'}, h('b', null, f.label), f.many ? h('span', {class: 'small muted'}, 'any number') : null,
        on && CHECK.filter ? h('span', {class: 'gf-filter'}, CHECK.filter) : null,
        h('span', {class: 'gf-state' + (mine && !agreed ? ' soft' : '')}, state)),
      theirsBox, body,
      h('div', {class: 'gf-x'},
        h('button', {class: 'gx gc-agree', 'aria-pressed': String(agreed), disabled: !chkCanAgree(f), title: `Agree with ${who} on this field (Space)`,
          onclick: () => { chkField(i, true); chkAgree([f.id], true); }}, '✓ agree  Space'),
        h('button', {class: 'gx', 'aria-pressed': String(!!mine && mine.status === 'unsure'), title: 'Not sure (?)', onclick: () => { chkField(i, true); chkMark(f, 'unsure'); }}, '? not sure'),
        h('button', {class: 'gx', 'aria-pressed': String(!!mine && mine.status === 'skip'), title: 'Skip this field (-)', onclick: () => { chkField(i, true); chkMark(f, 'skip'); }}, '– skip')));
  });
  const noteLab = it.labels.note;
  CHECK.note = h('input', {class: 'search gold-note', type: 'text', placeholder: 'Your note (optional)', value: noteLab ? noteLab.values[0] || '' : '', 'aria-label': 'Your note',
    onchange: e => chkSave('note', e.target.value.trim() ? [e.target.value.trim()] : [], 'set'),
    onkeydown: e => {
      if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); chkNext(); }
      else if (e.key === 'Escape' || e.key === 'ArrowUp') { e.preventDefault(); e.target.blur(); chkField(GOLD_FIELDS.length - 1); }
    }});
  const complete = chkComplete();
  const last = CHECK.state.items.every(r => r.checked || r.rank === it.rank);
  fill(CHECK.panel,
    it.theirs.note ? h('div', {class: 'note gc-note'}, h('span', {class: 'i', 'aria-hidden': 'true'}, 'i'), h('div', null, h('b', null, `${who}’s note: `), it.theirs.note)) : null,
    rows, CHECK.note,
    h('div', {class: 'gold-nav'},
      h('button', {class: 'btn', disabled: it.rank <= 1, onclick: () => chkShow(it.rank - 1)}, '‹ Back'),
      h('span', {class: 'sp'}),
      h('button', {class: 'btn primary ready', onclick: () => chkNext()}, complete ? (last ? 'Summary ›' : 'Next ›') : 'Agree with the rest ↵')),
    h('div', {class: 'gold-keys small muted'}, 'Enter agree with the rest and go on · Space agree with this field · 1–9, 0 or letters correct it · ? not sure · - skip · ↑↓ fields · ←→ items'));
}
function chkField(i, quiet) {
  if (i === CHECK.field && quiet) return;
  CHECK.field = Math.max(0, Math.min(GOLD_FIELDS.length, i));
  CHECK.filter = '';
  chkPanel();
}
// The sample's progress follows the owner's answers at once, before the server has them.
function chkLocal() {
  const it = CHECK.item, r = CHECK.state.items.find(x => x.rank === it.rank);
  if (r) { r.fields = GOLD_FIELDS.filter(f => it.labels[f.id]).length; r.checked = r.fields === GOLD_FIELDS.length; }
  CHECK.state.checked = CHECK.state.items.filter(x => x.checked).length;
  CHECK.state.next = (CHECK.state.items.find(x => !x.checked) || {}).rank || null;
  chkProgress();
  const strip = CHECK.grid && CHECK.grid.querySelector('.gold-round');
  if (strip) fill(strip, chkStrip());
}
function chkQueue(path, body, rank) {
  CHECK.saving = CHECK.saving.then(() => post(path, body))
    .catch(e => { flash('Not saved: ' + errText(e)); if (CHECK.item && CHECK.item.rank === rank) chkShow(rank, {quiet: true}); });
  chkLocal();
  return CHECK.saving;
}
// The owner's own answer for one field (a correction, not sure, skip or their note), through the ordinary label endpoint.
function chkSave(field, values, status) {
  const it = CHECK.item;
  if ((field === 'note' || field === 'mixed') && !values.length) delete it.labels[field]; else it.labels[field] = {values, status};
  return chkQueue(`/api/gold/${CHECK.set}/items/${it.position}/labels`, {field, values, status, duration_ms: chkElapsed()}, it.rank);
}
// Agree: Claude's answer for these fields becomes theirs, sure.
function chkAgree(fields, advance) {
  const it = CHECK.item;
  for (const f of fields) it.labels[f] = {values: [...it.theirs.labels[f].values], status: 'set'};
  const saved = chkQueue(`/api/gold/${CHECK.set}/check/${it.rank}/agree`, {fields, duration_ms: chkElapsed()}, it.rank);
  if (advance) chkAdvance(); else chkPanel();
  return saved;
}
function chkAdvance() {
  const next = GOLD_FIELDS.findIndex((f, i) => i > CHECK.field && !chkMine(f.id));
  const any = GOLD_FIELDS.findIndex(f => !chkMine(f.id));
  CHECK.field = next >= 0 ? next : any >= 0 ? any : GOLD_FIELDS.length;
  CHECK.filter = '';
  chkPanel();
}
function chkChoose(f, value) {
  const mine = chkMine(f.id), theirs = chkTheirs(f.id);
  if (!f.many) { chkSave(f.id, [value], 'set'); chkAdvance(); return; }
  if (value === '') { chkSave(f.id, [], 'set'); chkAdvance(); return; }
  // Correcting ask or route starts from Claude's set (or the owner's own, once they have one): a key adds or removes one value.
  const cur = new Set(mine ? (mine.status !== 'skip' ? mine.values : []) : theirs ? theirs.values : []);
  if (cur.has(value)) cur.delete(value); else cur.add(value);
  chkSave(f.id, [...cur], mine && mine.status === 'unsure' ? 'unsure' : 'set');
  CHECK.filter = '';
  chkPanel();
}
function chkMark(f, status) {
  const mine = chkMine(f.id), theirs = chkTheirs(f.id);
  if (mine && mine.status === status) { if (status === 'unsure' && (mine.values.length || f.many)) { chkSave(f.id, mine.values, 'set'); chkPanel(); } return; }
  chkSave(f.id, status === 'skip' ? [] : (mine ? mine.values : theirs ? theirs.values : []), status);
  chkAdvance();
}
// Enter: agree with every field still open, then the next item not yet checked (or the summary).
async function chkNext() {
  const it = CHECK.item;
  const open = GOLD_FIELDS.filter(f => !chkMine(f.id));
  const blocked = open.find(f => !chkCanAgree(f));
  if (blocked) {
    chkField(GOLD_FIELDS.indexOf(blocked));
    flash(`${chkWho()} gave no value for ${blocked.label.toLowerCase()}: choose one, or ? not sure, - skip`);
    return;
  }
  if (open.length) chkAgree(open.map(f => f.id), false);
  CHECK.busy = true;
  await CHECK.saving;
  CHECK.busy = false;
  if (CHECK.item !== it) return;
  const items = CHECK.state.items;
  const after = items.find(r => r.rank > it.rank && !r.checked) || items.find(r => !r.checked);
  if (after) chkShow(after.rank); else chkSummary();
}

// ---- the summary: per field what the owner kept and what they changed (from → to), and the overall rate.
async function chkSummary() {
  const t = ++CHECK.token;
  CHECK.busy = true;
  await CHECK.saving;
  let r;
  try { r = await fetchJSON(`/api/gold/${CHECK.set}/check/summary`); }
  catch (e) { CHECK.busy = false; flash(errText(e)); return; }
  if (t !== CHECK.token || !CHECK.root) return;
  CHECK.busy = false;
  CHECK.item = null;
  S.obj = null;
  try { history.replaceState(null, '', '#goldcheck'); } catch (e) {}
  const who = chkWho(), done = r.checked >= r.total;
  const pct = x => x == null ? '—' : `${Math.round(100 * x)}%`;
  const rows = GOLD_FIELDS.map(f => {
    const d = r.fields[f.id];
    return h('div', {class: 'gc-row'},
      h('b', null, f.label),
      h('span', {class: 'gc-num'}, d.answered ? `${d.agreed} of ${d.answered} kept` : 'not checked yet'),
      h('span', {class: 'gc-pct'}, pct(d.rate)),
      h('div', {class: 'gc-changes'}, d.changed.length ? d.changed.map(c =>
        h('button', {class: 'gc-change', title: `Open item ${c.rank} (answer key item ${c.position})`, onclick: () => chkShow(c.rank)},
          h('span', {class: 'gsum-n'}, String(c.rank)), h('span', {class: 'muted'}, chkAnswer(f.id, c.from)), ' → ', h('b', null, chkAnswer(f.id, c.to))))
        : h('span', {class: 'small muted'}, d.answered ? 'all kept' : '')));
  });
  const b = r.blind;
  fill(CHECK.grid, h('section', {class: 'card gold-sum'},
    h('div', {class: 'card-h'}, h('div', null, h('h2', null, done ? 'Check done' : 'The check so far'),
      h('p', null, `${r.checked} of ${r.total} items checked · you kept ${r.overall.agreed} of ${r.overall.answered} of ${who}’s answers (${pct(r.overall.rate)})`)),
      h('div', {class: 'head-r'}, done ? null : h('button', {class: 'btn primary', onclick: () => chkShow(CHECK.state.next || 1)}, 'Go on checking ›'))),
    h('div', {class: 'gc-t'}, rows),
    b && b.items ? h('p', {class: 'small', style: 'margin:12px 0 0'}, h('b', null, 'Counted apart: '),
      `${b.items === 1 ? 'the item' : `the ${b.items} items`} you labelled blind (${b.positions.join(', ')}): ${who} agrees with you on ${b.overall.exact} of ${b.overall.compared} fields answered for sure.`) : null,
    note('The full comparison, with not sure and skipped counted apart: uv run talos enrich gold report')));
  CHECK.root.focus({preventScroll: true});
}

document.addEventListener('keydown', e => {
  if (S.view !== 'goldcheck' || !CHECK.root || !CHECK.root.isConnected || PANE.open || menuEl || e.defaultPrevented) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
  const k = e.key;
  const act = fn => { e.preventDefault(); fn(); };
  if (CHECK.busy) { if (k.length === 1 || k === 'Enter') e.preventDefault(); return; }
  if (!CHECK.item) {
    if (k === 'Enter' && CHECK.state && CHECK.state.next) act(() => chkShow(CHECK.state.next));
    return;
  }
  const f = GOLD_FIELDS[CHECK.field], it = CHECK.item;
  if (k === 'ArrowDown') return act(() => { if (CHECK.field >= GOLD_FIELDS.length - 1) { CHECK.field = GOLD_FIELDS.length; chkPanel(); CHECK.note.focus(); } else chkField(CHECK.field + 1); });
  if (k === 'ArrowUp') return act(() => chkField(Math.min(CHECK.field, GOLD_FIELDS.length) - 1));
  if (k === 'ArrowLeft') return act(() => { if (it.rank > 1) chkShow(it.rank - 1); });
  if (k === 'ArrowRight') return act(() => { if (it.rank < CHECK.state.total) chkShow(it.rank + 1); else chkSummary(); });
  if (k === 'Escape' && CHECK.filter) return act(() => { CHECK.filter = ''; chkPanel(); });
  if (k === 'Enter') return act(() => {
    if (f && CHECK.filter) { const x = goldOptions(f, CHECK)[0]; if (x) chkChoose(f, x.value); return; }
    chkNext();
  });
  if (!f) return;
  if (k === ' ' && !CHECK.filter) return act(() => { if (chkCanAgree(f)) chkAgree([f.id], true); else flash(`${chkWho()} gave no value here: choose one`); });
  if (/^[0-9]$/.test(k)) return act(() => { const x = goldOptions(f, CHECK)[(Number(k) + 9) % 10]; if (x) chkChoose(f, x.value); });
  if (k === '?') return act(() => chkMark(f, 'unsure'));
  if (k === '-') return act(() => chkMark(f, 'skip'));
  if (k === 'Backspace') return act(() => { if (CHECK.filter) { CHECK.filter = CHECK.filter.slice(0, -1); chkPanel(); } });
  if (k.length === 1 && (/\p{L}/u.test(k) || (CHECK.filter && /[\s.&/'’-]/.test(k)))) return act(() => { CHECK.filter += k; chkPanel(); });
});

document.addEventListener('keydown', e => {
  if (S.view !== 'gold' || !GOLD.root || !GOLD.root.isConnected || PANE.open || menuEl || e.defaultPrevented) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
  const k = e.key;
  if (GOLD.summary) {
    if (k === 'Enter') { e.preventDefault(); const p = GOLD.meta.progress; if (p.done < p.total) goldShow(p.next || GOLD.summary.last + 1); }
    else if (k === 'ArrowLeft') { e.preventDefault(); goldPrev(); }
    return;
  }
  if (!GOLD.item) return;
  const f = GOLD_FIELDS[GOLD.field];
  const act = fn => { e.preventDefault(); fn(); };
  if (k === 'ArrowDown') return act(() => { if (GOLD.field >= GOLD_FIELDS.length - 1) { GOLD.field = GOLD_FIELDS.length; goldPanel(); GOLD.note.focus(); } else goldField(GOLD.field + 1); });
  if (k === 'ArrowUp') return act(() => goldField(Math.min(GOLD.field, GOLD_FIELDS.length) - 1));
  if (k === 'ArrowLeft') return act(goldPrev);
  if (k === 'ArrowRight') return act(goldNext);
  if (k === 'Escape' && GOLD.filter) return act(() => { GOLD.filter = ''; goldPanel(); });
  if (!f) { if (k === 'Enter') act(goldNext); return; }
  if (/^[0-9]$/.test(k)) return act(() => { const x = goldOptions(f)[(Number(k) + 9) % 10]; if (x) goldChoose(f, x.value); });
  if (k === '?') return act(() => goldMark(f, 'unsure'));
  if (k === '-') return act(() => goldMark(f, 'skip'));
  if (k === 'Backspace') return act(() => { if (GOLD.filter) { GOLD.filter = GOLD.filter.slice(0, -1); goldPanel(); } });
  if (k === 'Enter') return act(() => {
    if (GOLD.filter) { const x = goldOptions(f)[0]; if (x) goldChoose(f, x.value); return; }
    if (f.many) return goldConfirm(f);
    if (goldAnswered(f.id)) goldAdvance(); else if (goldComplete()) goldNext();
  });
  if (k.length === 1 && (/\p{L}/u.test(k) || (GOLD.filter && /[\s.&/'’-]/.test(k)))) return act(() => { GOLD.filter += k; goldPanel(); });
});

// ---------------------------------------------------------------- Argus, the service monitor
// Services on this Mac check in (or are probed); /argus/status says how each is doing, and how the
// outbound heartbeat to the hosted dead-man's switch is doing. Status colours are the good /
// warning / serious / critical inks, apart from the account colours and the object-kind colours.
const ARGUS_WORD = {up: 'up', late: 'late', down: 'down', failing: 'failing', paused: 'paused', unknown: 'unknown'};
const ARGUS_TIP = {up: 'Checked in, and its next check-in is not due yet', late: 'Its deadline plus grace has passed',
  down: 'Its deadline plus twice the grace has passed', failing: 'Its last check-in was a failure',
  paused: 'Silenced on purpose: never announced', unknown: 'Registered, never checked in'};
const argusPill = st => h('span', {class: 'pill ast ast-' + st, title: ARGUS_TIP[st] || st}, h('span', {class: 'ast-dot', 'aria-hidden': 'true'}), ARGUS_WORD[st] || st);
const argusAgo = iso => {
  if (!iso) return 'never';
  const s = (Date.now() - new Date(iso)) / 1000, f = Math.abs(s);
  const v = f < 90 ? `${Math.round(f)} s` : f < 5400 ? `${Math.round(f / 60)} min` : f < 172800 ? `${Math.round(f / 3600)} h` : `${Math.round(f / 86400)} d`;
  return s >= 0 ? `${v} ago` : `in ${v}`;
};
const argusClock = iso => iso ? new Date(iso).toLocaleString('sv-SE', {dateStyle: 'short', timeStyle: 'short'}) : '—';
function argusProbe(p) {
  if (!p) return 'push: the service checks in';
  const every = `every ${p.every} s`;
  if (p.type === 'http') return `GET ${p.url} · timeout ${p.timeout} s · ${every}`;
  if (p.type === 'launchd') return `launchd ${p.label} · ok exit ${p.ok_status.join(', ')}${p.require_running ? ' · must run' : ''} · ${every}`;
  if (p.type === 'disk') return `free space on ${p.path} ≥ ${p.min_free_gb} GB · ${every}`;
  if (p.type === 'file_growth') return `size of ${p.path} must not grow · ${every}`;
  return JSON.stringify(p);
}
function argusHistory(days) {
  const W = 14 * 9, H = 16;
  const g = s('svg', {viewBox: `0 0 ${W} ${H}`, class: 'ahist', role: 'img', 'aria-label': 'The last 14 days'});
  days.forEach((d, i) => {
    const fill = d.fail ? 'var(--serious)' : d.ok ? 'var(--good)' : 'var(--grid)';
    g.append(s('rect', {x: i * 9, y: 0, width: 7, height: H, rx: 1.5, style: `fill:${fill}`},
      s('title', null, `${d.day}: ${d.ok} ok, ${d.fail} failed` + (d.ok || d.fail ? '' : ' (no check-ins)'))));
  });
  return g;
}
async function argusAct(slug, action, msg) {
  try { await post(`/api/argus/${encodeURIComponent(slug)}/${action}`, {}); render(); }
  catch (e) { if (msg) msg.replaceChildren(errText(e)); }
}
function argusBeatCard(b, mon) {
  const pillCls = {ok: 'ok', failing: 'hi', 'not set up': 'md', 'not sent yet': ''}[b.state] || '';
  const kpi = (l, v, n, bad) => h('div', {class: 'kpi' + (bad ? ' bad' : '')}, h('div', {class: 'l'}, l), h('div', {class: 'v'}, v), h('div', {class: 'n'}, n));
  return h('section', {class: 'card abeat' + (b.state === 'failing' ? ' bad' : b.state === 'ok' ? '' : ' warn')},
    h('div', {class: 'card-h'}, h('div', null, h('h2', null, 'Outbound heartbeat'),
      h('p', null, `The dead-man's switch on Argus itself: a GET with no payload every ${Math.round(mon.beat_every / 60)} minutes to a hosted check, which alerts you by email when the beats stop`)),
      h('span', {class: 'pill ' + pillCls}, b.state)),
    b.configured ? h('div', {class: 'kpis', style: 'margin-top:0'},
      kpi('Last success', b.last_ok_at ? argusAgo(b.last_ok_at) : 'never', argusClock(b.last_ok_at)),
      kpi('Failures in a row', fmt(b.failures), b.last_error ? `last: ${b.last_error}` : 'none', b.failures > 0),
      kpi('Last failure', b.last_fail_at ? argusAgo(b.last_fail_at) : 'never', argusClock(b.last_fail_at)),
      kpi('Beats sent', fmt(b.total_ok), `${fmt(b.total_failures)} failed in all`)) :
      note(['Not set up: nothing watches Argus from outside yet. Create a check (healthchecks.io: period 5 minutes, grace 10), then store its ping URL in the Keychain: ',
            h('code', null, 'security add-generic-password -s talos -a argus-heartbeat-url -w')]),
    mon.timers ? null : h('div', {style: 'margin-top:10px'}, note(['The timers are off in this server: no probes, no sweep, no heartbeat. Check-ins are still recorded. Turn them on with ',
      h('code', null, '{"enabled": true}'), ' in TALOS_HOME/argus.json and restart talos serve.'])));
}
function argusRow(v) {
  const msg = h('span', {class: 'err small', role: 'alert'});
  const said = v.last_summary ? h('div', {class: v.status === 'failing' ? 'afail' : ''}, v.last_summary) : h('div', {class: 'muted'}, '—');
  const last = v.recent_failures.length && v.status !== 'failing' ? v.recent_failures[0] : null;
  return h('tr', {class: v.paused ? 'off' : null},
    h('td', null, argusPill(v.status), v.status_since && v.status !== 'unknown' ? h('div', {class: 'small muted', style: 'margin-top:3px'}, `for ${argusAgo(v.status_since).replace(' ago', '')}`) : null),
    h('td', {class: 'nm'}, v.name, h('small', null, `${v.slug} · ${v.kind === 'push' ? 'checks in' : 'probed'}`),
      v.probe ? h('small', {class: 'aspec', title: JSON.stringify(v.probe)}, argusProbe(v.probe)) : null),
    h('td', {class: 'small atime', title: v.late_at ? `late at ${argusClock(v.late_at)}, down at ${argusClock(v.down_at)} (grace ${v.grace_seconds} s)` : `grace ${v.grace_seconds} s`},
      h('div', null, h('span', {class: 'muted'}, 'last '), h('span', {title: argusClock(v.last_checkin_at)}, argusAgo(v.last_checkin_at))),
      h('div', null, h('span', {class: 'muted'}, v.expected_next_at && new Date(v.expected_next_at) < Date.now() ? 'was due ' : 'next '),
        v.expected_next_at ? h('span', {title: argusClock(v.expected_next_at)}, argusAgo(v.expected_next_at)) : '—')),
    h('td', {class: 'asum small'}, said,
      last ? h('div', {class: 'muted', title: v.recent_failures.map(f => `${argusClock(f.at)}: ${f.summary || ''}`).join('\n')}, `last failure ${argusAgo(last.at)}: ${last.summary || ''}`) : null,
      argusHistory(v.history)),
    h('td', {class: 'act'},
      v.probe && v.probe.type === 'file_growth' && v.status === 'failing' ? h('button', {class: 'btn sm', title: 'Accept the file’s current size as normal', onclick: () => argusAct(v.slug, 'ack', msg)}, 'Acknowledge') : null,
      ' ', h('button', {class: 'btn sm', onclick: () => argusAct(v.slug, v.paused ? 'resume' : 'pause', msg)}, v.paused ? 'Resume' : 'Pause'), msg));
}
async function viewArgus() {
  const st = await api('/argus/status');
  const bad = st.services.filter(v => ['late', 'down', 'failing'].includes(v.status));
  const c = st.counts;
  const sub = !st.services.length ? 'Nothing registered yet' : bad.length ? `${bad.length} of ${st.services.length} services need a look` :
    `${c.up} of ${st.services.length} services up` + (c.paused ? `, ${c.paused} paused` : '') + (c.unknown ? `, ${c.unknown} not heard from yet` : '');
  return h('div', null,
    header(withHelp('Argus', 'argus'), sub, h('button', {class: 'btn sm', onclick: () => { CACHE.clear(); render(); }}, 'Refresh')),
    h('p', {class: 'lead'}, 'The services on this Mac. Each one checks in and says when it will be back, or Argus probes it. Late, down and failing are announced as macOS notifications, and so is the recovery.'),
    h('div', {style: 'margin-top:16px'}, argusBeatCard(st.beat, st.monitor)),
    h('section', {class: 'card', style: 'margin-top:16px'},
      h('div', {class: 'card-h'}, h('div', null, h('h2', null, 'Services'),
        h('p', null, st.monitor.last_tick_at ? `Swept ${argusAgo(st.monitor.last_tick_at)}; every ${st.monitor.sweep_every} s` : 'Status as of now; the sweep has not run in this server')),
        h('div', {class: 'legend', style: 'margin:0'}, ['up', 'late', 'down', 'failing', 'paused', 'unknown'].map(argusPill))),
      st.services.length ? h('div', {class: 'tscroll'}, h('table', {class: 't atable'},
        h('thead', null, h('tr', null, h('th', null, 'Status'), h('th', null, 'Service'), h('th', null, 'Check-ins'),
          h('th', null, 'Last summary · 14 days'), h('th', {class: 'r'}, ''))),
        h('tbody', null, st.services.map(argusRow)))) :
        empty('No services yet', 'Register the day-one set with  talos argus register')),
    h('p', {class: 'small muted', style: 'margin-top:12px'}, 'A script checks in with the lines  talos argus token SLUG  prints; the contract is in docs/argus.md.'));
}
WIDGET_DATA.argus = () => api('/argus/status');
WIDGET_VIEW.argus = st => {
  const bad = st.services.filter(v => ['late', 'down', 'failing'].includes(v.status));
  const quiet = st.services.filter(v => ['paused', 'unknown'].includes(v.status));
  const b = st.beat;
  return card(withHelp('Argus', 'argus'), 'The services on this Mac', h('button', {class: 'btn sm', onclick: () => go('argus')}, 'Argus ›'),
    !st.services.length ? empty('Nothing watched yet', 'Register the day-one services with  talos argus register') :
    bad.length ? h('div', {class: 'wlinks'}, bad.map(v => h('button', {class: 'wlink', onclick: () => go('argus')}, argusPill(v.status),
      h('span', {class: 'aw-name'}, v.name), h('span', {class: 'small muted aw-sum'}, v.last_summary || argusAgo(v.last_checkin_at))))) :
    h('div', {class: 'aw-ok'}, argusPill('up'), h('span', null, `All ${st.services.length - quiet.length} services up`),
      quiet.length ? h('span', {class: 'small muted'}, `· ${quiet.map(v => `${v.name} ${v.status}`).join(', ')}`) : null),
    h('div', {class: 'small muted', style: 'margin-top:10px'}, 'Outbound heartbeat: ',
      h('span', {class: b.state === 'ok' ? 'aw-good' : 'aw-bad'}, b.state === 'ok' ? `ok, last ${argusAgo(b.last_ok_at)}` :
        b.state === 'failing' ? `failing ${b.failures}× in a row` : b.state)));
};
// The Argus page follows the clock while it is open.
setInterval(() => { if (S.view === 'argus' && !document.hidden) render(true); }, 30000);

const RENDER = {studio: viewStudio, teams: viewTeams, today: viewToday, jobs: viewJobs, releases: viewReleases, space: viewSpace, discover: viewDiscover, work: viewWork, calendar: viewCalendar, timeline: viewTimeline, overview: viewOverview, messages: viewMessages, objects: viewObjects, events: viewEvents, rules: viewRules, gold: viewGold, goldcheck: viewGoldCheck, accept: viewAccept, changesets: viewChangesets, structure: viewStructure, sources: viewSources, argus: viewArgus, clusters: viewClusters, discovery: viewDiscovery};

// A redraw in place (keepFocus: a refresh behind the page, a filter, a tick) keeps the scroll
// position, so the reader is never thrown back to the top; going to a page starts at its top.
async function render(keepFocus) {
  renderRail();
  const where = S.view + '|' + (S.obj || ''), y = keepFocus ? window.scrollY : 0;
  const focused = keepFocus && document.activeElement && document.activeElement.id;
  const caret = focused ? document.activeElement.selectionStart : null;
  // A focused row (a message, a card) keeps the focus through the redraw, found by its key.
  const rowKey = !focused && $main.contains(document.activeElement) && document.activeElement.dataset ? document.activeElement.dataset.paneKey : null;
  beginLearning(S.view);
  try {
    const view = await RENDER[S.view]();
    $main.replaceChildren(h('div', {class: 'view view-' + S.view}, hubTabs(), view));
  } catch (e) {
    $main.replaceChildren(h('div', {class: 'view'}, header('Something went wrong', ''), h('p', {class: 'err'}, String(e.message || e)),
      note('Is the database running? brew services list shows postgresql@18; it listens on port 5433.')));
  }
  if (y && where === S.view + '|' + (S.obj || '')) window.scrollTo(0, y);
  if (focused) { const el = document.getElementById(focused); if (el) { el.focus({preventScroll: true}); if (caret != null && el.setSelectionRange) el.setSelectionRange(caret, caret); } }
  else if (rowKey) { const el = byPaneKey(rowKey); if (el) el.focus({preventScroll: true}); }
  paneMark();
}
applyLook();
await loadOwner();
render();
refreshBadges();
setTimeout(warmUp, 2500);  // the main places, fetched ahead once the first page is in
