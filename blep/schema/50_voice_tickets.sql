-- VoiceMaster, Tickets, Webhooks and embed tools (pagination).
-- Every schema file runs on boot, so statements must be idempotent.

-- --------------------------------------------------------------------------
-- VoiceMaster
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS voicemaster_config (
    guild_id         BIGINT PRIMARY KEY,
    category_id      BIGINT,            -- category created by `voicemaster setup`
    channel_id       BIGINT,            -- the Join to Create voice channel
    interface_id     BIGINT,            -- the #interface text channel
    panel_message_id BIGINT,            -- the interface panel message
    redirect_id      BIGINT,            -- `voicemaster category`: where new channels go
    default_name     TEXT,
    default_bitrate  INTEGER,           -- kbps
    default_region   TEXT,
    join_role_id     BIGINT
);

CREATE TABLE IF NOT EXISTS voicemaster_channels (
    channel_id BIGINT PRIMARY KEY,
    guild_id   BIGINT      NOT NULL,
    owner_id   BIGINT      NOT NULL,
    role_id    BIGINT,                  -- `voicemaster role`: given to members inside
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS voicemaster_channels_guild_idx ON voicemaster_channels (guild_id);

-- --------------------------------------------------------------------------
-- Tickets
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tickets_staff (
    guild_id BIGINT NOT NULL,
    role_id  BIGINT NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS tickets_blacklist (
    guild_id   BIGINT      NOT NULL,
    target_id  BIGINT      NOT NULL,
    kind       TEXT        NOT NULL CHECK (kind IN ('member', 'role')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, target_id)
);

CREATE TABLE IF NOT EXISTS tickets_forms (
    id         SERIAL PRIMARY KEY,
    guild_id   BIGINT      NOT NULL,
    name       TEXT        NOT NULL,
    title      TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS tickets_forms_name_idx ON tickets_forms (guild_id, LOWER(name));

CREATE TABLE IF NOT EXISTS tickets_form_fields (
    id          SERIAL PRIMARY KEY,
    form_id     INTEGER NOT NULL REFERENCES tickets_forms (id) ON DELETE CASCADE,
    key         TEXT    NOT NULL,
    label       TEXT    NOT NULL,
    style       TEXT    NOT NULL DEFAULT 'short' CHECK (style IN ('short', 'paragraph')),
    required    BOOLEAN NOT NULL DEFAULT TRUE,
    placeholder TEXT,
    min_length  INTEGER,
    max_length  INTEGER,
    position    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tickets_form_fields_form_idx ON tickets_form_fields (form_id);

CREATE TABLE IF NOT EXISTS tickets_panels (
    id             SERIAL PRIMARY KEY,
    guild_id       BIGINT      NOT NULL,
    name           TEXT        NOT NULL,
    channel_id     BIGINT      NOT NULL,
    message_id     BIGINT,
    mode           TEXT        NOT NULL DEFAULT 'buttons' CHECK (mode IN ('buttons', 'dropdown')),
    message        TEXT,
    category_id    BIGINT,
    log_channel_id BIGINT,
    max_open       INTEGER     NOT NULL DEFAULT 1,
    naming         TEXT        NOT NULL DEFAULT 'ticket-{case}',
    case_padding   INTEGER     NOT NULL DEFAULT 4,
    next_case      INTEGER     NOT NULL DEFAULT 1,
    placeholder    TEXT,
    claims         BOOLEAN     NOT NULL DEFAULT TRUE,
    delete_delay   INTEGER     NOT NULL DEFAULT 5,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS tickets_panels_name_idx ON tickets_panels (guild_id, LOWER(name));

CREATE TABLE IF NOT EXISTS tickets_options (
    id             SERIAL PRIMARY KEY,
    panel_id       INTEGER  NOT NULL REFERENCES tickets_panels (id) ON DELETE CASCADE,
    guild_id       BIGINT   NOT NULL,
    label          TEXT     NOT NULL,
    emoji          TEXT,
    description    TEXT,
    style          TEXT     NOT NULL DEFAULT 'grey',
    category_id    BIGINT,
    naming         TEXT,
    greeting       TEXT,
    close_message  TEXT,
    support_roles  BIGINT[] NOT NULL DEFAULT '{}',
    required_roles BIGINT[] NOT NULL DEFAULT '{}',
    require_all    BOOLEAN  NOT NULL DEFAULT FALSE,
    form_id        INTEGER  REFERENCES tickets_forms (id) ON DELETE SET NULL,
    creator_close  BOOLEAN  NOT NULL DEFAULT TRUE,
    keep_staff     BOOLEAN  NOT NULL DEFAULT FALSE,
    disabled       BOOLEAN  NOT NULL DEFAULT FALSE,
    position       INTEGER  NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS tickets_options_label_idx ON tickets_options (panel_id, LOWER(label));

-- One row per ticket ever opened. Panel/option names are snapshotted so logs and
-- stats keep working after a panel or option is deleted.
CREATE TABLE IF NOT EXISTS tickets_entries (
    id                 SERIAL PRIMARY KEY,
    guild_id           BIGINT      NOT NULL,
    channel_id         BIGINT      NOT NULL,
    panel_id           INTEGER     REFERENCES tickets_panels (id) ON DELETE SET NULL,
    option_id          INTEGER     REFERENCES tickets_options (id) ON DELETE SET NULL,
    panel_name         TEXT,
    option_label       TEXT,
    case_id            INTEGER     NOT NULL,
    status             TEXT        NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed', 'deleted')),
    creator_id         BIGINT      NOT NULL,
    claimer_id         BIGINT,
    closer_id          BIGINT,
    reopener_id        BIGINT,
    deleter_id         BIGINT,
    claim_reason       TEXT,
    close_reason       TEXT,
    reopen_reason      TEXT,
    delete_reason      TEXT,
    opened_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_at         TIMESTAMPTZ,
    closed_at          TIMESTAMPTZ,
    reopened_at        TIMESTAMPTZ,
    deleted_at         TIMESTAMPTZ,
    allowed            BIGINT[]    NOT NULL DEFAULT '{}',
    control_message_id BIGINT,
    closed_message_id  BIGINT,
    form_id            INTEGER,
    form_name          TEXT,
    form_title         TEXT,
    form_answers       JSONB,
    form_submitted_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS tickets_entries_channel_idx ON tickets_entries (channel_id);
CREATE INDEX IF NOT EXISTS tickets_entries_status_idx ON tickets_entries (guild_id, status);
CREATE INDEX IF NOT EXISTS tickets_entries_creator_idx ON tickets_entries (panel_id, creator_id, status);

-- --------------------------------------------------------------------------
-- Webhooks
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS webhooks_hooks (
    guild_id   BIGINT      NOT NULL,
    identifier TEXT        NOT NULL,
    webhook_id BIGINT      NOT NULL,
    token      TEXT        NOT NULL,
    channel_id BIGINT      NOT NULL,
    name       TEXT        NOT NULL,
    author_id  BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, identifier)
);
CREATE UNIQUE INDEX IF NOT EXISTS webhooks_hooks_webhook_idx ON webhooks_hooks (webhook_id);

-- --------------------------------------------------------------------------
-- Embed tools: reaction paginated messages
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS embeds_paginations (
    message_id   BIGINT PRIMARY KEY,
    guild_id     BIGINT      NOT NULL,
    channel_id   BIGINT      NOT NULL,
    author_id    BIGINT,
    pages        TEXT[]      NOT NULL DEFAULT '{}',
    current_page INTEGER     NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS embeds_paginations_guild_idx ON embeds_paginations (guild_id);
