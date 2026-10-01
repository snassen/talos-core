-- The calendar goes live: the owner's iCloud and Google calendars join Talos's own and
-- Microsoft 365's, and entries are written back to the real calendar when the owner saves (talos.calwrite).
-- A new entry may go to any calendar the owner can write to, so the default is no longer only a Talos one.
-- An entry gets an alert (minutes before the start), which the real calendar fires.

alter table calendar drop constraint calendar_source_check;
alter table calendar add constraint calendar_source_check check (source in ('talos', 'm365', 'icloud', 'google'));
alter table calendar drop constraint calendar_check;

alter table calendar_entry add column reminder_minutes int check (reminder_minutes is null or reminder_minutes between 0 and 40320);
