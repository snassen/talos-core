-- Unlocking labelling, improvement jobs and machine-mail clusters (talos.unlock, talos.clusters).
--
-- Written for a populated database: three new tables, nothing existing changed.

-- A computed answer the Overview and the Clusters page read instead of scanning the archive on
-- every load: the low-hanging fruit (sender groups whose undecided mail one answer would unlock),
-- the improvement jobs and the machine-mail clusters. fingerprint says what the answer was
-- computed from (cheap counters: the newest ids, the tables' write counts, the accept stamps);
-- when it no longer matches, the answer is shown as it was and computed again behind it.
create table insight_cache (
    name         text primary key,
    fingerprint  text not null,
    computed_at  timestamptz not null default now(),
    seconds      real,
    payload      jsonb not null
);

-- An improvement job the owner marked done or dismissed: it is no longer shown. key is the job's own
-- ('focus:topic:uncertain', 'claude:unlock'); deleting the row shows the job again.
create table improvement_job (
    key         text primary key,
    state       text not null check (state in ('done', 'dismissed')),
    field       text,
    note        text,
    decided_at  timestamptz not null default now()
);

-- A sender-group item of an unlock answer key stands for these messages: the undecided mail of
-- its sender (and system) when the set was drawn. Like gold_group_case, but by message, since
-- undecided mail need not be a backfill case. gold.propagate_groups gives the owner's answer to both.
create table gold_group_message (
    item_id     integer not null references gold_item (id) on delete cascade,
    message_id  bigint not null,
    primary key (item_id, message_id)
);
