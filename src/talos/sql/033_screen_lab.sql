-- The screen lab (talos.screenlab): samples of prompt injection and of ordinary text, from public sources and
-- Talos's own, to measure and train talos-doctor's screen (its deterministic rules, and Jev).
--
-- A source is a module (talos.screenlab.sources), switched on or off, with its own sample cap, pinned to the
-- revision it was imported at. A sample's text is kept here only: never in a repository, never shown raw to an
-- agent. Duplicates, exact or near, across all sources, point at the sample they repeat (dup_of) and are left
-- out of every measurement.

create table screen_source (
    id           text primary key check (id ~ '^[a-z0-9][a-z0-9-]{1,40}$'),
    title        text not null,
    license      text,
    url          text,
    enabled      boolean not null default false,
    cap          integer check (cap is null or cap > 0),
    revision     text,                         -- what was imported: a commit, a dataset revision, a file hash
    imported_at  timestamptz,
    counts       jsonb not null default '{}'   -- imported, attack, benign, duplicates
);

create table screen_sample (
    id          bigserial primary key,
    source_id   text not null references screen_source (id) on delete cascade,
    ext_id      text not null,                 -- the sample's id in its source
    label       text not null check (label in ('attack', 'benign')),
    category    text,                          -- the source's own category or family
    kind        text not null default 'text',  -- code, repo-file, email, prompt, tool-output, document
    path_hint   text,                          -- a file name the screen can judge by (README.md, ci.yml, …)
    text        text not null,
    text_key    text not null,                 -- sha256 of the normalized text, for exact duplicates
    bands       bigint[] not null default '{}',-- MinHash bands of its word shingles, for near duplicates
    dup_of      bigint references screen_sample (id) on delete set null,
    created_at  timestamptz not null default now(),
    unique (source_id, ext_id)
);
create index screen_sample_key_idx on screen_sample (text_key);
create index screen_sample_source_idx on screen_sample (source_id, label);

create table screen_run (
    id           text primary key,
    engine       text not null check (engine in ('rules', 'jev')),
    version      text not null,                -- the rules file's hash, or Jev's question version
    params       jsonb not null default '{}',
    started_at   timestamptz not null default now(),
    finished_at  timestamptz,
    samples      integer not null default 0,
    cost_usd     numeric(10, 4) not null default 0
);

create table screen_result (
    run_id     text not null references screen_run (id) on delete cascade,
    sample_id  bigint not null references screen_sample (id) on delete cascade,
    verdict    text not null check (verdict in ('block', 'review', 'clean', 'error')),
    fired      text[] not null default '{}',   -- the rules that fired (rules), or the questions above 0.5 (jev)
    scores     jsonb not null default '{}',    -- jev: each question's probability of yes
    primary key (run_id, sample_id)
);
create index screen_result_sample_idx on screen_result (sample_id);
