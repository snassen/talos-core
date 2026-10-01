-- The Studio (talos.studio, docs/studio.md): the owner's quick decisions over groups of mail Jev is
-- unsure of, what each wrote, and what the owner's verdicts on Jev's guesses lifted.
--
-- studio_decision: one card decided (or skipped). scope 'group' writes the owner's values on every message
-- of the card's group, 'one' on its representative alone, 'skip' writes nothing. It keeps the ids of
-- the rows it wrote, the human rows it superseded and the proposals it rejected, so undo can put
-- everything back.
create table studio_decision (
    id              bigserial primary key,
    at              timestamptz not null default now(),
    card            text not null check (card in ('lever', 'check')),
    group_key       text not null,
    scope           text not null check (scope in ('group', 'one', 'skip')),
    rep_id          bigint not null,
    message_ids     bigint[] not null default '{}',
    lines           jsonb not null default '[]',
    settled         integer not null default 0,
    written_ids     bigint[] not null default '{}',
    superseded_ids  bigint[] not null default '{}',
    rejected_ids    bigint[] not null default '{}',
    undone_at       timestamptz
);
create index studio_decision_group_idx on studio_decision (group_key) where undone_at is null;

-- One verdict per line of a decided card whose representative had a Jev value (proposed or
-- accepted): did the owner agree with it? The calibration of Jev's levels is read from these, per cell
-- (field, Jev's value, its level, people or machine).
create table studio_verdict (
    id            bigserial primary key,
    decision_id   bigint not null references studio_decision (id) on delete cascade,
    field         text not null,
    value         text not null,
    level         text not null,
    side          text not null,
    status        text not null,               -- the Jev value's: proposed or active
    confidence    real,
    agreed        boolean not null,
    chosen        text,                        -- the owner's value; null when they only said no
    sender        text
);
create index studio_verdict_cell_idx on studio_verdict (field, value, level, side);

-- A lift: once the owner's verdicts show that Jev's value in a cell is right often enough, the cell's
-- remaining proposals are accepted (status active, decided_by 'studio-lift:<id>'), their confidence
-- set to the calibrated one (Jev's own kept in evidence.jev_confidence). assignment_ids holds every
-- row it accepted, so undo can return them to proposals.
create table studio_lift (
    id              bigserial primary key,
    at              timestamptz not null default now(),
    field           text not null,
    value           text not null,
    level           text not null,
    side            text not null,
    verdicts        integer not null,
    agreed          integer not null,
    calibrated      real not null,
    messages        integer not null default 0,
    assignment_ids  bigint[] not null default '{}',
    undone_at       timestamptz
);
create index studio_lift_cell_idx on studio_lift (field, value, level, side) where undone_at is null;
