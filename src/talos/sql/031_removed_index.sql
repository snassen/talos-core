-- Which messages a Talos changeset moved to the trash, found by message (talos.search REMOVED_COLUMN:
-- the red "Purged" chip on a row asks it for every message in a list).
create index changeset_op_trashed_idx on changeset_op (message_id) where op = 'trash' and status = 'done';
