-- The enrichment backfill: Jev over the whole archive in collapsed units, as proposals
-- (talos.backfill, docs/enrichment-plan.md §4.2, §5, §6).
--
-- Written for a populated database: two new tables and one partial index, nothing changed.
-- A run is a model_run row (purpose 'enrich-backfill'; params hold the stage, the question and
-- record versions, the policy, tokens, cost and wall time). Its answers are stored here per
-- case; what they propose for messages is written as ordinary `assignment` rows with
-- source_kind 'model', status 'proposed' and source_ref = the run id, so the effective views
-- (which read only status 'active') ignore them until `talos enrich accept` makes them active.
--
-- Why not jev_case: a jev_case row is an answer-key item (its key is gold_item.id, and the
-- evaluation reads it by item). A backfill case is a unit of the archive (a template sample, a
-- thread, a Teams window) with the messages it stands for, and it needs its own key, its members
-- and a place for errors, so resuming can retry what failed.

-- One case: a unit sent to Jev (or tried and failed).
--   stage        machine | person | teams
--   unit_kind    template (one of a template's three samples) | tail (a small template, or a
--                sender's small templates) | thread | window
--   unit_key     template:<pattern key> | tail:<pattern key> or tail-sender:<address> |
--                thread:<thread id> (message:<id> without one) | window:<chat thread id>:<first id>
--   sample_no    0..2 for the three samples of a template, otherwise 0
--   anchor_id    the message the record is anchored on (for a thread: its latest incoming one)
--   member_ids   the messages the case stands for (a template: all of its messages)
--   error        why it failed (then there are no predictions; a resumed run tries it again)
create table enrich_case (
    id             bigserial primary key,
    run_id         text not null references model_run (id) on delete cascade,
    stage          text not null check (stage in ('machine', 'person', 'teams')),
    unit_kind      text not null check (unit_kind in ('template', 'tail', 'thread', 'window')),
    unit_key       text not null,
    sample_no      smallint not null default 0,
    anchor_id      bigint not null references message (id) on delete cascade,
    member_ids     bigint[] not null,
    record_sha256  text,
    record_chars   integer,
    model          text,
    input_tokens   integer not null default 0,
    output_tokens  integer not null default 0,
    elapsed_ms     integer,
    error          text,
    created_at     timestamptz not null default now(),
    unique (run_id, unit_key, sample_no)
);

-- One field of one case, routed and gated by the run's policy (talos.policy.route_case):
--   top         the best value (a one-value field, or ask's and route's choice, 'none' included)
--   selected    the decided values: {top} when decided, {} when not; ask and route after the gates
--   scores      every value's probability (and ask's 'deadline' statement score)
--   top3        the three best scores, as written into the proposals' evidence
--   confidence  the top probability; margin: top minus runner-up
--   gate        the gates that forced the field to none (ask, route), with ungated what it was before
--   fixed       not asked but fixed (a Teams window's origin): never proposed
create table enrich_prediction (
    case_id     bigint not null references enrich_case (id) on delete cascade,
    field       text not null check (field in ('origin', 'type', 'topic', 'ask', 'value', 'route')),
    top         text,
    selected    text[] not null default '{}',
    scores      jsonb not null,
    top3        jsonb not null default '{}',
    confidence  double precision,
    margin      double precision,
    decided     boolean not null,
    gate        text[] not null default '{}',
    ungated     text[],
    fixed       boolean not null default false,
    primary key (case_id, field)
);

-- A run's proposals, per field: what accept, unaccept and a rerun's replacement read and write
-- (about 230k messages × 6 fields for a whole backfill). Only model rows are indexed.
create index assignment_model_run_idx on assignment (source_ref, dimension_id) where source_kind = 'model';
