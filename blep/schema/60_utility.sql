-- Utility module: AFK statuses, reminders, timezones and birthdays.
-- Every schema file runs on boot, so statements must be idempotent.

-- AFK is global: it follows the user across every server Blep is in.
CREATE TABLE IF NOT EXISTS utility_afk (
    user_id BIGINT      PRIMARY KEY,
    status  TEXT        NOT NULL,
    since   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS utility_reminders (
    id         SERIAL      PRIMARY KEY,
    user_id    BIGINT      NOT NULL,
    guild_id   BIGINT,
    channel_id BIGINT      NOT NULL,
    message_id BIGINT,
    content    TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    due_at     TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS utility_reminders_due_idx ON utility_reminders (due_at);
CREATE INDEX IF NOT EXISTS utility_reminders_user_idx ON utility_reminders (user_id);

CREATE TABLE IF NOT EXISTS utility_timezones (
    user_id  BIGINT PRIMARY KEY,
    timezone TEXT   NOT NULL
);

CREATE TABLE IF NOT EXISTS utility_birthdays (
    user_id BIGINT   PRIMARY KEY,
    month   SMALLINT NOT NULL CHECK (month BETWEEN 1 AND 12),
    day     SMALLINT NOT NULL CHECK (day BETWEEN 1 AND 31)
);
