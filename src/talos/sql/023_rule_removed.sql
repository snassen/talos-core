-- Removing a rule, and where a rule came from (talos.rules.remove, talos.suggest).
--
-- A removed rule leaves the rule table, and so do the values and memberships it made; its
-- definition is kept here, with when and why, so nothing is lost and it can be put back.
create table rule_removed (
    id          bigserial primary key,
    rule_id     text not null,
    definition  jsonb not null,                      -- the rule row as it was (id, version, conditions, action…)
    removed_at  timestamptz not null default now(),
    why         text,
    removed_by  text not null default 'owner'
);
create index rule_removed_rule_idx on rule_removed (rule_id, removed_at desc);

-- Where a rule came from, when Talos made it: one suggestion ({"suggestion": id}) or a ticked
-- group of suggestions ({"group": {"dimension", "value", "members": [id, …]}}). Null for a rule
-- written by hand or loaded from a file. The suggestions read it: a member of a group rule is
-- covered, and comes back as a suggestion when the group rule is removed.
alter table rule add column origin jsonb;
