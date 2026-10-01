-- Search that uses its indexes, message values that see their thread, and encrypted PDFs.

-- A search for an address fragment ("nordvik") matches from_address with ilike; like the
-- subject, it needs a trigram index so the search does not read every message.
create index if not exists message_from_trgm_idx on message using gin (from_address gin_trgm_ops);

-- A PDF that needs a password is marked as such, not as an extraction error.
alter table attachment drop constraint attachment_extract_status_check;
alter table attachment add constraint attachment_extract_status_check
    check (extract_status in ('pending', 'ok', 'empty', 'unsupported', 'encrypted', 'error'));

-- The value a MESSAGE has in a dimension, counting its own assignments and its thread's.
--
-- effective_assignment ranks per entity: a message's values, a thread's values. But rules
-- and humans assign messages while cases (Jev) and thread-level decisions assign threads,
-- so a human decision on a thread must still beat a rule's value on one of its messages.
-- Here both sets compete together: human beats rule beats accepted model beats import,
-- across message and thread; within a tier the message's own assignment beats the
-- thread's, then the newest wins. For 'many' dimensions every active value of either
-- counts, once. `via` says where the winning value sits: 'message' or 'thread'.
--
-- Filtering on message_id (a partition column) pushes down to the message row, so asking
-- for one message reads only its own and its thread's assignments, by index.
create view effective_message_assignment as
select message_id, dimension_id, value, source_kind, source_ref, confidence, created_at, entity_id, via
from (
    select c.*,
           row_number() over (
               partition by c.message_id, c.dimension_id
               order by c.tier desc, c.own desc, c.created_at desc, c.id desc) as rn,
           row_number() over (
               partition by c.message_id, c.dimension_id, c.value
               order by c.tier desc, c.own desc, c.created_at desc, c.id desc) as value_rn
    from (
        select m.id as message_id, a.id, a.entity_id, a.dimension_id, a.value, a.source_kind, a.source_ref,
               a.confidence, a.created_at, d.cardinality,
               a.entity_id = m.id as own,
               case when a.entity_id = m.id then 'message' else 'thread' end as via,
               case a.source_kind when 'human' then 4 when 'rule' then 3 when 'model' then 2 else 1 end as tier
        from message m
        join assignment a on a.entity_id = any (array[m.id, m.thread_id]) and a.status = 'active'
        join dimension d on d.id = a.dimension_id
    ) c
) ranked
where (cardinality = 'one' and rn = 1) or (cardinality = 'many' and value_rn = 1);
