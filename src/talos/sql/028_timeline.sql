-- The timeline (Work › Timeline, a Gantt over the binders). A work item gets a
-- start date beside its due date, so it can be a bar from start to due; a binder (a project above
-- all) may have its own start and end, which otherwise follow from its items. docs/timeline.md.

alter table work_item add column start_on date;
alter table work_item add constraint work_item_start_before_due check (start_on is null or due is null or start_on <= due);

alter table object add column starts_on date;
alter table object add column ends_on date;
alter table object add constraint object_start_before_end check (starts_on is null or ends_on is null or starts_on <= ends_on);
