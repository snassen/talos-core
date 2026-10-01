-- Taxonomy v1: closed value lists with a family, a label and a one-line description per value.
--
-- Written for a populated database: one column, three dimensions, four origin values and a
-- view. The lists themselves come from rules/taxonomy.json through `talos taxonomy load`
-- (which `talos setup` runs), not from here: the file is the source of truth.

-- value -> {family, label, description}. `allowed` keeps the order of the file; this keeps the
-- words. A value in allowed but not here shows as its raw id.
alter table dimension add column value_meta jsonb not null default '{}';

-- The answer key's fields that were not dimensions yet (docs/enrichment-plan.md §3). Their
-- values are set by `talos taxonomy load`; until then they are open lists.
insert into dimension (id, label, cardinality, description) values
    ('ask',   'Ask',   'many', 'What the message expects of you, or promises you made'),
    ('value', 'Value', 'one',  'How worth keeping and finding again the message is'),
    ('route', 'Route', 'many', 'Which work category the message belongs to')
on conflict (id) do nothing;

-- The pre-pass (talos.enrich, signals v2) now writes these origins, so they are allowed even
-- before the taxonomy is loaded. Appended, so the existing order is kept.
update dimension d set allowed = d.allowed || (
    select coalesce(jsonb_agg(v order by o), '[]') from unnest(array['auto_reply', 'system_report', 'mail_system', 'spam'])
        with ordinality as x(v, o)
    where not d.allowed ? v)
where d.id = 'origin' and d.allowed is not null;

-- The answer key reads its labels through this view: a label whose value is no longer in its
-- field's list (a route label that still holds an object id, a value dropped from the file)
-- reads as unset, so the field is open again. The row stays in gold_label, and the next answer
-- replaces it. A field whose dimension has no list (an open topic) takes any value.
create view gold_label_valid as
select l.* from gold_label l
where l.field = 'note'
   or exists (select 1 from dimension d where d.id = l.field
              and (d.allowed is null or not exists (select 1 from unnest(l.values) v where not d.allowed ? v)));
