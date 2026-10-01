-- Sync robustness. Written to run on a populated database: nothing is dropped, and the
-- existing rows are brought along (folder_path filled in, Graph locations re-keyed).

-- A message whose body never arrived: a UID the server left out of a FETCH, a FETCH entry
-- without a body, or a Graph message whose MIME could not be downloaded. There is no
-- original to keep, so this is not ingest_failure (whose raw_sha256 must name a blob).
-- The adapters retry these on their next run and delete the row once the body arrives
-- or the server no longer has the message.
create table fetch_failure (
    account_id  text not null references account (id) on delete cascade,
    ref         text not null,                    -- Graph message id, or '<folder>:<uidvalidity>:<uid>'
    location    jsonb not null default '{}',
    error       text not null,
    attempts    integer not null default 1,
    first_at    timestamptz not null default now(),
    last_at     timestamptz not null default now(),
    primary key (account_id, ref)
);

-- message_location.folder is the key the server knows the folder by: the Graph folder id
-- (stable across renames), the IMAP folder name, or '[all]' for Gmail. folder_path is the
-- readable path that rules, search and people use ("Inkorgen/Backup").
alter table message_location add column folder_path text;
update message_location set folder_path = folder where folder_path is null;

-- Graph locations were keyed by the readable path until now. The sync cursors (scope
-- 'folder:<id>', state {"path", "folder_id"}) say which id each path belongs to. A path no
-- cursor names (a folder renamed before this migration) is left as it is.
with f as (
    select distinct on (c.account_id, c.state ->> 'path')
           c.account_id, c.state ->> 'path' as path,
           coalesce(c.state ->> 'folder_id', substr(c.scope, 8)) as folder_id
    from sync_cursor c
    join account a on a.id = c.account_id
    where a.provider = 'graph' and left(c.scope, 7) = 'folder:' and c.state ? 'path'
    order by c.account_id, c.state ->> 'path', c.updated_at desc
)
update message_location l
set folder = f.folder_id
from f
where l.account_id = f.account_id and l.folder = f.path and l.folder <> f.folder_id
  and not exists (select 1 from message_location x where x.message_id = l.message_id and x.folder = f.folder_id);

-- mark_gone by provider id (Graph @removed) no longer scans the account's locations.
create index message_location_provider_idx on message_location (account_id, provider_id);
