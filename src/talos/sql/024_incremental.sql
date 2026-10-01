-- Incremental enrichment: new mail gets Jev's judgement shortly after it arrives (talos.incremental,
-- docs/enrichment-plan.md §6 "Incremental").
--
-- Written for a populated database: one new column, two new indexes, one new table and one Argus
-- service row. Nothing existing is changed.

-- A case that was not sent to Jev but reuses earlier answers (a template whose three samples
-- agreed, or the thread or window a new message joined) says where its answers came from; its
-- proposals carry it as evidence.propagated_from. NULL for a case Jev answered.
alter table enrich_case add column source jsonb;

-- "Is this message a member of any case?" for a few hundred new messages, every quarter hour.
create index enrich_case_members_idx on enrich_case using gin (member_ids);
-- A unit's earlier cases, by key (a template, a thread, a window), across runs.
create index enrich_case_unit_key_idx on enrich_case (unit_key);

-- One invocation of `talos enrich jev new` (by the sync hook or by hand): the throttle reads the
-- last one the hook made, the daily budget sums what the hook spent today.
--   trigger   sync (the hook after talos sync --then-rules) | cli (by hand)
--   runs      the model runs it made (full set, then the focused kind and value runs)
--   summary   counts: selected, reused, asked, deferred, accepted, placed; or the error
create table enrich_tick (
    id           bigserial primary key,
    started_at   timestamptz not null default now(),
    finished_at  timestamptz,
    trigger      text not null check (trigger in ('sync', 'cli')),
    backlog      boolean not null default false,
    ok           boolean,
    runs         text[] not null default '{}',
    cases        integer not null default 0,
    reused       integer not null default 0,
    cost_usd     double precision not null default 0,
    summary      jsonb not null default '{}'
);
create index enrich_tick_trigger_idx on enrich_tick (trigger, started_at desc);

-- Its Argus service: a push check-in after each run of the hook (talos.argus.DAY_ONE has it too).
insert into argus_service (slug, name, kind, grace_seconds, notes)
values ('talos-enrich', 'Talos enrichment', 'push', 900,
        'talos sync --then-rules runs talos enrich jev new at most every 15 minutes when TALOS_HOME/enrich.json'
        ' says {"enabled": true}; it checks in, in-process, after each run, expecting the next within the'
        ' cadence plus one sync')
on conflict (slug) do nothing;
