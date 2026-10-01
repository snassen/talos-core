-- Talos foundation schema. See docs/architecture.md for the layer model.
-- Every addressable thing is an entity, so the edge table can link anything.

create extension if not exists pg_trgm;
create extension if not exists unaccent;
create extension if not exists vector;

-- ---------------------------------------------------------------- accounts

create table account (
    id            text primary key,              -- short handle: 'gmail', 'work'
    provider      text not null check (provider in ('gmail', 'graph', 'imap', 'local')),
    address       text not null,
    display_name  text,
    enabled       boolean not null default true,
    settings      jsonb not null default '{}',   -- host, port, keychain refs; never secrets
    created_at    timestamptz not null default now()
);

-- Every address that is "me", across accounts and aliases. Direction (in/out)
-- is decided against this list, not against the account the mail arrived in.
create table my_address (
    address     text primary key,
    account_id  text references account (id) on delete set null
);

-- ---------------------------------------------------------------- entities

create table entity (
    id          bigserial primary key,
    kind        text not null check (kind in
                  ('message', 'attachment', 'person', 'org', 'thread', 'object', 'event')),
    created_at  timestamptz not null default now()
);
create index entity_kind_idx on entity (kind);

-- ---------------------------------------------------------------- L0 vault index

create table blob (
    sha256       text primary key,                -- over the uncompressed bytes
    kind         text not null check (kind in ('raw', 'attachment')),
    size         bigint not null,
    stored_size  bigint not null,
    codec        text not null check (codec in ('zstd', 'none')),
    path         text not null,                   -- relative to the vault root
    created_at   timestamptz not null default now()
);

-- ---------------------------------------------------------------- people and orgs

create table org (
    id      bigint primary key references entity (id) on delete cascade,
    domain  text not null unique,
    name    text
);

create table person (
    id               bigint primary key references entity (id) on delete cascade,
    display_name     text,
    primary_address  text not null unique,
    is_me            boolean not null default false,
    org_id           bigint references org (id) on delete set null
);

create table address (
    address    text primary key,                  -- lower-cased
    person_id  bigint not null references person (id) on delete cascade,
    domain     text not null
);
create index address_domain_idx on address (domain);

-- ---------------------------------------------------------------- L1 messages

create table thread (
    id                  bigint primary key references entity (id) on delete cascade,
    account_id          text not null references account (id),
    provider_thread_id  text,                     -- X-GM-THRID, conversationId; null for header threading
    subject             text,
    first_at            timestamptz,
    last_at             timestamptz,
    message_count       integer not null default 0,
    unique (account_id, provider_thread_id)
);
create index thread_last_idx on thread (last_at desc);

create table message (
    id              bigint primary key references entity (id) on delete cascade,
    account_id      text not null references account (id),
    medium          text not null default 'email' check (medium in ('email', 'teams_chat', 'teams_channel')),
    provider_key    text not null,                -- stable per account; see architecture.md
    rfc_message_id  text,
    thread_id       bigint references thread (id) on delete set null,
    raw_sha256      text references blob (sha256),
    sent_at         timestamptz,                  -- the Date header
    received_at     timestamptz,                  -- the server's internal date
    direction       text not null check (direction in ('in', 'out', 'self')),
    from_address    text,
    from_name       text,
    subject         text,
    snippet         text,
    size_bytes      integer,
    has_attachments boolean not null default false,
    is_automated    boolean not null default false,
    list_id         text,
    in_reply_to     text,
    references_ids  text[] not null default '{}',
    headers         jsonb not null default '{}',  -- the few headers rules need
    parser_version  integer not null,
    ingested_at     timestamptz not null default now(),
    unique (account_id, provider_key)
);
create index message_account_received_idx on message (account_id, received_at desc);
create index message_received_idx on message (received_at desc);
create index message_rfc_idx on message (rfc_message_id);
create index message_thread_idx on message (thread_id);
create index message_from_idx on message (from_address);
create index message_subject_trgm_idx on message using gin (subject gin_trgm_ops);

-- Bodies live apart from the message row so lists and rules stay small and fast.
create table message_text (
    message_id      bigint primary key references message (id) on delete cascade,
    body_kind       text not null check (body_kind in ('plain', 'html', 'none')),
    body_text       text not null default '',
    quote_stripped  text not null default '',
    search          tsvector not null
);
create index message_text_search_idx on message_text using gin (search);

