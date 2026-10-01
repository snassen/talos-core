# The timeline (Work › Timeline)

A project-oriented calendar laid out as a Gantt timeline, so you can see when you have taken on too much. It
includes entries from the ordinary calendar (without the Gantt properties), filters by binder, project and board,
and makes real calendar entries from the Gantt items.

## What it shows (`#timeline`, `talos.timeline`)

- **Rows:** one per binder (its kind's colour), and under it its work items. The binders are the homes of the
  items in view, plus binders with their own dates in the window. A binder folds away with ▸.
- **Bars:**
  - a work item with a **start** (`work_item.start_on`, new) and a **due** date is a bar from start to due;
  - with a due date only it is a **diamond** on that day;
  - with a start only it is a bar that **fades** out to the right (open-ended);
  - with neither it is **undated**: listed under its binder with "Show items without dates", to be given one.
  - A binder's bar is its **own dates** (`object.starts_on`, `ends_on`, set by clicking the binder) or, hatched,
    the span of its items.
  - Colour: an item's bar is its status's colour (the Board's), a focus item has a green outline.
- **The load**, above the binders:
  - **Meetings:** the hours per day taken by the visible calendars' entries shown as busy, tentative, away or
    working elsewhere. Free, cancelled and all-day entries do not count; overlapping entries count once. Blue
    gets darker towards 8 hours; from 6 hours a day it turns red.
  - **Running:** the open items with a start running that day (a column's height), and a red dot for items due.
- **Calendar entries split off** from an item are dots on its row, in their calendar's colour; a dot opens the
  calendar at that day.
- **Zoom:** Weeks (8 weeks, days), Months (26 weeks, week numbers), Year (52 weeks). Today, earlier, later.
- **Filters:** kind, binder (a binder includes the binders nested in it), focus only, show done, show items
  without dates. The choices are remembered in the browser.

## Splitting part of a project off into the calendar

In the work item pane (click a bar or a row), **Calendar › Make a calendar entry** opens a new entry with the
item's title, on its start day at 09:00 when that is ahead, otherwise the next half hour, in the default Talos
calendar. It is an ordinary calendar entry (`calendar_entry.work_item_id` links it back): it shows in the Calendar,
is edited there, and is listed in the item's pane. Alerts are not built yet (docs/calendar.md, "Not yet").

## API

`GET /api/timeline?start=&end=&kind=&binder=&done=1&focus=1` → `binders` (each with `span_start`, `span_end`,
`derived`, `items`, `undated`, `outside`: items of it outside the window), `load` (per day `busy_hours`, `active`,
`due`) and `undated` (a count). Binder dates: `POST /api/objects/{id}` with `starts_on`, `ends_on` (null clears).
A work item's start: `start_on` on `POST /api/work` and `/api/work/{id}`; a start after the due date is refused.
