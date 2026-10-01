-- Enrichment step A: the deterministic pre-pass (talos.enrich, docs/enrichment-plan.md §3–4).
--
-- Written for a populated database: it adds a dimension, closes the value list of `type`,
-- adds two SQL functions and three tables, and widens nothing. The tables are filled by
-- `talos enrich prepass` (about a minute on the real archive), not here.

-- ---------------------------------------------------------------- origin, and a closed type

-- Who produced a message. Rules fill it where the signals are unambiguous; Jev the rest.
insert into dimension (id, label, cardinality, allowed, description) values
    ('origin', 'Origin', 'one',
     '["person", "person_via_system", "transactional", "notification", "marketing", "alert", "list"]',
     'Who produced it: a person, a person through a system (a ticket reply, a platform message),'
     ' or a machine (transactional, notification, marketing, alert) or a mailing list')
on conflict (id) do nothing;

-- `type` becomes a closed list (plan §3). The rules on the real archive write only values in
-- it (notification, backup, alert, newsletter, receipt, invitation, invoice). Any other value
-- already in use, by an assignment or a saved rule's action, is kept in the list, so closing
-- it never makes an existing value or rule invalid.
update dimension d set
    allowed = (
        select jsonb_agg(v order by o, v)
        from (
            select v, min(o) as o from (
                select v, o from unnest(array['conversation', 'request', 'invitation', 'receipt', 'invoice', 'order',
                                              'booking', 'newsletter', 'promotion', 'notification', 'alert', 'backup',
                                              'security', 'document', 'other']) with ordinality as x(v, o)
                union all
                select value, 1000 from assignment where dimension_id = 'type' and status in ('active', 'proposed')
                union all
                select action ->> 'value', 1000 from rule where action ->> 'dimension' = 'type'
                                                         and coalesce(action ->> 'value', '') <> ''
            ) all_values
            group by v
        ) closed),
    description = 'What kind of message it is (closed list; see docs/enrichment-plan.md)'
where d.id = 'type' and d.allowed is null;

-- ---------------------------------------------------------------- subject patterns

-- The subject skeleton (plan §4.2): lower-cased; reply and forward prefixes stripped (Re, Sv,
-- Fw, Fwd, VB, AW, WG, TR, RV, VS, Antw, also "Re[2]:" and repeated ones); every token that
-- contains a digit becomes '#', and a run of them one '#'; punctuation dropped; the first
-- three words kept. "SV: VB: Faktura 2026-09 från Telia" → "faktura # från".
-- One definition, used by ingest, the pre-pass and the tests alike.
create or replace function subject_skeleton(subject text) returns text
language sql immutable parallel safe as $$
    select coalesce(substring(
        regexp_replace(
            regexp_replace(
                regexp_replace(
                    regexp_replace(lower(coalesce(subject, '')),
                        '^(\s*(re|sv|fw|fwd|vb|aw|wg|tr|rv|vs|antw)\s*(\[[0-9]+\]|\([0-9]+\))?\s*:\s*)+', ''),
                    '\S*[0-9]\S*', '#', 'g'),
                '[^[:alnum:]#[:space:]]+', '', 'g'),
            '#(\s+#)+', '#', 'g')
        from '^\s*(\S+(\s+\S+){0,2})'), '')
$$;

-- Pattern key = sender address + skeleton. Mail from one sender with one subject template
-- shares a key, so Jev can see a few samples per template instead of every message.
create or replace function subject_pattern_key(from_address text, subject text) returns text
language sql immutable parallel safe as $$
    select lower(coalesce(from_address, '')) || '|' || subject_skeleton(subject)
$$;

-- Each e-mail's pattern (Teams has none: its unit is a conversation window). Written at
-- ingest; `talos enrich prepass` fills in older messages and refreshes a changed subject.
create table message_pattern (
    message_id   bigint primary key references message (id) on delete cascade,
    pattern_key  text not null,
    skeleton     text not null
);
create index message_pattern_key_idx on message_pattern (pattern_key);

-- The patterns with their counts; rebuilt by the pre-pass. sample_ids are up to three
-- messages spread over time (first, middle, last): what Jev would see for the template.
create table subject_pattern (
    pattern_key    text primary key,
    from_address   text not null,
    skeleton       text not null,
    message_count  integer not null,
    thread_count   integer not null,
    accounts       text[] not null default '{}',
    first_at       timestamptz,
    last_at        timestamptz,
    sample_ids     bigint[] not null default '{}',
    computed_at    timestamptz not null default now()
);
create index subject_pattern_from_idx on subject_pattern (from_address);

-- ---------------------------------------------------------------- sender profiles

-- One row per (account, from address) of incoming mail; rebuilt by the pre-pass.
-- written_to counts the owner's outgoing messages in this account with the address in To/Cc/Bcc,
-- written_to_all in any account. replied_threads: their threads the owner has written in.
-- The shares are over their incoming messages. templates: distinct subject skeletons.
create table sender_profile (
    account_id              text not null references account (id) on delete cascade,
    address                 text not null,
    message_count           integer not null,
    first_at                timestamptz,
    last_at                 timestamptz,
    written_to              integer not null default 0,
    written_to_all          integer not null default 0,
    last_written_at         timestamptz,
    threads                 integer not null default 0,
    replied_threads         integer not null default 0,
    list_unsubscribe_share  real not null default 0,
    automated_share         real not null default 0,
    event_share             real not null default 0,
    templates               integer not null default 0,
    computed_at             timestamptz not null default now(),
    primary key (account_id, address)
);
create index sender_profile_address_idx on sender_profile (address);

-- "Has the owner written in this thread?" is asked of every message by the pre-pass (and by
-- importance): the owner's outgoing messages are a sixth of the table, so index just those. On the
-- real archive the distinct-thread scan drops from about 210 ms to a few.
create index message_out_thread_idx on message (thread_id) where direction = 'out';

-- The pre-pass writes origin as rule-made assignments with source_ref 'prepass:origin.<signal>@<v>'.
-- rules.run_all deletes only source_ref 'rule:…', and the pre-pass only its own prefix, so
-- neither wipes the other's; this index serves the pre-pass's diff against its own rows.
create index assignment_prepass_idx on assignment (entity_id)
    where source_kind = 'rule' and source_ref like 'prepass:%';