-- A mirror of what the server says about the message: where it is, flags, labels.
-- Changesets are planned against this.
create table message_location (
    message_id   bigint not null references message (id) on delete cascade,
    account_id   text not null references account (id),
    folder       text not null,                   -- IMAP folder, Graph folder id, or '[all]' for Gmail
    uidvalidity  bigint,
    uid          bigint,
    provider_id  text,                            -- Graph immutable id, Gmail X-GM-MSGID
    flags        text[] not null default '{}',    -- normalised: seen, flagged, answered, draft
    labels       text[] not null default '{}',    -- Gmail labels, Graph categories
    present      boolean not null default true,   -- false once the server no longer has it
    observed_at  timestamptz not null default now(),
    primary key (message_id, folder)
);
create index message_location_uid_idx on message_location (account_id, folder, uidvalidity, uid);
create index message_location_labels_idx on message_location using gin (labels);

create table participant (
    message_id  bigint not null references message (id) on delete cascade,
    role        text not null check (role in ('from', 'sender', 'reply_to', 'to', 'cc', 'bcc')),
    address     text not null,
    name        text,
    ordinal     smallint not null default 0,
    primary key (message_id, role, address)
);
create index participant_address_idx on participant (address);

create table attachment (
    id               bigint primary key references entity (id) on delete cascade,
    message_id       bigint not null references message (id) on delete cascade,
    blob_sha256      text not null references blob (sha256),
    part_path        text not null,               -- MIME part position, e.g. '2.1'
    filename         text,
    content_type     text not null,
    size_bytes       bigint not null,
    disposition      text,
    content_id       text,
    extract_status   text not null default 'pending'
                       check (extract_status in ('pending', 'ok', 'empty', 'unsupported', 'error')),
    extracted_text   text,
    attrs            jsonb not null default '{}', -- EXIF, page count, sheet names
    unique (message_id, part_path)
);
create index attachment_blob_idx on attachment (blob_sha256);
create index attachment_type_idx on attachment (content_type);

-- ---------------------------------------------------------------- L2 graph and objects

create table edge (
    src         bigint not null references entity (id) on delete cascade,
    rel         text not null,
    dst         bigint not null references entity (id) on delete cascade,
    source      text not null,                    -- ingest | human | rule:<id>@<v> | model:<run>
    props       jsonb not null default '{}',
    created_at  timestamptz not null default now(),
    primary key (src, rel, dst, source)
);
create index edge_dst_idx on edge (dst, rel);
create index edge_rel_idx on edge (rel);

