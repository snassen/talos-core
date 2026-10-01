-- Importance: how much a message deserves the owner's attention (talos.importance).
--
-- Written for a populated database: it only adds a dimension and two tables. The scores
-- are filled by `talos importance` (a full run takes about a minute), not here.

-- One row per message. `score` is the sum of the signals that fired, `reasons` lists each
-- of them with its points ([{"signal", "points", "text"}]), so any score can be explained.
-- `level` is the effective level: the owner's own mark (the importance dimension) overrides
-- the score. `waiting` is an incoming question to the owner with no later reply from them in
-- the thread. `received_at` is the message's, copied so "recent and important" is one index.
-- `version` is importance.VERSION when the row was computed; a change of weights bumps it.
create table message_importance (
    message_id   bigint primary key references message (id) on delete cascade,
    score        integer not null,
    level        text not null check (level in ('high', 'medium', 'low', 'noise')),
    reasons      jsonb not null default '[]',
    waiting      boolean not null default false,
    received_at  timestamptz,
    version      integer not null,
    computed_at  timestamptz not null default now()
);
create index message_importance_recent_idx on message_importance (received_at desc)
    where level in ('high', 'medium');
create index message_importance_waiting_idx on message_importance (received_at)
    where waiting;

-- VIP senders: a whole address (nils@nordvik.se) or a domain (nordvik.se), lower-cased.
-- Seeded empty; `talos importance vip-add ADDRESS|DOMAIN` fills it.
create table importance_vip (
    pattern     text primary key check (pattern = lower(pattern) and pattern <> ''),
    note        text,
    created_at  timestamptz not null default now()
);

-- The owner's own verdict, which beats every signal (promise: human beats rule).
insert into dimension (id, label, cardinality, allowed, description) values
    ('importance', 'Importance', 'one', '["important", "not_important"]',
     'Your own verdict on a message: important forces high, not_important forces noise')
on conflict (id) do nothing;
