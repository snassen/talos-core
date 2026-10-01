-- The calendar: a Calendar section with day, work week and week views.
-- Two kinds of calendar: Talos's own (source 'talos'), which the owner creates and edits here, and
-- read-only copies of the owner's Microsoft 365 calendars (source 'm365'), read with the Calendars.Read
-- scope already consented to. Nothing here writes to a mail or calendar server; editing
-- the M365 calendars from Talos would be a new write path and needs the owner's go first.

create table calendar (
    id           bigserial primary key,
    source       text not null check (source in ('talos', 'm365')),
    account_id   text references account (id),        -- the account an m365 calendar is read with
    remote_id    text,                                -- Graph's calendar id
    name         text not null,
    color        int not null default 1 check (color between 1 and 8),  -- --series-1 … --series-8
    visible      boolean not null default true,
    is_default   boolean not null default false,      -- where a new entry goes; always a Talos calendar
    position     double precision not null default 0,
    gone         boolean not null default false,      -- an m365 calendar no longer in the list
    attrs        jsonb not null default '{}',         -- owner, canEdit and the like, as Graph gave them
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    unique (source, remote_id),
    check (not is_default or source = 'talos')
);
create unique index calendar_one_default_idx on calendar (is_default) where is_default;

-- An entry: one appointment. An all-day entry keeps its dates as midnight UTC of each date (the end
-- is exclusive, as in Graph and iCalendar), so it lands on the same days in every time zone.
-- A Talos entry that is removed keeps its row with removed_at set (trash, never a hard delete);
-- an m365 entry that is no longer in its calendar is marked gone. work_item_id links an entry
-- split off from a work item (the Gantt's "make a calendar entry").
create table calendar_entry (
    id            bigserial primary key,
    calendar_id   bigint not null references calendar (id) on delete cascade,
    remote_id     text,
    title         text not null,
    starts_at     timestamptz not null,
    ends_at       timestamptz not null,
    all_day       boolean not null default false,
    location      text not null default '',
    body          text not null default '',
    show_as       text not null default 'busy',       -- free | tentative | busy | oof | workingElsewhere
    attrs         jsonb not null default '{}',        -- organizer, attendees, web link, cancelled …
    work_item_id  bigint references work_item (id) on delete set null,
    gone          boolean not null default false,
    removed_at    timestamptz,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    check (ends_at >= starts_at),
    unique (calendar_id, remote_id)
);
create index calendar_entry_range_idx on calendar_entry (starts_at, ends_at);
create index calendar_entry_work_idx on calendar_entry (work_item_id) where work_item_id is not null;
