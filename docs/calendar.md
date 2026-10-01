# The calendar

A normal calendar view, as in most mail clients: a single day, the work week or the full week, a Today button, a
choice of which calendars are visible and which one new entries go to, entries you can edit, and clear colours with a
legend. Nothing fancier. It shows Talos's own calendars beside live copies of your Microsoft 365, iCloud and Google
calendars, and an entry you save in a real calendar is written to it at once (`talos.calwrite`).

## What there is (`#calendar`)

- **Views:** Day, Work week (Monday to Friday) and Week, remembered in the browser. Today, back and forward. A day
  header opens that day. The hours open at 07:00; the current time is a red line on today.
- **Calendars** (table `calendar`), `source` one of:
  - `talos`: your own, kept in Talos only;
  - `m365`: your Microsoft 365 calendars (the work account);
  - `icloud`: your iCloud calendars, including subscribed feeds (read-only);
  - `google`: your Google calendars (the Gmail account), once signed in.
- **Colour and legend:** each calendar has a colour 1–8 (`--series-1` … `--series-8`, the same in light and dark).
  The sidebar lists the calendars per source with a tick (shown or hidden) and is the legend. ⋯ opens a calendar's
  colour (and name, for Talos calendars). With more calendars than colours, two may share one: recolour with ⋯.
