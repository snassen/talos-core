-- Composing and sending mail (talos.compose, talos.send; docs in CLAUDE.md, promise 1).
--
-- Talos sends a mail only when the owner presses Send on a mail they composed, after confirming
-- the sending account. These tables hold the drafts, the signatures, the send settings and a
-- log of every send attempt. The log never holds a body.

-- A draft, saved while the owner types. Local to Talos: it never reaches a mail server as a draft.
create table draft (
    id                   bigserial primary key,
    account_id           text references account (id) on delete set null,
    mode                 text not null default 'new' check (mode in ('new', 'reply', 'reply_all', 'forward')),
    original_id          bigint references message (id) on delete set null,  -- what a reply or forward answers
    original_account_id  text,                         -- the account the original was received on
    to_addrs             text[] not null default '{}',
    cc_addrs             text[] not null default '{}',
    bcc_addrs            text[] not null default '{}',
    subject              text not null default '',
    body                 text not null default '',     -- what the owner wrote, plain text
    quoted               text not null default '',     -- the quoted original (reply) or forwarded text
    include_quote        boolean not null default true,
    signature_id         bigint,                       -- null: no signature
    in_reply_to          text,
    references_ids       text[] not null default '{}',
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now()
);
create index draft_updated_idx on draft (updated_at desc);

create table signature (
    id          bigserial primary key,
    name        text not null,
    body_text   text not null default '',
    body_html   text,                                  -- optional simple HTML, cleaned on save
    accounts    text[] not null default '{}',          -- the account ids it applies to
    for_new     boolean not null default true,         -- used for new mail
    for_reply   boolean not null default true,         -- used for replies and forwards
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

alter table draft add constraint draft_signature_fk
    foreign key (signature_id) references signature (id) on delete set null;

-- One default signature per account per kind of mail.
create table signature_default (
    account_id    text not null references account (id) on delete cascade,
    mode          text not null check (mode in ('new', 'reply')),
    signature_id  bigint not null references signature (id) on delete cascade,
    primary key (account_id, mode)
);

-- Send settings: 'default_account' (the account a new mail starts from), 'from_name:<account>'.
create table send_setting (
    key         text primary key,
    value       jsonb not null,
    updated_at  timestamptz not null default now()
);

-- Every send attempt: who, when, from, to, subject, Message-ID and what happened. Never the body.
create table send_log (
    id           bigserial primary key,
    at           timestamptz not null default now(),
    by_whom      text not null default 'owner',
    account_id   text,
    from_addr    text,
    to_addrs     text[] not null default '{}',
    cc_addrs     text[] not null default '{}',
    bcc_addrs    text[] not null default '{}',
    subject      text,
    message_id   text,
    in_reply_to  text,
    draft_id     bigint,
    result       text not null check (result in ('sent', 'failed', 'refused')),
    detail       text
);
create index send_log_at_idx on send_log (at desc);
