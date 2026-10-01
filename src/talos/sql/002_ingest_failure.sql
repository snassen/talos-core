-- A message that could not be turned into rows is still kept: its original is in the
-- vault, and it is listed here with what went wrong and where it came from, so a fix to
-- the parser can re-ingest it later (talos retry). One bad message never stops a sync.

create table ingest_failure (
    account_id    text not null references account (id) on delete cascade,
    provider_key  text not null,
    raw_sha256    text not null references blob (sha256),
    location      jsonb not null,
    error         text not null,
    attempts      integer not null default 1,
    first_at      timestamptz not null default now(),
    last_at       timestamptz not null default now(),
    primary key (account_id, provider_key)
);
