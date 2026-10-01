-- The owner is named by the code, never by the schema (talos.personal.OWNER_ID, from owner.json).
--
-- Columns that say who did something had the owner as their default, and watcher and aggregation
-- allowed only the owner or claude. Now the code always names who, and any name is allowed.
-- No row changes: what is stored stays as it is.

alter table gold_label alter column labeller drop default;
alter table send_log alter column by_whom drop default;
alter table rule_removed alter column removed_by drop default;
alter table watcher alter column made_by drop default;
alter table aggregation alter column made_by drop default;

alter table watcher drop constraint watcher_made_by_check;
alter table watcher add constraint watcher_made_by_check check (length(btrim(made_by)) > 0);
alter table aggregation drop constraint aggregation_made_by_check;
alter table aggregation add constraint aggregation_made_by_check check (length(btrim(made_by)) > 0);
