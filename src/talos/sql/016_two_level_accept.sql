-- Two-level acceptance, the acceptance explorer and focused Jev runs (talos.boundary,
-- talos.backfill.accept, talos.acceptance, talos.focus; docs/enrichment-plan.md §11).
--
-- Written for a populated database: one new column, four new dimensions, two wider checks and
-- three partial indexes. Nothing existing is changed.

-- A derived dimension names its source and maps every family of the source's values to one of
-- its own values (rules/taxonomy.json, `talos taxonomy load`).
alter table dimension add column derived_from jsonb;

-- The four boundaries. Their value lists and mappings come with `talos taxonomy load`; the rows
-- exist from here on so that assignments can reference them.
insert into dimension (id, label, cardinality, description) values
    ('sender_kind', 'Sender', 'one', 'Who produced the message, a human or a machine. Derived from origin.'),
    ('sphere', 'Work or personal', 'one', 'Whether the message is work or personal. Derived from topic.'),
    ('form', 'Conversation or other', 'one', 'Whether the message is a conversation or something else. Derived from type.'),
    ('keep', 'Worth keeping', 'one', 'Whether the message is worth keeping or short-lived. Derived from value.')
on conflict (id) do nothing;

-- A focused run (talos.focus) asks one question per case; sender_kind is asked directly there, and
-- on the answer key. A backfill run's boundaries are derived from its stored scores when its
-- proposals are written, never stored as predictions.
alter table enrich_prediction drop constraint enrich_prediction_field_check;
alter table enrich_prediction add constraint enrich_prediction_field_check
    check (field in ('origin', 'type', 'topic', 'ask', 'value', 'route', 'sender_kind', 'sphere', 'form', 'keep'));
alter table jev_prediction drop constraint jev_prediction_field_check;
alter table jev_prediction add constraint jev_prediction_field_check
    check (field in ('origin', 'type', 'topic', 'ask', 'value', 'route', 'sender_kind', 'sphere', 'form', 'keep'));

-- People / Automated reads sender_kind before origin (search.machine_sql): the same index-only
-- lookup assignment_origin_idx gives origin.
create index assignment_sender_kind_idx on assignment (entity_id) include (value, source_kind, created_at, id)
    where dimension_id = 'sender_kind' and status = 'active';

-- A boundary filter in Messages (sphere, keep, …) groups the dimension's values per entity: this
-- index gives them in entity order, from the index alone.
create index assignment_boundary_idx on assignment (dimension_id, entity_id) include (value, source_kind, created_at, id)
    where status = 'active' and dimension_id in ('sender_kind', 'sphere', 'form', 'keep');

-- The acceptance explorer's examples: the proposals of a field nearest a threshold, by index.
create index assignment_model_confidence_idx on assignment (dimension_id, confidence)
    where source_kind = 'model' and status in ('proposed', 'active');

-- A newer run supersedes an older run's proposals for the same message and field; that update
-- finds the older rows by message.
create index assignment_model_entity_idx on assignment (entity_id, dimension_id)
    where source_kind = 'model' and status in ('proposed', 'active');
