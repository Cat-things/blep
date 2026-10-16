-- Integrations: Last.fm, social notifications, Fortnite and Roblox.
-- Every schema file runs on boot, so statements must be idempotent.

-- --------------------------------------------------------------------------- Last.fm
-- One linked Last.fm account per Discord user, plus their personal nowplaying layout
-- (`lastfm mode`) and custom reactions (`lastfm cr`).
CREATE TABLE IF NOT EXISTS lastfm_users (
    user_id    BIGINT PRIMARY KEY,
    username   TEXT        NOT NULL,
    mode       TEXT,
    upvote     TEXT,
    downvote   TEXT,
    linked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS lastfm_users_username_idx ON lastfm_users (LOWER(username));

-- The member with the most plays for an artist in a server (claimed through `whoknows`).
CREATE TABLE IF NOT EXISTS lastfm_crowns (
    guild_id    BIGINT      NOT NULL,
    artist_key  TEXT        NOT NULL,
    artist      TEXT        NOT NULL,
    user_id     BIGINT      NOT NULL,
    plays       INTEGER     NOT NULL DEFAULT 0,
    claimed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, artist_key)
);
CREATE INDEX IF NOT EXISTS lastfm_crowns_user_idx ON lastfm_crowns (guild_id, user_id);

-- --------------------------------------------------------------------------- Social notifications
-- platform: youtube, twitch, reddit or kick. account_id is the YouTube channel ID, the
-- lowercase Twitch login / Kick slug or the lowercase subreddit name.
CREATE TABLE IF NOT EXISTS social_feeds (
    id           SERIAL PRIMARY KEY,
    guild_id     BIGINT      NOT NULL,
    channel_id   BIGINT      NOT NULL,
    platform     TEXT        NOT NULL,
    account_id   TEXT        NOT NULL,
    account_name TEXT        NOT NULL,
    message      TEXT,
    pingable     BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (guild_id, channel_id, platform, account_id)
);
CREATE INDEX IF NOT EXISTS social_feeds_account_idx ON social_feeds (platform, account_id);
CREATE INDEX IF NOT EXISTS social_feeds_guild_idx ON social_feeds (guild_id, platform);

-- Poller state shared by every server following the same account: the most recent
-- post/video IDs that were already announced, and the stream that's currently live.
CREATE TABLE IF NOT EXISTS social_state (
    platform   TEXT        NOT NULL,
    account_id TEXT        NOT NULL,
    seen       TEXT[]      NOT NULL DEFAULT '{}',
    live_id    TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (platform, account_id)
);

-- --------------------------------------------------------------------------- Fortnite
CREATE TABLE IF NOT EXISTS fortnite_shop_config (
    guild_id   BIGINT PRIMARY KEY,
    channel_id BIGINT,
    role_id    BIGINT,
    voting     BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS fortnite_watch (
    user_id       BIGINT      NOT NULL,
    cosmetic_id   TEXT        NOT NULL,
    cosmetic_name TEXT        NOT NULL,
    last_notified DATE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, cosmetic_id)
);
CREATE INDEX IF NOT EXISTS fortnite_watch_cosmetic_idx ON fortnite_watch (cosmetic_id);

-- Single-row key/value state for the shop poller (last shop signature).
CREATE TABLE IF NOT EXISTS fortnite_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
