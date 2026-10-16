-- Core tables shared by the framework (prefixes, aliases, permissions, cases...).
-- Every schema file runs on boot, so statements must be idempotent.

CREATE TABLE IF NOT EXISTS prefixes (
    guild_id BIGINT PRIMARY KEY,
    prefix   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS self_prefixes (
    user_id BIGINT PRIMARY KEY,
    prefix  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blacklist (
    target_id  BIGINT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('user', 'guild')),
    reason     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS aliases (
    guild_id BIGINT NOT NULL,
    alias    TEXT   NOT NULL,
    command  TEXT   NOT NULL,
    PRIMARY KEY (guild_id, alias)
);

CREATE TABLE IF NOT EXISTS disabled_commands (
    guild_id BIGINT NOT NULL,
    command  TEXT   NOT NULL,
    PRIMARY KEY (guild_id, command)
);

CREATE TABLE IF NOT EXISTS disabled_modules (
    guild_id BIGINT NOT NULL,
    module   TEXT   NOT NULL,
    PRIMARY KEY (guild_id, module)
);

CREATE TABLE IF NOT EXISTS ignored (
    guild_id  BIGINT NOT NULL,
    target_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, target_id)
);

CREATE TABLE IF NOT EXISTS command_restrictions (
    guild_id BIGINT NOT NULL,
    command  TEXT   NOT NULL,
    role_id  BIGINT NOT NULL,
    PRIMARY KEY (guild_id, command, role_id)
);

CREATE TABLE IF NOT EXISTS fake_permissions (
    guild_id   BIGINT NOT NULL,
    role_id    BIGINT NOT NULL,
    permission TEXT   NOT NULL,
    PRIMARY KEY (guild_id, role_id, permission)
);

CREATE TABLE IF NOT EXISTS staff_roles (
    guild_id BIGINT NOT NULL,
    role_id  BIGINT NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

-- Moderation primitives used by the moderation, security and automod modules.
CREATE TABLE IF NOT EXISTS moderation_config (
    guild_id               BIGINT PRIMARY KEY,
    modlog_channel_id      BIGINT,
    jail_role_id           BIGINT,
    jail_channel_id        BIGINT,
    mute_role_id           BIGINT,
    image_mute_role_id     BIGINT,
    reaction_mute_role_id  BIGINT
);

CREATE TABLE IF NOT EXISTS cases (
    guild_id     BIGINT      NOT NULL,
    case_id      INTEGER     NOT NULL,
    user_id      BIGINT      NOT NULL,
    moderator_id BIGINT      NOT NULL,
    action       TEXT        NOT NULL,
    reason       TEXT,
    duration     INTEGER,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, case_id)
);
CREATE INDEX IF NOT EXISTS cases_user_idx ON cases (guild_id, user_id);
CREATE INDEX IF NOT EXISTS cases_moderator_idx ON cases (guild_id, moderator_id);

CREATE TABLE IF NOT EXISTS jailed (
    guild_id  BIGINT      NOT NULL,
    user_id   BIGINT      NOT NULL,
    role_ids  BIGINT[]    NOT NULL DEFAULT '{}',
    jailed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS warnings (
    id           SERIAL PRIMARY KEY,
    guild_id     BIGINT      NOT NULL,
    user_id      BIGINT      NOT NULL,
    moderator_id BIGINT      NOT NULL,
    reason       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS warnings_user_idx ON warnings (guild_id, user_id);

-- Custom responses/DMs for moderation commands (`invoke ban message ...`).
CREATE TABLE IF NOT EXISTS invoke_messages (
    guild_id BIGINT NOT NULL,
    command  TEXT   NOT NULL,
    kind     TEXT   NOT NULL CHECK (kind IN ('message', 'dm')),
    template TEXT   NOT NULL,
    PRIMARY KEY (guild_id, command, kind)
);

-- Generic per-guild key/value settings (`settings dj`, `settings autoplay` ...).
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id BIGINT NOT NULL,
    key      TEXT   NOT NULL,
    value    JSONB  NOT NULL,
    PRIMARY KEY (guild_id, key)
);
