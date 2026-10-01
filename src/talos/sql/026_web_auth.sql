-- Signing in to Talos Web (talos.webauth, docs/security.md). Three new tables; nothing existing changes.
-- The credentials themselves (password hash, authenticator secret, recovery codes) are in the Keychain,
-- never here: a copy of this database cannot be used to sign in.

-- A session: only the SHA-256 of its cookie token is kept. It ends at expires_at, after a day unused
-- (checked against last_seen), or when revoked.
create table web_session (
    id          text primary key,
    origin      text not null default '',
    user_agent  text not null default '',
    created_at  timestamptz not null default now(),
    last_seen   timestamptz not null default now(),
    expires_at  timestamptz not null,
    revoked_at  timestamptz
);

-- What happened at the door: login_ok, login_fail, logout, signout_all, session_minted,
-- recovery_used, bulk_stop, step_up, step_up_fail.
create table web_event (
    id      bigserial primary key,
    at      timestamptz not null default now(),
    kind    text not null,
    origin  text not null default '',
    detail  jsonb not null default '{}'::jsonb
);
create index web_event_kind_at_idx on web_event (kind, at desc);

-- Small facts the door keeps: the last authenticator time step used (a code works once).
create table web_state (
    key    text primary key,
    value  text not null
);
