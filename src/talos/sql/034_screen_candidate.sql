-- Rule candidates mined in the screen lab (talos.screenlab.mine): phrasings common among the attacks talos-doctor's
-- rules missed and Jev caught, and absent from ordinary text, each measured on samples it was not mined from.
-- The owner accepts or rejects each; accepted ones are exported as rules for talos-doctor.
create table screen_candidate (
    id             serial primary key,
    pattern        text not null unique,           -- the regex, over the normalized text
    words          text[] not null,                -- the phrasing it was made from
    train_support  integer not null,               -- attacks it covers in the part it was mined from
    held_attacks   integer not null,               -- attacks it catches in the held-back part
    held_new       integer not null,               -- of those, attacks today's rules miss
    held_benign    integer not null,               -- false alarms in the held-back ordinary text
    benign_all     integer not null,               -- false alarms in all ordinary text, mined part included
    top_source     text,                           -- where most of its catches come from
    top_share      real,                           -- that source's share of them
    status         text not null default 'proposed' check (status in ('proposed', 'accepted', 'rejected')),
    mined_from     jsonb not null default '{}',    -- the runs and split it came from
    mined_at       timestamptz not null default now(),
    decided_at     timestamptz
);
