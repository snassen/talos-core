-- Kind: the coarse level above type (docs/mailbox-structure-plan.md, decisions 7 and 8), answer
-- keys that label a chosen set of fields, and sender groups in the answer key.
--
-- Written for a populated database: one new dimension row, wider field checks, the valid-label
-- view made again, one new table and one partial index. Nothing existing is changed.

-- Kind's value list and its mapping from type come with `talos taxonomy load`; the row exists
-- from here on so that assignments and answers can reference it.
insert into dimension (id, label, cardinality, description) values
    ('kind', 'Kind', 'one', 'What the message is to you, in one coarse word. Derived from type, or asked directly.')
on conflict (id) do nothing;

-- The answer key may label kind (a set says which fields it labels: gold_set.params.label_fields),
-- and a sender-group item carries the owner's "mixed group" tick as the field 'mixed' (like the note, not a
-- dimension: a row means ticked).
alter table gold_label drop constraint gold_label_field_check;
alter table gold_label add constraint gold_label_field_check
    check (field in ('origin', 'type', 'topic', 'ask', 'value', 'route', 'kind', 'note', 'mixed'));

drop view gold_label_valid;
create view gold_label_valid as
select l.* from gold_label l
where l.field in ('note', 'mixed')
   or exists (select 1 from dimension d where d.id = l.field
              and (d.allowed is null or not exists (select 1 from unnest(l.values) v where not d.allowed ? v)));

-- A focused run asks kind directly, on the archive and on the answer key; a backfill run's kind is
-- derived from its stored type scores when its proposals are written, never stored.
alter table enrich_prediction drop constraint enrich_prediction_field_check;
alter table enrich_prediction add constraint enrich_prediction_field_check
    check (field in ('origin', 'type', 'topic', 'ask', 'value', 'route', 'sender_kind', 'sphere', 'form', 'keep',
                     'kind'));
alter table jev_prediction drop constraint jev_prediction_field_check;
alter table jev_prediction add constraint jev_prediction_field_check
    check (field in ('origin', 'type', 'topic', 'ask', 'value', 'route', 'sender_kind', 'sphere', 'form', 'keep',
                     'kind'));

-- A sender-group item of the answer key stands for every unsure backfill case of one sender (five
-- or more): these are its cases, frozen when the set is drawn. The owner's answer for the item can
-- later be given to every message of them (gold.propagate_groups), unless ticked "mixed".
create table gold_group_case (
    item_id  integer not null references gold_item (id) on delete cascade,
    case_id  bigint not null,
    primary key (item_id, case_id)
);

-- The kind filter in Messages groups kind's values per entity, as the boundary filters do.
create index assignment_kind_idx on assignment (dimension_id, entity_id) include (value, source_kind, created_at, id)
    where status = 'active' and dimension_id = 'kind';