- **New entries go to** any calendar Talos can write (Talos's, or a real one that is writable now).
- **Entries** (table `calendar_entry`): created and edited in the pane. The form has Title, Calendar, All day, Start,
  End, Where, Show as, **Alert** (for a real calendar, which fires it on your phone and computer) and Notes.
  All-day entries keep their dates as midnight UTC with the end exclusive; the API shows `start_date`/`end_date`
  with the end inclusive. `work_item_id` links an entry split off from a work item (the Timeline).
- **Live:** the real calendars are copied by the 5-minute sync, by **Refresh**, and by the page itself when the copy
  is more than three minutes old (it asks for a new one behind the page and redraws in place).

## Writing back (`talos.calwrite`)

A save goes to the calendar's server first; what the server answers is stored as the copy, so Talos and the
calendar agree at once. Only the web app imports `calwrite` (a guard test holds this): no sync, rule, job or model
writes to a calendar.

| | Microsoft 365 | iCloud | Google |
|---|---|---|---|
| Create | `POST /me/calendars/{id}/events` | `PUT <uid>.ics` (If-None-Match: *) | `events.insert` |
| Edit | `PATCH /me/events/{id}`, only what changed | read, change the VEVENT, `PUT` with If-Match on its ETag | `events.patch` |
| Remove | `DELETE`: to Outlook's Deleted Items | not offered: iCloud has no trash for one event | `events.delete`: to Google's trash |
| Alert | isReminderOn / reminderMinutesBeforeStart | a VALARM (display) | a popup reminder |

What it will not do, whatever it is asked:

- **Never invite.** No write names an attendee, and every Google write carries `sendUpdates=none`. An entry that has
  invitees other than you is read-only in Talos, since a change would send them an update.
- **Never touch a series.** An occurrence of a repeating event is read-only: change the series in its own app.
- **Someone else's meeting** (Microsoft 365, not the organiser) is read-only: answer it in Outlook.
- **Notes Outlook cut short:** Graph gives Talos only the first 255 characters of an Outlook entry's notes, so when
  they are that long the notes are read-only in Talos and a save leaves them alone.
- **iCloud edits never overwrite a change made meanwhile** on the phone: If-Match on the ETag; a clash says so.
- **Trash is the furthest**, as for mail. iCloud removal waits for your decision (it would be permanent there).

A read-only entry opens as details that say why, with Join (Teams or Meet) and Open in Outlook or Google Calendar.

## Connecting each calendar

- **iCloud** needs nothing new: CalDAV takes the app-specific password already in the Keychain for the iCloud mail
  account (`imap:…`). Subscribed calendars are read from their feeds; a feed that has ended shows nothing and can be
  hidden.
- **Microsoft 365, writing** needs the **Calendars.ReadWrite** permission, which you (or your tenant's admin) grant:
  1. Entra admin center → App registrations → the Talos app → API permissions → Add a permission → Microsoft Graph →
     Delegated → **Calendars.ReadWrite** → Add, then **Grant admin consent for <your organisation>**.
  2. In a terminal on the Mac: `talos auth graph work` (a device code, as before). The sign-in asks for everything
     Talos uses, now including Calendars.ReadWrite (`graphauth.CALENDAR_SCOPES`).
  Until then the Microsoft 365 group says what is missing, and its calendars stay read-only.
- **Google** needs an OAuth client of your own (Google takes no app password for calendars):
  1. console.cloud.google.com → create a project (e.g. "Talos") → APIs & Services → Library → **Google Calendar API**
     → Enable.
  2. OAuth consent screen: External; app name Talos; your Gmail as support and developer contact. Scopes:
     `…/auth/calendar.events` and `…/auth/calendar.calendarlist.readonly`. Add yourself as a test user. Then
     **Publish app** (In production): a Testing app's sign-in expires after seven days. Google shows "unverified
     app" for your own app; that is expected.
  3. Credentials → Create credentials → OAuth client ID → **Desktop app** → Download JSON.
  4. Store it in the Keychain yourself (paste the JSON when asked):
     `security add-generic-password -s talos -a google-oauth-client -w`
  5. On the Mac: `talos auth google`. It opens Google's consent page; the refresh token goes to the Keychain
     (`google-token:gmail`). The calendars come in at the next sync or with Refresh.

## Reading (the sources, read-only)

- `sources/m365calendar.py`: `/me/calendars` and each calendar's `calendarView` (series expanded), GET only.
- `sources/icloudcalendar.py`: CalDAV PROPFIND (principal, calendar home, calendars with colour and write privilege,
  to-do lists left out) and REPORT with `<C:expand>` (the server expands series, times in UTC). A subscribed
  calendar's feed is fetched and expanded with `recurring_ical_events`. The sync guard allows only PROPFIND and
  REPORT through `request()`.
- `sources/googlecalendar.py`: calendarList and events with `singleEvents=true` (series expanded), GET only.
- `calendars.sync_source` upserts calendars (keeping your colour, visible and default choices) and entries for 60
  days back to 400 ahead; an entry no longer there is marked `gone`, a calendar no longer listed `gone` with it. One
  calendar that fails is noted and the others go on. `sync_all` runs every connected account; `syncs()` gives the
  last copy per source.
- It runs after every full `talos sync --then-rules`, on Refresh (`POST /api/calendar/sync`), from the page when
  stale, and with `talos calendar sync`. `talos calendar` lists the calendars.

## Not yet

- **Removing an iCloud entry**: iCloud has no trash for a single event. Talos could keep the event's ICS itself and
  offer Undo; that is your call.
- **Alerts for Talos's own calendars**: they are kept but nothing fires them.
- **Moving an entry between real calendars**: make it anew in the other calendar.

## API

| Route | What |
|---|---|
| `GET /api/calendar?start=YYYY-MM-DD&end=YYYY-MM-DD` | calendars (with `writable`, `write_note`), entries touching those days (with `read_only`, `read_only_reason`, `removable`), `syncs` per source |
| `POST /api/calendar/entries` | create: `calendar_id` (optional: the default), `title`, `start`, `end`, `all_day`, `location`, `body`, `show_as`, `reminder_minutes`, `work_item_id` |
| `POST /api/calendar/entries/{id}` | change any of those, or `{"removed": true}` (Talos entries also `false`) |
| `POST /api/calendar/calendars` | a new Talos calendar: `name`, `color` |
| `POST /api/calendar/calendars/{id}` | `name` (Talos only), `color`, `visible`, `is_default` (a calendar Talos can write) |
| `POST /api/calendar/sync` | copy the real calendars now |

Timed entries go in and out as ISO instants with a zone; all-day ones as dates.
