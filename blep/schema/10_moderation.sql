-- Moderation module: hardbans, tempbans, forced nicknames, lockdown state and role restore.
-- Cases, warnings, jail and invoke messages live in the core tables (00_core.sql).

-- Users that are banned again whenever they're unbanned or rejoin (`hardban`).
CREATE TABLE IF NOT EXISTS moderation_hardbans (
    guild_id     BIGINT      NOT NULL,
    user_id      BIGINT      NOT NULL,
    moderator_id BIGINT      NOT NULL,
    reason       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id)
);

-- Scheduled unbans (`tempban`), lifted by a background loop so they survive restarts.
CREATE TABLE IF NOT EXISTS moderation_tempbans (
    guild_id     BIGINT      NOT NULL,
    user_id      BIGINT      NOT NULL,
    moderator_id BIGINT      NOT NULL,
    reason       TEXT,
    expires_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS moderation_tempbans_expires_idx ON moderation_tempbans (expires_at);

-- Nicknames kept enforced by the bot (`forcenick`).
CREATE TABLE IF NOT EXISTS moderation_forcenick (
    guild_id     BIGINT      NOT NULL,
    user_id      BIGINT      NOT NULL,
    nickname     TEXT        NOT NULL,
    moderator_id BIGINT      NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id)
);

-- Channels skipped by `lockdown`.
CREATE TABLE IF NOT EXISTS moderation_lockdown_ignore (
    guild_id   BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

-- Channels locked by `lock`/`lockdown`, so `unlockdown` only reopens what Blep closed.
CREATE TABLE IF NOT EXISTS moderation_locked_channels (
    guild_id   BIGINT      NOT NULL,
    channel_id BIGINT      NOT NULL,
    locked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, channel_id)
);

-- Roles a member had when they left (`role restore`). sticky_role_ids holds the jail/mute
-- roles they had, which are re-applied automatically when they rejoin.
CREATE TABLE IF NOT EXISTS moderation_role_restore (
    guild_id        BIGINT      NOT NULL,
    user_id         BIGINT      NOT NULL,
    role_ids        BIGINT[]    NOT NULL DEFAULT '{}',
    sticky_role_ids BIGINT[]    NOT NULL DEFAULT '{}',
    left_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id)
);
