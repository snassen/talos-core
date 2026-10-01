-- The Rules page counts each rule's matches. Rule-made memberships are a small slice of a
-- large edge table (1.16M rows on the real archive), so index just that slice.
create index if not exists edge_rule_source_idx on edge (source) where source like 'rule:%';
