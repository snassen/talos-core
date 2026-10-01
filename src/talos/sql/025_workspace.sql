-- The work space and Discover (talos.space, talos.discover; docs/workspace.md).
--
-- Two new tables; nothing existing is changed. A binder's search terms (what "Found in your mail"
-- and its neighbours look for) live in object.attrs.terms, so they need no column.

-- A watcher: a saved Messages selection that keeps looking. It counts what matches, and what
-- arrived since the owner last looked (seen_at); it may belong to a binder, whose page lists it and
-- can take its new mail in (by the owner's hand, never by itself).
--   query     the Messages filters as a query string (q=…&dim=type:invoice&exclude_senders=…),
--             the same the Messages view sends, so opening it shows exactly what it counts
--   made_by   the owner (talos.personal.OWNER_ID) | claude
create table watcher (
    id          bigserial primary key,
    name        text not null check (length(name) between 1 and 200),
    query       text not null,
    object_id   bigint references object(id) on delete set null,
    made_by     text not null default 'owner' check (made_by in ('owner', 'claude')),
    paused      boolean not null default false,
    seen_at     timestamptz not null default now(),
    created_at  timestamptz not null default now()
);
create index watcher_object_idx on watcher (object_id);

-- A saved aggregation: a Messages selection counted in groups (by sender, domain, year, month or
-- account), shown on Discover and kept up to date because it is counted when shown.
create table aggregation (
    id          bigserial primary key,
    name        text not null check (length(name) between 1 and 200),
    description text not null default '',
    query       text not null,
    group_by    text not null check (group_by in ('sender', 'domain', 'year', 'month', 'account')),
    made_by     text not null default 'owner' check (made_by in ('owner', 'claude')),
    created_at  timestamptz not null default now()
);

-- Claude's first aggregations, over the metadata only (no names from the archive).
insert into aggregation (name, description, query, group_by, made_by) values
    ('Vendors who sell to me cold', 'Cold sales outreach, by the sender''s domain', 'dim=type%3Asales_outreach', 'domain', 'claude'),
    ('Invoices by sender', 'Every invoice, by the domain it came from', 'dim=type%3Ainvoice', 'domain', 'claude'),
    ('Everything worth keeping', 'Mail marked worth keeping, per year', 'dim=keep%3Akeep', 'year', 'claude'),
    ('Decisions asked of me', 'Mail that asks you to decide something, per month', 'dim=ask%3Adecision', 'month', 'claude'),
    ('Security events by sender', 'Security events and alerts, by the address that sent them', 'dim=type%3Asecurity_event', 'sender', 'claude'),
    ('Licences and subscriptions', 'Licence, asset and subscription mail, by domain', 'dim=type%3Alicense_asset', 'domain', 'claude');
