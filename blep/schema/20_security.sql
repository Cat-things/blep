-- Security modules: antinuke, antiraid (join gate), honeypot and the automod filter.
-- Every schema file runs on boot, so statements must be idempotent.

-- ------------------------------------------------------------------ antinuke
-- One row per configured module (ban, kick, role, channel, emoji, webhook, vanity, botadd).
CREATE TABLE IF NOT EXISTS antinuke_modules (
    guild_id   BIGINT  NOT NULL,
    module     TEXT    NOT NULL,
    enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    threshold  INTEGER NOT NULL DEFAULT 3,
    punishment TEXT    NOT NULL DEFAULT 'ban',
    command    BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (guild_id, module)
);

-- Users and bots that bypass the antinuke entirely.
CREATE TABLE IF NOT EXISTS antinuke_whitelist (
    guild_id BIGINT NOT NULL,
    user_id  BIGINT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- Users (besides the owner) allowed to configure the antinuke.
CREATE TABLE IF NOT EXISTS antinuke_admins (
    guild_id BIGINT NOT NULL,
    user_id  BIGINT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- Dangerous permissions that may not be granted by non-whitelisted members.
CREATE TABLE IF NOT EXISTS antinuke_permissions (
    guild_id   BIGINT NOT NULL,
    permission TEXT   NOT NULL,
    punishment TEXT   NOT NULL DEFAULT 'ban',
    PRIMARY KEY (guild_id, permission)
);

-- ------------------------------------------------------------------ antiraid
CREATE TABLE IF NOT EXISTS antiraid_config (
    guild_id           BIGINT PRIMARY KEY,
    massjoin           BOOLEAN NOT NULL DEFAULT FALSE,
    massjoin_threshold INTEGER NOT NULL DEFAULT 10,
    massjoin_action    TEXT    NOT NULL DEFAULT 'ban',
    massjoin_lock      BOOLEAN NOT NULL DEFAULT FALSE,
    massjoin_punish    BOOLEAN NOT NULL DEFAULT TRUE,
    avatar             BOOLEAN NOT NULL DEFAULT FALSE,
    avatar_action      TEXT    NOT NULL DEFAULT 'kick',
    age                BOOLEAN NOT NULL DEFAULT FALSE,
    age_threshold      INTEGER NOT NULL DEFAULT 7,
    age_action         TEXT    NOT NULL DEFAULT 'kick',
    raid_active        BOOLEAN NOT NULL DEFAULT FALSE,
    raid_started_at    TIMESTAMPTZ,
    -- channel_id -> previous @everyone send_messages overwrite (null / true), restored by `antiraid state`
    locked_channels    JSONB   NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS antiraid_whitelist (
    guild_id BIGINT NOT NULL,
    user_id  BIGINT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- ------------------------------------------------------------------ honeypot
CREATE TABLE IF NOT EXISTS honeypot_channels (
    channel_id BIGINT PRIMARY KEY,
    guild_id   BIGINT NOT NULL,
    punishment TEXT   NOT NULL DEFAULT 'ban'
);
CREATE INDEX IF NOT EXISTS honeypot_channels_guild_idx ON honeypot_channels (guild_id);

-- ------------------------------------------------------------------ automod (filter)
-- One row per filter: words, invites, links, spam, caps, massmention, emojis.
CREATE TABLE IF NOT EXISTS automod_filters (
    guild_id  BIGINT  NOT NULL,
    filter    TEXT    NOT NULL,
    enabled   BOOLEAN NOT NULL DEFAULT TRUE,
    threshold INTEGER,
    action    TEXT    NOT NULL DEFAULT 'delete',
    PRIMARY KEY (guild_id, filter)
);

CREATE TABLE IF NOT EXISTS automod_words (
    guild_id BIGINT NOT NULL,
    word     TEXT   NOT NULL,
    PRIMARY KEY (guild_id, word)
);

-- Members, roles, channels and categories the filter ignores.
CREATE TABLE IF NOT EXISTS automod_exempt (
    guild_id  BIGINT NOT NULL,
    target_id BIGINT NOT NULL,
    kind      TEXT   NOT NULL,
    PRIMARY KEY (guild_id, target_id)
);

CREATE TABLE IF NOT EXISTS automod_link_whitelist (
    guild_id BIGINT NOT NULL,
    domain   TEXT   NOT NULL,
    PRIMARY KEY (guild_id, domain)
);
