-- The mailbox structure planner (talos.structure; docs/mailbox-structure-plan.md).
--
-- A plan, not a change: where every mail of an account should go in the new Talos/ structure,
-- computed from its effective values by the rules in rules/structure.json. Nothing here reaches a
-- mailbox; changesets made from the plan stay planned until the owner commits them.
-- Two new tables; nothing existing changes.

-- One row per email message of a planned account. place: 'inbox' (stays in the inbox, no label),
-- 'label' (target is a Talos/… label, or later an Exchange folder) or 'leave' (never touched).
-- in_inbox is the mirror's state when the plan was computed; leaves_inbox = in the inbox now and
-- placed elsewhere. changed_at moves only when the target or leaves_inbox changes, so the daily
-- changeset can ask for what is new since yesterday.
create table structure_plan (
    message_id     bigint primary key references message (id) on delete cascade,
    account_id     text not null references account (id) on delete cascade,
    place          text not null check (place in ('inbox', 'label', 'leave')),
    target         text not null,
    rule_id        text not null,
    in_inbox       boolean not null,
    leaves_inbox   boolean not null,
    rules_version  integer not null,
    rules_sha      text not null,
    computed_at    timestamptz not null default now(),
    changed_at     timestamptz not null default now()
);
create index structure_plan_target_idx on structure_plan (account_id, target);
create index structure_plan_changed_idx on structure_plan (account_id, changed_at);

-- Each computation: full or incremental (after a sync), how long it took and what it counted.
create table structure_run (
    id             bigserial primary key,
    account_id     text not null references account (id) on delete cascade,
    mode           text not null check (mode in ('full', 'incremental')),
    rules_version  integer not null,
    rules_sha      text not null,
    started_at     timestamptz not null default now(),
    seconds        real,
    placed         integer not null default 0,
    summary        jsonb not null default '{}'
);
create index structure_run_account_idx on structure_run (account_id, id desc);
