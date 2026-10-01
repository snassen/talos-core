-- Work: the place the owner acts. Native work items, the binder kinds from Obsidian Talos,
-- and notes. See docs/obsidian-mapping.md: Talos Web becomes the source of truth for work,
-- and the Obsidian vault is lifted over (talos.vault) and then retired.

alter table entity drop constraint entity_kind_check;
alter table entity add constraint entity_kind_check check (kind in
    ('message', 'attachment', 'person', 'org', 'thread', 'object', 'event', 'work_item', 'note'));

-- The binder kinds of Obsidian Talos join the object kinds.
alter table object drop constraint object_kind_check;
alter table object add constraint object_kind_check check (kind in
    ('project', 'personal_project', 'case', 'collection', 'area', 'topic', 'system', 'saved_search'));

-- A binder's own text (purpose, what belongs here, outcome, current context) as Markdown,
-- and where the object came from (for an import: the vault path, uid and content hash).
alter table object add column body text not null default '';
alter table object add column origin jsonb not null default '{}';
create unique index object_origin_uid_idx on object ((origin ->> 'uid')) where origin ? 'uid';

-- A work item: one thing to decide or do. Its status vocabulary is Obsidian Talos's, kept
-- exactly. home is the one binder it belongs to; secondary contexts are 'related' edges.
-- The mail or chat it came from is an 'about' edge to the message (source: human or import).
create table work_item (
    id            bigint primary key references entity (id) on delete cascade,
    title         text not null,
    status        text not null default 'inbox'
                    check (status in ('inbox', 'next', 'doing', 'blocked', 'someday', 'done')),
    home_id       bigint references object (id) on delete set null,
    focus         boolean not null default false,
    due           date,
    review_after  date,
    body          text not null default '',        -- Markdown: desired outcome, context, working notes
    source        jsonb not null default '{}',     -- kind, account, id, link: where it came from
    origin        jsonb not null default '{}',     -- import provenance: vault path, uid, hash, routed-by, receipt
    position      double precision not null default 0,  -- order within a board column
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    done_at       timestamptz
);
create index work_item_status_idx on work_item (status);
create index work_item_home_idx on work_item (home_id, status, position);
create unique index work_item_origin_uid_idx on work_item ((origin ->> 'uid')) where origin ? 'uid';

-- Every change to a work item, so a board move or an edit can be traced and undone.
create table work_item_event (
    id            bigserial primary key,
    work_item_id  bigint not null references work_item (id) on delete cascade,
    at            timestamptz not null default now(),
    field         text not null,
    old_value     jsonb,
    new_value     jsonb,
    by            text not null                    -- the owner (talos.personal.OWNER_ID) | import:vault | an agent's name
);
create index work_item_event_item_idx on work_item_event (work_item_id, at);

-- Notes that belong to an object: a binder's notes, decisions, research, incidents, receipts.
create table note (
    id          bigint primary key references entity (id) on delete cascade,
    object_id   bigint references object (id) on delete cascade,
    kind        text not null default 'note',      -- note | decision | research | incident | receipt | state
    title       text not null,
    body        text not null default '',
    attrs       jsonb not null default '{}',
    origin      jsonb not null default '{}',
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create index note_object_idx on note (object_id, kind);
create unique index note_origin_path_idx on note ((origin ->> 'path')) where origin ? 'path';
