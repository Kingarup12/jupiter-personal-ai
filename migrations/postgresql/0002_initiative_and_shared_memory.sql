-- Durable production schema for proactive assistance and explicit sharing.
-- Private per-user memory content remains outside these cross-user tables.

CREATE TABLE IF NOT EXISTS proactive_settings (
    user_id TEXT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    reminders BOOLEAN NOT NULL DEFAULT TRUE,
    followups BOOLEAN NOT NULL DEFAULT TRUE,
    weather_warnings BOOLEAN NOT NULL DEFAULT FALSE,
    directed_messages BOOLEAN NOT NULL DEFAULT TRUE,
    memory_followups BOOLEAN NOT NULL DEFAULT TRUE,
    allow_nonurgent_interruptions BOOLEAN NOT NULL DEFAULT FALSE,
    quiet_start TIME NOT NULL DEFAULT TIME '22:00',
    quiet_end TIME NOT NULL DEFAULT TIME '07:00',
    timezone TEXT NOT NULL DEFAULT 'Asia/Calcutta',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS initiative_events (
    event_id UUID PRIMARY KEY,
    user_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    priority TEXT NOT NULL CHECK (priority IN ('urgent','important','normal','low')),
    interruption_policy TEXT NOT NULL DEFAULT 'natural_boundary',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending',
    snoozed_until TIMESTAMPTZ,
    source_chat_id TEXT,
    source_message_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, event_type, source_message_id)
);
CREATE INDEX IF NOT EXISTS initiative_events_due_idx
    ON initiative_events (user_id, status, due_at);

CREATE TABLE IF NOT EXISTS directed_messages (
    message_id UUID PRIMARY KEY,
    sender_user_id TEXT NOT NULL,
    recipient_user_id TEXT NOT NULL,
    sender_display_name TEXT NOT NULL,
    message_body TEXT NOT NULL,
    deliver_after TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    priority TEXT NOT NULL CHECK (priority IN ('urgent','important','normal','low')),
    status TEXT NOT NULL DEFAULT 'pending',
    source_message_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (sender_user_id, source_message_id)
);
CREATE INDEX IF NOT EXISTS directed_messages_delivery_idx
    ON directed_messages (recipient_user_id, status, deliver_after);

CREATE TABLE IF NOT EXISTS shared_people_facts (
    fact_id UUID PRIMARY KEY,
    source_user_id TEXT NOT NULL,
    subject_label TEXT NOT NULL,
    fact_text TEXT NOT NULL,
    visibility_scope TEXT NOT NULL CHECK (
        visibility_scope IN ('PRIVATE','SHARE_WITH_USERS','SHARED_PROFILE')
    ),
    allowed_user_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    allow_source_attribution BOOLEAN NOT NULL DEFAULT FALSE,
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    sensitivity TEXT NOT NULL DEFAULT 'normal',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS shared_people_facts_source_idx
    ON shared_people_facts (source_user_id, visibility_scope);

