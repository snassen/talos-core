-- Enrichment step C: Jev on the answer key, for evaluation only.
--
-- Written for a populated database: two new tables, nothing changed. A run is a model_run row
-- (purpose 'gold-eval'; params hold the set, the unit design, the question version, the policy,
-- tokens, cost and wall time). Nothing here is an assignment: a Jev answer on the answer key is
-- scored, never proposed.

-- One case sent and answered: the answer-key item, the exact record's hash (what left the Mac
-- can be rebuilt and checked), the model that answered, its token usage and the time it took.
-- A rerun of the same run skips the items that have a row here.
create table jev_case (
    run_id          text not null references model_run (id) on delete cascade,
    item_id         integer not null references gold_item (id) on delete cascade,
    record_sha256   text not null,
    record_chars    integer not null,
    model           text not null,
    input_tokens    integer not null default 0,
    output_tokens   integer not null default 0,
    elapsed_ms      integer,
    created_at      timestamptz not null default now(),
    primary key (run_id, item_id)
);

-- One field of one case, routed by the policy of the run (talos.policy):
--   top         the best value (one-value field) or the best-scored statement (ask, route)
--   selected    the decided values: {top} when a one-value field is decided, {} when it is not;
--               the statements at or over the threshold for ask and route
--   scores      every choice's probability, or every statement's score (value -> number)
--   confidence  the top probability; for ask and route the least certain statement's certainty
--   margin      top minus runner-up (one-value fields)
--   raw         Jev's answer(s) for the field, as returned
-- (Question templates v2, recorded in model_run.params.template_version, ask ask and route as
-- one choice with 'none': top is the choice's top, scores its probabilities plus ask's
-- 'deadline' statement, selected is gated by talos.policy, and raw names any gate that fired.)
create table jev_prediction (
    run_id      text not null references model_run (id) on delete cascade,
    item_id     integer not null references gold_item (id) on delete cascade,
    field       text not null check (field in ('origin', 'type', 'topic', 'ask', 'value', 'route')),
    top         text,
    selected    text[] not null default '{}',
    scores      jsonb not null,
    confidence  double precision,
    margin      double precision,
    decided     boolean not null,
    raw         jsonb not null,
    primary key (run_id, item_id, field),
    foreign key (run_id, item_id) references jev_case (run_id, item_id) on delete cascade
);
