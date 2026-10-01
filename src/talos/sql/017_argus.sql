-- Argus, the service monitor (talos.argus; docs/argus.md).
--
-- Operational data, not mail: nothing here is content-addressed or immutable, and none of it
-- belongs in the vault. Three new tables and one single-row table; nothing existing changes.

-- A watched service. push: the service checks in. probe: Argus checks it on a timer (probe holds
-- the spec: {"type": "http" | "launchd" | "disk" | "file_growth", "every": seconds, …}).
-- The check-in facts (last_*, expected_next_at) are written by every check-in; status is what the
-- sweep last saw, and it is only the sweep that changes it and notifies.
create table argus_service (
    slug             text primary key check (slug ~ '^[a-z0-9][a-z0-9._-]{0,62}$'),
    name             text not null,
    kind             text not null check (kind in ('push', 'probe')),
    probe            jsonb,
    grace_seconds    integer not null default 300 check (grace_seconds between 0 and 604800),
    paused           boolean not null default false,
    paused_at        timestamptz,
    notes            text,
    created_at       timestamptz not null default now(),
    last_checkin_at  timestamptz,
    last_ok_at       timestamptz,
    last_fail_at     timestamptz,
    last_ok          boolean,
    last_summary     text,
    expected_next_at timestamptz,  -- when the service said it would be back (at + expected_next_within)
    probe_at         timestamptz,  -- when this service was last probed
    probe_state      jsonb not null default '{}',  -- a probe's memory, e.g. file_growth's baseline size
    status           text not null default 'unknown'
                     check (status in ('unknown', 'up', 'late', 'down', 'failing', 'paused')),
    status_since     timestamptz,
    notified         jsonb not null default '{}',  -- {status: iso time} of the last notice per state
    check ((kind = 'probe') = (probe is not null))
);

-- Every check-in and every probe result, append-only; pruned after 30 days (argus.prune).
create table argus_checkin (
    id               bigserial primary key,
    slug             text not null references argus_service (slug) on delete cascade,
    at               timestamptz not null default now(),
    ok               boolean not null,
    summary          text,
    expected_next_at timestamptz,
    source           text not null check (source in ('push', 'probe'))
);
create index argus_checkin_slug_at_idx on argus_checkin (slug, at desc);
create index argus_checkin_at_idx on argus_checkin (at);

-- The daily rollup, kept after the check-ins are pruned: counted as each check-in arrives.
create table argus_daily (
    slug text not null references argus_service (slug) on delete cascade,
    day  date not null,
    ok   integer not null default 0,
    fail integer not null default 0,
    primary key (slug, day)
);

-- The outbound heartbeat to the hosted dead-man's switch: its own state, one row. The URL itself
-- is never stored here; it lives in the Keychain (argus-heartbeat-url).
create table argus_beat (
    id              integer primary key default 1 check (id = 1),
    last_attempt_at timestamptz,
    last_ok_at      timestamptz,
    last_fail_at    timestamptz,
    last_error      text,
    failures        integer not null default 0,  -- in a row, since the last success
    total_ok        bigint not null default 0,
    total_failures  bigint not null default 0
);
insert into argus_beat (id) values (1);