create table object (
    id           bigint primary key references entity (id) on delete cascade,
    kind         text not null check (kind in ('project', 'case', 'collection', 'area', 'saved_search')),
    name         text not null,
    description  text,
    attrs        jsonb not null default '{}',
    query        jsonb,                           -- live membership: a rule-style condition list
    archived     boolean not null default false,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

create table dimension (
    id           text primary key,                -- topic, type, action, area, tag
    label        text not null,
    cardinality  text not null check (cardinality in ('one', 'many')),
    allowed      jsonb,                           -- list of values, or null for free values
    description  text
);

create table assignment (
    id            bigserial primary key,
    entity_id     bigint not null references entity (id) on delete cascade,
    dimension_id  text not null references dimension (id),
    value         text not null,
    source_kind   text not null check (source_kind in ('human', 'rule', 'model', 'import')),
    source_ref    text,                           -- rule id@version, model run id
    status        text not null default 'active'
                    check (status in ('active', 'proposed', 'rejected', 'superseded')),
    confidence    real,
    evidence      jsonb not null default '{}',
    created_at    timestamptz not null default now(),
    decided_by    text,
    decided_at    timestamptz
);
create index assignment_entity_idx on assignment (entity_id, dimension_id) where status = 'active';
create index assignment_value_idx on assignment (dimension_id, value) where status = 'active';
create index assignment_source_idx on assignment (source_kind, source_ref);
create index assignment_proposed_idx on assignment (dimension_id) where status = 'proposed';

-- The value that counts. Human beats rule beats accepted model beats import; the
-- newest wins inside a tier. For 'many' dimensions every active value counts.
create view effective_assignment as
with ranked as (
    select a.*, d.cardinality,
           row_number() over (
               partition by a.entity_id, a.dimension_id
               order by case a.source_kind when 'human' then 4 when 'rule' then 3
                                           when 'model' then 2 else 1 end desc,
                        a.created_at desc, a.id desc) as rn
    from assignment a
    join dimension d on d.id = a.dimension_id
    where a.status = 'active'
)
select entity_id, dimension_id, value, source_kind, source_ref, confidence, created_at
from ranked
where cardinality = 'many' or rn = 1;

create table rule (
    id           text primary key,
    version      integer not null default 1,
    name         text not null,
    description  text,
    enabled      boolean not null default true,
    priority     integer not null default 100,    -- lower runs first
    conditions   jsonb not null,                  -- [{field, op, value}], ANDed
    action       jsonb not null,                  -- {dimension, value} | {object} | {event}
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

create table model_run (
    id            text primary key,
    model         text not null,
    purpose       text not null,
    params        jsonb not null default '{}',
    input_count   integer not null default 0,
    output_count  integer not null default 0,
    created_at    timestamptz not null default now()
);

create table event (
    id                 bigint primary key references entity (id) on delete cascade,
    message_id         bigint not null references message (id) on delete cascade,
    extractor          text not null,
    extractor_version  integer not null,
    system             text,                      -- which system reported it
    kind               text not null,             -- backup, alarm, alert, notification
    status             text,                      -- ok, warning, failed, info
    occurred_at        timestamptz,
    fields             jsonb not null default '{}',
    unique (message_id, extractor)
);
create index event_kind_idx on event (kind, occurred_at desc);

-- ---------------------------------------------------------------- sync

create table sync_cursor (
    account_id  text not null references account (id) on delete cascade,
    scope       text not null,                    -- folder, or 'all'
    state       jsonb not null,
    updated_at  timestamptz not null default now(),
    primary key (account_id, scope)
);

create table sync_run (
    id           bigserial primary key,
    account_id   text not null references account (id) on delete cascade,
    started_at   timestamptz not null default now(),
    finished_at  timestamptz,
    status       text not null default 'running' check (status in ('running', 'ok', 'partial', 'failed')),
    seen         integer not null default 0,
    added        integer not null default 0,
    updated      integer not null default 0,
    error        text
);

-- ---------------------------------------------------------------- L3 changesets

create table changeset (
    id            bigserial primary key,
    title         text not null,
    status        text not null default 'draft'
                    check (status in ('draft', 'planned', 'committed', 'applying', 'done', 'failed', 'cancelled')),
    selection     jsonb not null default '{}',    -- how the messages were chosen
    request       jsonb not null default '{}',    -- the operation asked for
    summary       jsonb not null default '{}',    -- counts per account, folder and op
    note          text,
    created_at    timestamptz not null default now(),
    planned_at    timestamptz,
    committed_at  timestamptz,
    finished_at   timestamptz
);

create table changeset_op (
    id            bigserial primary key,
    changeset_id  bigint not null references changeset (id) on delete cascade,
    message_id    bigint not null references message (id) on delete cascade,
    account_id    text not null references account (id),
    op            text not null check (op in ('mark_read', 'mark_unread', 'flag', 'unflag',
                                              'archive', 'move', 'trash', 'add_label', 'remove_label')),
    args          jsonb not null default '{}',
    status        text not null default 'pending' check (status in ('pending', 'done', 'skipped', 'failed')),
    attempts      integer not null default 0,
    error         text,
    inverse       jsonb,                          -- the op that undoes this one
    applied_at    timestamptz,
    unique (changeset_id, message_id, op, args)
);
create index changeset_op_pending_idx on changeset_op (changeset_id, account_id) where status = 'pending';

-- ---------------------------------------------------------------- seed dimensions

insert into dimension (id, label, cardinality, description) values
    ('topic',  'Topic',        'one',  'What it is about. Seeded from taxonomy v2.'),
    ('type',   'Message type', 'one',  'receipt, newsletter, notification, personal, work, alert…'),
    ('action', 'Action',       'one',  'to_reply, to_do, to_report, waiting, done, reference'),
    ('area',   'Area',         'one',  'Work, Signals or Operations — the Talos zones'),
    ('tag',    'Tag',          'many', 'Free tags; any number per entity');
