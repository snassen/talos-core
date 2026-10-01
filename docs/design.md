# Talos Web: the design

Decided from a review of every view at desktop and phone width (a design canvas with a theme manager). Themes
live in the browser, and the accounts are told apart by more than colour alone.

The look stays calm: it is a productivity tool, so motion and colour explain and never decorate. What
changed is where things live, how filters take the screen, the phone, and a tighter set of values under a
theme manager.

## Places

Seven places and a room for tuning, instead of 18 rail entries (Teams became the seventh: inside the Mail
page it got messy). Every view keeps its own hash (`#rules`,
`#accept` …), so old links still work; `HUBS` in app.js is the map.

| Place | Tabs | Was |
|---|---|---|
| Today | — | Overview's top half (needs attention, waiting, most important, Argus) and Work space › Needs you |
| Mail | Messages · Conversations (a switch), Drafts in a pane; e-mail only | Messages |
| Teams | Conversations · Messages (a switch); chats by kind and teams › channels in the sidebar | Teams inside Mail |
| Work | Board · List · Timeline | Board, Timeline |
| Calendar | — | Calendar |
| Binders | Areas · All binders | Work space, Objects |
| Discover | Patterns · Events · Insights | Discover, Events, Overview's lower half |
| Tune | Classify (Answer key, Acceptance, Jobs & fruit) · Rules (Rules, Structure, Changesets) · Sources & health (Sources, Clusters, Systems, Argus) | the nine Operations entries |

"Discovery" is shown as **Systems**, so it no longer echoes Discover. Search sits at the top of the rail
(⌘K or Ctrl+K anywhere) and opens Mail with the search field in focus. The places have number keys in the rail's order (1 Today, 2 Mail …), shown beside each place; "[" and "]"
step through a place's tabs, and "/" opens search (`placeKey` in app.js; not in fields, nor in the answer key, its
check and Systems, which use keys of their own). Each place opens on the tab last used
in this browser (`talos-hub-last`).

On a phone (under 820 px) the rail gives way to a tab bar: Today · Mail · Work · Calendar · More (Teams is in More). Mail's
filters collapse into one Filters button that opens a sheet; the board shows one column at a time, picked
from a row of chips; the sender sidebar starts closed.

## Tokens

Everything in style.css is a token; nothing names a raw value (the mail frame's white is the exception: mail
is written for a white page).

- **Type, seven sizes:** `--fs-caps` 11, `--fs-meta` 12, `--fs-ui` 13, `--fs-strong` 14, `--fs-read` 15,
  `--fs-title` 20, `--fs-figure` 28 (was 23 sizes). Text size in Appearance adds 1 or 2 px to all of them.
- **Shape, three radii:** `--r-sm` 6 (buttons, inputs, rows), `--r-md` 10 (cards, columns, menus, the pane),
  `--r-full` (pills, chips). Corners in Appearance scales them together (Square 3/4, Round 9/16).
- **Tint:** an object takes a faint wash of the colour it relates to: `color-mix()` of that colour at `--tint`
  (6 % light, 9 % dark; `--tint-strong` for chips and headers) into its surface, so in the dark it lifts the
  surface towards the colour. Appearance › Colour: Off, Subtle, More.
- **Rhythm:** a list row's vertical padding is `--row-y` (9 px, 5 px when compact).
- **Colour roles, one meaning each:** surfaces and text; the accent for doing (buttons, links, selection,
  focus), never for the brand; status inks, red only for overdue, down or failed; accounts as fills; binder
  kinds as a small square beside a neutral name. Muted text passes AA (4.9:1; it was 3.4:1).

## Accounts

