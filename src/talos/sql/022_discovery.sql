-- Discovery review (talos.discovery, Operations › Discovery): the drafted systems, candidate
-- binders and the owner's own setup (rules/discovery/*.json), one row per item, with the decision.
--
-- Written for a populated database: one new table, nothing existing changed.
--
-- `talos discovery load` fills and refreshes the drafts (source, item_key, kind, name, payload,
-- status, position) and never touches a decision already made. Accepting makes the binder
-- (object_id); rejecting stores the decision only, unless the owner keeps a retired system as a binder.
-- The table is the source of truth; `talos discovery export` writes the decisions back into the
-- JSON files.
create table discovery_item (
    id           bigserial primary key,
    source       text not null check (source in ('systems', 'candidates', 'my-setup')),
    item_key     text not null,                   -- the draft's own key ('unifi', 'area:network', 'cli:atuin')
    kind         text not null,                   -- proposed: system, area, topic, project, … or application, cli, repo …
    name         text not null,
    payload      jsonb not null,                  -- the draft item as the file has it
    status       text,                            -- the draft's status (systems): active, unclear, legacy, …
    position     integer not null default 0,      -- order in its file
    decision     text check (decision in ('accept', 'reject')),
    correction   jsonb,                           -- the fields the owner changed: name, kind, description, status, note
    decided_at   timestamptz,
    object_id    bigint references object (id) on delete set null,  -- the binder it became or enriched
    loaded_at    timestamptz not null default now(),
    unique (source, item_key),
    check ((decision is null) = (decided_at is null))
);
create index discovery_item_source_idx on discovery_item (source, position);

-- Each file's header as last loaded (generated, status_values, host, method, open_questions …),
-- so the page and the binders can say where a draft came from without reading the file again.
create table discovery_source (
    source     text primary key check (source in ('systems', 'candidates', 'my-setup')),
    path       text not null,
    meta       jsonb not null default '{}',
    loaded_at  timestamptz not null default now()
);
