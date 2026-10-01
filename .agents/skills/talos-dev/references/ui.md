# Talos Web: the UI

Vanilla JS with no build step: `src/talos/web/static/app.js` (one file, ~5,700 lines), `style.css` and
`index.html`. The server fingerprints the assets. After a restart, a browser needs a **reload** to get new JS
or CSS, so tell the owner to reload.

## Building blocks in app.js

- **`h(tag, attrs, ...kids)`** builds elements; `s()` builds SVG.
  - `attrs.class`, `attrs.style` (a string), and `on<event>` handlers.
  - `false` or `null` attributes are skipped, so `disabled: !x` is safe.
  - **Never `innerHTML`**: a guard test fails on it. Mail is untrusted input.
- `fill(el, ...kids)` replaces children.
- `card(title, sub, right, ...body)`, `header(title, sub, right)`, `seg(options, current, onChange)`,
  `note(text)`, `empty(title, text)`, `emptyPage(...)`.
- `kindBadge(kind, text)` and `kindCls(kind)` give the binder kind colours (area, project, topic, system,
  personal project).
- `acctColor(id)`, `acctName(id)`, `acctDot(id)`: accounts come from the owner's accounts.json through `/api/owner` (`loadOwner()`): their names, letters, colours and order.
- `fmt(n)` (Swedish number grouping), `when(iso)`, `day(iso)`, `flash(text)` (a toast), `errText(e)`.
- **`api(path)`**: GET with a stale-while-revalidate cache (`CACHE`, fresh for 5 s).
  - A changed revalidation redraws the page in place.
  - `post(path, body, keep)` sends the `X-Talos: 1` header (the server refuses POSTs without it) and clears
    the cache unless `keep`.
  - `fetchJSON` goes around the cache.
- **`render(keepFocus)`** redraws the current view.
  - `render(true)` is an in-place redraw: it keeps the focus **and the scroll position**. Use it after
    filters, ticks and background refreshes.
  - `render()` is for navigation.
  - `go(view, id)` navigates and scrolls to the top.
- **Slow server answers** (`insight_cache`) come back with `stale`/`refreshing`. Call
  `pollWhileRefreshing(d, path)` in the view. It polls quietly and redraws once, when the new answer is in.
  Never redraw on every poll: the page used to jump to the top that way.
- **The reading pane**:
  - `openPane(node, {key, title})`, or `paneLoad({key, title}, async () => ({node}))` for content that loads.
  - `closePane(false)` closes it.
  - Rows mark themselves with `data-pane-key`.
  - Forms (new watcher, new binder…) open in the pane, never in a modal.
- **The live manual**: `withHelp(title, id)` puts an ⓘ after a title, `helpBtn(id)` beside anything else; it
  opens the section `## Title {#id}` of `docs/manual.md` in a dialog over the dimmed page (`talos.manual`,
  `/api/manual`). When a change makes a section untrue, fix the section in the same commit; a new place or
  control the manual should explain gets a section and an ⓘ (tests/test_manual.py holds the two together).
- **Views** are `async function viewX()` returning a node, registered in `VIEWS`, `RENDER` and, to be
  reachable, a tab of one of the `HUBS` (the six places and Tune; docs/design.md). A hub of several views
  shows them as tabs above the view (`hubTabs()`), and a tab may carry a mode (`'work:list'`). `ICONS` holds
  the SVG paths. The hash is still `#view` or `#objects/<id>`, so old links keep working.
- **The phone** (under 820 px) has no rail: `renderTabbar()` draws Today · Mail · Work · Calendar · More at
  the bottom. `.only-narrow` shows a control only there (Mail's Filters button, the board's column picker).
- **Messages filters** are `S.f`. `blank()` and `defaults()` build them, and `filterParams(S.f)` turns them
  into the query string.
  - Dimension filters travel as `dim=field:value`.
  - Dimensions without a menu travel in `S.f.dims`.
  - `filtersFromQuery(qs)` / `openQuery(qs)` go the other way: stored selections (watchers, aggregations)
    open in Messages that way.

## Placement matters

`tests/test_gold.py` checks that no code between the `// ---- the answer key` section and
`const RENDER = {` opens messages (the answer key must stay blind). Put new view code **above** the answer-key
section, not just before `RENDER`.

## CSS

The full design system (places, type scale, radii, colour roles, motion, themes) is in `docs/design.md`.
Use the tokens, never raw values, so every theme and both modes work:
- surfaces and text: `--surface-1`, `--surface-2`, `--surface-sunken`, `--border`, `--border-strong`,
  `--text-primary`, `--text-secondary`, `--text-muted`;
- accent and states: `--accent`, `--critical-ink`;
- kind colours: `--kind-area` etc., set as `--kc` by `kindCls`;
- account colours: `--series-*`, set as `--acct` per chip or row. An account in a row or table is
  `acctMark(id)` (its letter in its colour; Appearance can make it a bar or a wash), never a bare dot;
- type: `--fs-caps`, `--fs-meta`, `--fs-ui`, `--fs-strong`, `--fs-read`, `--fs-title`, `--fs-figure` (seven
  sizes; no `font-size` in px), headings in `--font-head`;
- shape and rhythm: `--r-sm`, `--r-md`, `--r-full` (three radii), `--row-y` (a list row's vertical padding,
  which Density changes);
- motion: `--dur-1` (hover), `--dur-2` (menus, chips), `--dur-3` (the pane, sheets) with `--ease-in` /
  `--ease-out`. Motion explains where something came from; nothing loops but a loading pulse.

**Themes** (Appearance, in the rail's foot) are only data-* attributes on `<html>` (`LOOK`, `applyLook()`),
saved in the browser as `talos-look`; `index.html` applies them before the first paint. A preset is a block of
`light-dark()` pairs in style.css. A new token needs a value in the `:root` block, and in a preset only when
that preset should differ. Status, account and kind colours are shared by every preset on purpose.

Layout helpers: `.grid`, `.g-hero` (two columns), `.chips`, `.pill` (with `ok`, `md`, `hi`, `ac`), `.btn` (with
`sm`, `ghost`, `primary`), `.linkbtn`, `.t` (tables), `.fc` (a filter chip; `.on` when set), `.nrow` (a row
with a status dot, as on Today). One dress per job: one primary button per view or pane; a row shows its one
likely action and puts the rest behind ⋯ (`.k-more`); red (`hi`) only for overdue, down or failed. Add a
`@media (max-width: …)` rule, or a container query on `.view`, for anything that must work on a phone.

## Checking a page

Talos Web needs a session (docs/security.md). Never type the owner's password or codes anywhere.

- **In-app browser** (Claude Desktop): navigate to `http://127.0.0.1:7420/#view`. If it shows the sign-in
  page, ask the owner to sign in there. After a restart, reload.
  Read text with the page-text tool, or check behaviour with a small script (click, wait, read `scrollY`).
  Reset any viewport you emulated.
- **Headless**, for any agent: first make a short session into a private file,
  `uv run talos web session --minutes 20 --cookie-file <scratch>/state.json` (it is logged, and the owner gets a
  notification), then load it with `browser.new_context(storage_state="<scratch>/state.json")`. Then run
  `uv run --with playwright python script.py`, with `p.chromium.launch(channel="chrome")`,
  `viewport={"width":1440,"height":900}`, `goto`, `wait_for_timeout(4000)` (the views load async), then
  `screenshot` or `evaluate`. Keep scripts and screenshots in a scratch folder, never in the repo: they show
  real mail.
- **Opening compose creates a draft.** Discard it afterwards: `POST /api/drafts/<id>/discard` with
  `X-Talos: 1`. Never press Send.
