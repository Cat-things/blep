-- Music & Spotify.
-- The DJ role and autoplay toggle live in guild_settings (keys "dj_role" / "music_autoplay", Config module);
-- the music player itself keeps no persistent state.

-- Spotify accounts connected with `spotify login` / `spotify code` (Authorization Code flow).
CREATE TABLE IF NOT EXISTS spotify_accounts (
    user_id       BIGINT      PRIMARY KEY,
    access_token  TEXT        NOT NULL,
    refresh_token TEXT        NOT NULL,
    expires_at    TIMESTAMPTZ NOT NULL,
    scope         TEXT,
    spotify_id    TEXT,
    display_name  TEXT,
    product       TEXT,
    linked_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
