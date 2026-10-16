-- Server automation: system messages, timers, sticky messages, autoresponders, reaction
-- triggers, autoroles, image-only channels, logging and vanity roles.
-- Every schema file runs on boot, so statements must be idempotent.

-- --------------------------------------------------------------------------- messages
-- welcome / goodbye / boost messages (one per channel per kind).
CREATE TABLE IF NOT EXISTS messages_system (
    guild_id      BIGINT  NOT NULL,
    kind          TEXT    NOT NULL CHECK (kind IN ('welcome', 'goodbye', 'boost')),
    channel_id    BIGINT  NOT NULL,
    template      TEXT    NOT NULL,
    self_destruct INTEGER,
    PRIMARY KEY (guild_id, kind, channel_id)
);

-- How many times Blep has seen a member boost (exposed as {boost.count}).
CREATE TABLE IF NOT EXISTS messages_boosts (
    guild_id BIGINT  NOT NULL,
    user_id  BIGINT  NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

-- Auto messages (`timer`), one per channel.
CREATE TABLE IF NOT EXISTS messages_timers (
    channel_id BIGINT PRIMARY KEY,
    guild_id   BIGINT      NOT NULL,
    template   TEXT        NOT NULL,
    seconds    INTEGER     NOT NULL,
    next_run   TIMESTAMPTZ NOT NULL,
    created_by BIGINT
);
CREATE INDEX IF NOT EXISTS messages_timers_guild_idx ON messages_timers (guild_id);

-- Sticky messages, one per channel.
CREATE TABLE IF NOT EXISTS messages_sticky (
    channel_id      BIGINT PRIMARY KEY,
    guild_id        BIGINT NOT NULL,
    template        TEXT   NOT NULL,
    last_message_id BIGINT
);
CREATE INDEX IF NOT EXISTS messages_sticky_guild_idx ON messages_sticky (guild_id);

-- --------------------------------------------------------------------------- autoresponder
CREATE TABLE IF NOT EXISTS autoresponder_triggers (
    guild_id             BIGINT      NOT NULL,
    trigger              TEXT        NOT NULL,
    response             TEXT        NOT NULL,
    strict               BOOLEAN     NOT NULL DEFAULT TRUE,
    self_destruct        INTEGER,
    delete_trigger       BOOLEAN     NOT NULL DEFAULT FALSE,
    reply                BOOLEAN     NOT NULL DEFAULT FALSE,
    ignore_command_check BOOLEAN     NOT NULL DEFAULT FALSE,
    exclusive_channels   BIGINT[]    NOT NULL DEFAULT '{}',
    exclusive_roles      BIGINT[]    NOT NULL DEFAULT '{}',
    roles_add            BIGINT[]    NOT NULL DEFAULT '{}',
    roles_remove         BIGINT[]    NOT NULL DEFAULT '{}',
    created_by           BIGINT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, trigger)
);

-- --------------------------------------------------------------------------- reactions
CREATE TABLE IF NOT EXISTS reactions_triggers (
    guild_id   BIGINT      NOT NULL,
    trigger    TEXT        NOT NULL,
    emoji      TEXT        NOT NULL,
    owner_id   BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, trigger, emoji)
);

-- `reaction messages`: react to every message in a channel.
CREATE TABLE IF NOT EXISTS reactions_channels (
    channel_id BIGINT PRIMARY KEY,
    guild_id   BIGINT NOT NULL,
    emojis     TEXT[] NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS reactions_channels_guild_idx ON reactions_channels (guild_id);

-- --------------------------------------------------------------------------- autorole
CREATE TABLE IF NOT EXISTS autorole_roles (
    guild_id BIGINT NOT NULL,
    role_id  BIGINT NOT NULL,
    target   TEXT   NOT NULL DEFAULT 'all' CHECK (target IN ('all', 'humans', 'bots')),
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS autorole_imageonly (
    channel_id BIGINT PRIMARY KEY,
    guild_id   BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS autorole_imageonly_guild_idx ON autorole_imageonly (guild_id);

-- --------------------------------------------------------------------------- logging
-- Which channel receives each event group (messages, members, roles ...).
CREATE TABLE IF NOT EXISTS logs_events (
    guild_id   BIGINT NOT NULL,
    event      TEXT   NOT NULL,
    channel_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, event)
);

-- Webhooks Blep created in log channels.
CREATE TABLE IF NOT EXISTS logs_webhooks (
    channel_id BIGINT PRIMARY KEY,
    guild_id   BIGINT NOT NULL,
    webhook_id BIGINT NOT NULL,
    token      TEXT   NOT NULL
);

CREATE TABLE IF NOT EXISTS logs_ignored (
    guild_id  BIGINT NOT NULL,
    target_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, target_id)
);

-- --------------------------------------------------------------------------- vanity
CREATE TABLE IF NOT EXISTS vanity_config (
    guild_id   BIGINT PRIMARY KEY,
    substring  TEXT,
    message    TEXT,
    channel_id BIGINT,
    role_ids   BIGINT[] NOT NULL DEFAULT '{}'
);

-- Members currently rewarded (so the award message is only sent once).
CREATE TABLE IF NOT EXISTS vanity_awarded (
    guild_id   BIGINT      NOT NULL,
    user_id    BIGINT      NOT NULL,
    awarded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id)
);
