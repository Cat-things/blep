-- Engagement & roles: reaction/button roles, booster roles, starboard/clownboard, levels,
-- bump reminder, counters and giveaways. Runs on every boot, keep it idempotent.

-- --------------------------------------------------------------------------- reaction roles
CREATE TABLE IF NOT EXISTS reactionroles (
    guild_id   BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    emoji_key  TEXT   NOT NULL,   -- custom emoji ID or the unicode emoji without variation selectors
    emoji      TEXT   NOT NULL,   -- the emoji as it should be displayed / reacted with
    role_id    BIGINT NOT NULL,
    PRIMARY KEY (message_id, emoji_key)
);
CREATE INDEX IF NOT EXISTS reactionroles_guild_idx ON reactionroles (guild_id);

-- --------------------------------------------------------------------------- button roles
CREATE TABLE IF NOT EXISTS buttonroles (
    guild_id   BIGINT      NOT NULL,
    channel_id BIGINT      NOT NULL,
    message_id BIGINT      NOT NULL,
    role_id    BIGINT      NOT NULL,
    style      TEXT        NOT NULL DEFAULT 'gray',
    emoji      TEXT,
    label      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (message_id, role_id)
);
CREATE INDEX IF NOT EXISTS buttonroles_guild_idx ON buttonroles (guild_id);

-- --------------------------------------------------------------------------- booster roles
CREATE TABLE IF NOT EXISTS boosterroles_config (
    guild_id      BIGINT PRIMARY KEY,
    base_role_id  BIGINT,
    award_role_id BIGINT,
    role_limit    INTEGER
);

CREATE TABLE IF NOT EXISTS boosterroles (
    guild_id   BIGINT      NOT NULL,
    user_id    BIGINT      NOT NULL,
    role_id    BIGINT      NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id)
);

-- --------------------------------------------------------------------------- starboard / clownboard
CREATE TABLE IF NOT EXISTS starboard_config (
    guild_id         BIGINT  NOT NULL,
    board            TEXT    NOT NULL CHECK (board IN ('starboard', 'clownboard')),
    channel_id       BIGINT,
    locked           BOOLEAN NOT NULL DEFAULT TRUE,
    threshold        INTEGER NOT NULL DEFAULT 3,
    emoji            TEXT,
    self_star        BOOLEAN NOT NULL DEFAULT FALSE,
    color            INTEGER,
    show_timestamp   BOOLEAN NOT NULL DEFAULT TRUE,
    show_jump_url    BOOLEAN NOT NULL DEFAULT TRUE,
    show_attachments BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (guild_id, board)
);

CREATE TABLE IF NOT EXISTS starboard_ignored (
    guild_id  BIGINT NOT NULL,
    board     TEXT   NOT NULL,
    target_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, board, target_id)
);

CREATE TABLE IF NOT EXISTS starboard_posts (
    guild_id        BIGINT  NOT NULL,
    board           TEXT    NOT NULL,
    channel_id      BIGINT  NOT NULL,   -- channel of the original message
    message_id      BIGINT  NOT NULL,   -- the original message
    post_channel_id BIGINT  NOT NULL,
    post_id         BIGINT  NOT NULL,   -- the repost in the board channel
    author_id       BIGINT,
    count           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, board, message_id)
);
CREATE INDEX IF NOT EXISTS starboard_posts_post_idx ON starboard_posts (post_id);

-- --------------------------------------------------------------------------- levels
CREATE TABLE IF NOT EXISTS levels_config (
    guild_id           BIGINT  PRIMARY KEY,
    enabled            BOOLEAN NOT NULL DEFAULT FALSE,
    stack_roles        BOOLEAN NOT NULL DEFAULT TRUE,
    message            TEXT,
    message_mode       TEXT    NOT NULL DEFAULT 'context' CHECK (message_mode IN ('context', 'pm', 'channel', 'none')),
    message_channel_id BIGINT,
    multiplier         REAL    NOT NULL DEFAULT 1.0,
    minimum_level      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS levels_ignored (
    guild_id  BIGINT NOT NULL,
    target_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, target_id)
);

CREATE TABLE IF NOT EXISTS levels_rewards (
    guild_id BIGINT  NOT NULL,
    role_id  BIGINT  NOT NULL,
    level    INTEGER NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS levels_members (
    guild_id BIGINT NOT NULL,
    user_id  BIGINT NOT NULL,
    xp       BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS levels_members_xp_idx ON levels_members (guild_id, xp DESC);

-- --------------------------------------------------------------------------- bump reminder
CREATE TABLE IF NOT EXISTS bumpreminder_config (
    guild_id       BIGINT  PRIMARY KEY,
    channel_id     BIGINT,
    message        TEXT,
    pingable       BOOLEAN NOT NULL DEFAULT FALSE,
    thankyou       TEXT,
    autolock       BOOLEAN NOT NULL DEFAULT FALSE,
    autoclean      BOOLEAN NOT NULL DEFAULT FALSE,
    last_bumper_id BIGINT,
    last_bump_at   TIMESTAMPTZ,
    next_bump_at   TIMESTAMPTZ,
    reminded       BOOLEAN NOT NULL DEFAULT TRUE
);

-- --------------------------------------------------------------------------- counters
CREATE TABLE IF NOT EXISTS counters (
    channel_id   BIGINT      PRIMARY KEY,
    guild_id     BIGINT      NOT NULL,
    option       TEXT        NOT NULL,
    template     TEXT        NOT NULL,
    updated_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS counters_guild_idx ON counters (guild_id);

-- --------------------------------------------------------------------------- giveaways
CREATE TABLE IF NOT EXISTS giveaways (
    message_id     BIGINT      PRIMARY KEY,
    guild_id       BIGINT      NOT NULL,
    channel_id     BIGINT      NOT NULL,
    host_id        BIGINT      NOT NULL,
    prize          TEXT        NOT NULL,
    description    TEXT,
    thumbnail      TEXT,
    image          TEXT,
    winner_count   INTEGER     NOT NULL DEFAULT 1,
    required_roles BIGINT[]    NOT NULL DEFAULT '{}',
    min_level      INTEGER,
    max_level      INTEGER,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ends_at        TIMESTAMPTZ NOT NULL,
    ended          BOOLEAN     NOT NULL DEFAULT FALSE,
    winners        BIGINT[]    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS giveaways_guild_idx ON giveaways (guild_id, ended);

CREATE TABLE IF NOT EXISTS giveaway_entries (
    message_id BIGINT      NOT NULL,
    user_id    BIGINT      NOT NULL,
    entered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (message_id, user_id)
);
