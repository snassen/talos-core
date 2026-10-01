-- Enrichment step B: the answer key (gold set), and a faster origin lookup for Messages.
--
-- Written for a populated database: three new tables and one partial index. The tables are
-- filled by `talos enrich gold sample` and the Answer key screen, not here.

-- ---------------------------------------------------------------- the answer key

-- A frozen, stratified sample of items that the owner labels blind (docs/enrichment-plan.md §7).
-- A set is written once by the sampler and never changed; a new sample is a new set.
create table gold_set (
    id          serial primary key,
    name        text not null,
    seed        integer not null,
    target      integer not null,                 -- how many items were asked for
    params      jsonb not null default '{}',      -- sampler version and the strata's targets
    created_at  timestamptz not null default now()
);

-- One item: anchored on one message, with the context it is judged in. unit says which:
-- 'pattern' (the pattern's other samples, frozen in context_ids), 'thread' (the conversation),
-- 'window' (a Teams window, window_first_id .. window_last_id in its chat) or 'message' (alone).
-- stratum and reason say why it was picked; they, and info, are never shown while labelling.
create table gold_item (
    id               serial primary key,
    set_id           integer not null references gold_set (id) on delete cascade,
    position         integer not null,           -- the labelling order, shuffled by the seed
    message_id       bigint not null references message (id) on delete cascade,
    stratum          text not null check (stratum in ('machine', 'person', 'teams', 'boundary')),
    reason           text not null,
    unit             text not null check (unit in ('pattern', 'thread', 'window', 'message')),
    thread_id        bigint,
    pattern_key      text,
    context_ids      bigint[] not null default '{}',
    window_first_id  bigint,
    window_last_id   bigint,
    info             jsonb not null default '{}', -- sampler facts: sizes, the origin and signal when sampled
    revealed_at      timestamptz,                 -- when its round's agreement preview was first shown
    unique (set_id, position),
    unique (set_id, message_id)
);
create index gold_item_message_idx on gold_item (message_id);

-- The owner's answers, one row per item and field, kept apart from assignments so evaluation can
-- compare rules and models against them. status: 'set' (values hold the answer; an empty list
-- is "none" for ask and route), 'unsure' (values may hold a guess) or 'skip'. The note is the
-- field 'note'. duration_ms is the time spent on the item when this was saved; after_reveal
-- marks an answer changed after its round's agreement preview was shown.
create table gold_label (
    set_id        integer not null references gold_set (id) on delete cascade,
    item_id       integer not null references gold_item (id) on delete cascade,
    field         text not null check (field in ('origin', 'type', 'topic', 'ask', 'value', 'route', 'note')),
    values        text[] not null default '{}',
    status        text not null default 'set' check (status in ('set', 'unsure', 'skip')),
    labelled_at   timestamptz not null default now(),
    duration_ms   integer,
    after_reveal  boolean not null default false,
    primary key (item_id, field)
);
create index gold_label_set_idx on gold_label (set_id, labelled_at);

-- ---------------------------------------------------------------- origin for Messages

-- People / Automated in Messages now read each message's effective origin. Over the whole
-- archive that is one lookup per message; this index answers it from the index alone (the
-- ranking needs value, source, time and id), and serves the Origin facet the same way.
create index assignment_origin_idx on assignment (entity_id) include (value, source_kind, created_at, id)
    where dimension_id = 'origin' and status = 'active';
