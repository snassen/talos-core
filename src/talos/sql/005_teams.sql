-- Microsoft Teams: chats and channel messages, read-only (talos.sources.teams).
--
-- Written for a populated database: it only widens checks, relaxes two NOT NULLs and adds
-- tables. The two check constraints are widened by reading their current value lists and
-- adding one value, so values another migration may have added are kept.

-- A Teams message's original is its Graph JSON: blob kind 'json' (zstd, <sha>.json.zst).
-- The 'teams' account has its own sync runs, cursors and lock, apart from the work account's mail.
do $$
declare
    t record;
    c record;
    vals text[];
begin
    for t in select * from (values ('blob', 'kind', 'json'), ('account', 'provider', 'teams')) v(tbl, col, extra) loop
        vals := array[]::text[];
        for c in
            select con.conname, pg_get_constraintdef(con.oid) as def
            from pg_constraint con
            where con.conrelid = t.tbl::regclass and con.contype = 'c'
              and pg_get_constraintdef(con.oid) ~ ('\m' || t.col || '\M')
              and pg_get_constraintdef(con.oid) ~* '(= any|in \()'
        loop
            vals := vals || array(select m[1] from regexp_matches(c.def, '''([^'']+)''', 'g') m);
            execute format('alter table %I drop constraint %I', t.tbl, c.conname);
        end loop;
        vals := array(select distinct v from unnest(vals || t.extra) v order by v);
        execute format('alter table %I add constraint %I check (%I = any (%L::text[]))',
                       t.tbl, t.tbl || '_' || t.col || '_check', t.col, vals);
    end loop;
end $$;

-- Files shared in Teams live in SharePoint or OneDrive. They are recorded as links
-- (name, type, url in attrs), not downloaded, so they have no blob and no known size.
alter table attachment alter column blob_sha256 drop not null;
alter table attachment alter column size_bytes drop not null;
alter table attachment add constraint attachment_blob_or_link
    check (blob_sha256 is not null or attrs ? 'url');

-- Every version of a message's original that sync has seen. message.raw_sha256 points
-- at the newest; an edited Teams message keeps its earlier JSON here and in the vault.
create table message_original (
    message_id  bigint not null references message (id) on delete cascade,
    sha256      text not null references blob (sha256),
    seen_at     timestamptz not null default now(),
    primary key (message_id, sha256)
);

-- Microsoft user id → address, learnt from chat members. A Teams message names its sender
-- by user id only; this is how a sender becomes the same person as their e-mail address.
create table teams_user (
    user_id       text primary key,
    address       text,                          -- lower-cased; null when Graph gave none
    display_name  text,
    updated_at    timestamptz not null default now()
);
