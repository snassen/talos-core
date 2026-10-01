-- The answer key gets a labeller: Claude labels every item, the owner checks a random sample.
--
-- Written for a populated database. Every existing answer is the owner's (the default). The
-- blind screen, its progress and its sessions read and write only the owner's; another labeller's
-- answers are for comparing ('claude' today), and never shown on the blind screen.

alter table gold_label add column labeller text not null default 'owner'
    check (length(btrim(labeller)) > 0);

alter table gold_label drop constraint gold_label_pkey;
alter table gold_label add primary key (item_id, field, labeller);

-- The view froze gold_label's columns when it was made; made again, it carries the labeller.
drop view gold_label_valid;
create view gold_label_valid as
select l.* from gold_label l
where l.field = 'note'
   or exists (select 1 from dimension d where d.id = l.field
              and (d.allowed is null or not exists (select 1 from unnest(l.values) v where not d.allowed ? v)));

-- The check: a frozen random sample of the items another labeller answered, which the owner
-- checks field by field (agree, or correct). The owner's answers there are ordinary labels; this
-- table only says which items are in the sample and in what order. rank is the checking order.
create table gold_check_item (
    set_id      integer not null references gold_set (id) on delete cascade,
    item_id     integer not null references gold_item (id) on delete cascade,
    rank        integer not null,
    labeller    text not null,                 -- whose labels are being checked
    seed        integer not null,
    created_at  timestamptz not null default now(),
    primary key (set_id, item_id),
    unique (set_id, rank)
);