A row carries its account as a **letter in the account's colour**, for example W Work, G Gmail, i iCloud,
C Club, T Teams (`acctMark`; each account's `ui.letter` in accounts.json). The letter tells them apart without relying on colour; the colour
makes them quick to scan. Orange, green and amber carry dark ink, blue and violet white, so every letter
is readable. The account chips above the list show the same letters, so they are also the legend.
Appearance › Accounts in lists can turn the letter into a thin bar, or a bar and a tinted row (the old look).

## The mark

A beacon, the lighthouse Talos is often shown beside (chosen from three rounds of candidates on
the design canvas; faces did not survive icon size). It is drawn in the theme's colours: the tower in the text
colour, the lamp and beams in the beacon's colour, which says how things stand (`beaconLevel` in app.js): the
accent when calm; amber for important mail in the last 24 hours, a review due or a late service; red for overdue
work or a service down or failing. Its tooltip says why. On hover the beams sweep (none with reduced motion). The
browser tab's icon is the same beacon on a dark tile, always with a blue lamp: only the mark in the rail changes colour.

## The manual

A small ⓘ after a title opens the part of the manual that describes it (docs/manual.md, drawn from the field
guide and kept true to the pages). It opens over the page in a `<dialog>`: the page stays
where it was, dimmed behind it, and a click outside, × or Esc closes it (Esc closes only the manual, never the
reading pane behind it). See also moves between sections inside the dialog, with Back. This is the one place a
modal is right: it is reading, not work, and it goes away the moment you look back at the page. Forms still open
in the pane.

The ⓘ goes only where the manual has something to say, on the place's title or a card's title
(`withHelp(title, id)`, or `helpBtn(id)` beside a control). tests/test_manual.py fails when an ⓘ names a section
the manual lacks, or a section has no ⓘ; a new section needs a place to open from.

## Controls

Decided after an interview: controls were too quiet (grey text and thin outlines). Every action
now has a line symbol before its word, and related actions sit in an outlined group with a small title (`agroup`).
There are three kinds of button (`abtn`): **main**, filled, one to three per pane (Reply in red, Reply all in
violet, Forward in green: `--act-reply`, `--act-replyall`, `--act-forward`); **act**, a coloured outline with its
symbol, filled while on (Important in red with an exclamation mark, a waiting question in amber with a question
mark, Not important in grey with a line); **quiet**, a grey outline, never bare text. The message pane's actions
have shortcut letters shown on their buttons (`MESSAGE_KEYS`: R reply, A reply all, F forward, W work item, L link,
I important, N not important, C conversation, B binder). They work while a message or conversation is in the pane
and the focus is not in a field, and never while a send or post waits for its confirmation. Nothing waits for the
pointer: card and watcher menus are always in view. Appearance › Controls › Quiet brings back the lighter look.

## Motion

Three speeds, used only where something arrives or leaves: `--dur-1` 90 ms (hover, press), `--dur-2`
160 ms (menus, chips, toasts), `--dur-3` 220 ms (the reading pane, the phone's sheets). The pane slides in
16 px from its edge; menus fade and scale from 0.98; a toast rises 8 px; mail that arrived since the list was
last drawn gets an accent wash that fades over 1.2 s; loading shows grey lines that pulse, not a word. Data
never animates, nothing bounces, nothing loops but the loading pulse. Motion › Reduced keeps fades and drops
slides; Off stops everything; the system's reduced-motion setting always wins.

## Themes (Appearance)

Appearance, at the foot of the rail, opens in the reading pane, so the page behind it is the preview. It
changes the look only. Settings live in this browser (`talos-look` in localStorage); index.html applies them
before the first paint, `applyLook()` after a change.

- **Mode:** System, Light, Dark.
- **Theme:** Talos (warm grey, blue), Slate (cool, indigo), Paper (warm cream, teal), Graphite (black and
  white, stronger borders), Midnight (blue-black), Bronze (the icon's bronze as the accent). Each is a block
  of `light-dark()` pairs in style.css; all pass AA for body text, muted text and button text in both modes.
- **Accent:** the theme's own, or blue, indigo, teal, bronze, plum, graphite.
- **Text size, headings (sans or serif), density, corners, accounts in lists, motion.**

Every setting is a data-* attribute on `<html>` that style.css turns into tokens. Status, account and kind
colours are shared by every preset, so meaning never changes with the theme. The pane shows the contrast of
body text, dates and button text for the current choice.

## Teams

Mail and Teams keep separate filters (`S.fs.mail`, `S.fs.teams`; `S.f` is the one in view), so a selection in one
never leaks into the other. Mail opens on received e-mail (`medium=email`); Teams on every chat and channel, both
directions (`teamsDefaults()`). A Teams row names its kind with a coloured pill: 1:1, Group, Meeting, or
Team › Channel. A found message opens its conversation with it in the middle (`?around=`), marked for a moment
(`.bubble.found`), with Load earlier and Load later. Writing sits under the conversation; the confirmation
names where the post goes in the title size, like the From block of a mail. A background service (`<prefix>.teams`) asks for new chat
messages every 20 seconds; the open page (`teamsLive`) checks every 10 seconds whether anything arrived, and the
header says when Talos last looked.
