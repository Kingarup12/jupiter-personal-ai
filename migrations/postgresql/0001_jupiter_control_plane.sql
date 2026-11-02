-- Jupiter multi-provider continuity architecture
-- Prepared only: do not execute automatically.
-- Additive and restart-safe. No DROP/TRUNCATE/DELETE statements are present.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id text PRIMARY KEY,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    applied_by text NOT NULL
);

CREATE TABLE IF NOT EXISTS user_accounts (
    user_id text PRIMARY KEY,
    external_account_id text UNIQUE,
    status text NOT NULL DEFAULT 'ACTIVE',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version bigint NOT NULL DEFAULT 1,
    CHECK (status IN ('ACTIVE','DISABLED','DELETED'))
);

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id text PRIMARY KEY REFERENCES user_accounts(user_id),
    profile_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    privacy_class text NOT NULL DEFAULT 'NORMAL_USER_DATA',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version bigint NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS personality_profiles (
    user_id text PRIMARY KEY REFERENCES user_accounts(user_id),
    language_style text,
    tone text,
    answer_length text,
    formality text,
    humour_level text,
    emoji_level text,
    explanation_style text,
    coding_style text,
    preferred_formats jsonb NOT NULL DEFAULT '[]'::jsonb,
    custom_instructions text,
    auto_update_enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version bigint NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS personality_revisions (
    revision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    profile_version bigint NOT NULL,
    changed_fields jsonb NOT NULL,
    source_message_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    explicit_user_instruction boolean NOT NULL DEFAULT false,
    supersedes_revision_id uuid REFERENCES personality_revisions(revision_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, profile_version)
);

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    title text,
    status text NOT NULL DEFAULT 'ACTIVE',
    no_persist_mode boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    version bigint NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_conversations_user_updated
    ON conversations(user_id, updated_at DESC) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS messages (
    message_id text PRIMARY KEY,
    conversation_id text NOT NULL REFERENCES conversations(conversation_id),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    role text NOT NULL,
    content text NOT NULL,
    model_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    privacy_class text NOT NULL DEFAULT 'NORMAL_USER_DATA',
    created_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    CHECK (role IN ('user','assistant','system','tool'))
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
    ON messages(user_id, conversation_id, created_at) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS conversation_summaries (
    summary_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id text NOT NULL REFERENCES conversations(conversation_id),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    summary text NOT NULL,
    through_message_id text,
    embedding vector(768),
    embedding_model text,
    embedding_version text,
    embedding_pending boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS conversation_topics (
    topic_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    conversation_id text REFERENCES conversations(conversation_id),
    title text NOT NULL,
    canonical_topic text NOT NULL,
    summary text,
    status text NOT NULL DEFAULT 'ACTIVE',
    linked_entities jsonb NOT NULL DEFAULT '[]'::jsonb,
    linked_memories jsonb NOT NULL DEFAULT '[]'::jsonb,
    source_message_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, conversation_id, canonical_topic)
);

CREATE TABLE IF NOT EXISTS memories (
    memory_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    tier text NOT NULL,
    memory_type text NOT NULL,
    canonical_fact text NOT NULL,
    canonical_key text NOT NULL,
    structured_data jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'ACTIVE',
    confidence double precision NOT NULL DEFAULT 0.5,
    source_type text NOT NULL,
    source_message_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    user_confirmed boolean NOT NULL DEFAULT false,
    user_pinned boolean NOT NULL DEFAULT false,
    inferred boolean NOT NULL DEFAULT false,
    valid_from timestamptz,
    valid_until timestamptz,
    expires_at timestamptz,
    next_review_at timestamptz,
    last_verified_at timestamptz,
    last_recalled_at timestamptz,
    recall_count bigint NOT NULL DEFAULT 0,
    last_mentioned_at timestamptz NOT NULL DEFAULT now(),
    embedding vector(768),
    embedding_model text,
    embedding_version text,
    embedding_pending boolean NOT NULL DEFAULT false,
    sensitivity text NOT NULL DEFAULT 'NORMAL_USER_DATA',
    visibility_scope text NOT NULL DEFAULT 'PRIVATE_TO_SOURCE_USER',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    version bigint NOT NULL DEFAULT 1,
    CHECK (tier IN ('P0','P1','P2','P3','P4')),
    CHECK (status IN ('ACTIVE','HISTORICAL','SUPERSEDED','DISPUTED','EXPIRED','PENDING_CONFIRMATION')),
    CHECK (confidence >= 0 AND confidence <= 1)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_memories_user_canonical_active
    ON memories(user_id, canonical_key) WHERE status='ACTIVE' AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_memories_user_tier_status
    ON memories(user_id, tier, status, updated_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_memories_search
    ON memories USING gin (to_tsvector('simple', canonical_fact));
CREATE INDEX IF NOT EXISTS idx_memories_embedding
    ON memories USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL;

CREATE TABLE IF NOT EXISTS memory_revisions (
    revision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id uuid NOT NULL REFERENCES memories(memory_id),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    prior_status text,
    new_status text NOT NULL,
    prior_value jsonb,
    new_value jsonb NOT NULL,
    correction_authority text NOT NULL,
    source_message_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memory_sources (
    memory_id uuid NOT NULL REFERENCES memories(memory_id),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    message_id text NOT NULL,
    source_type text NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (memory_id, message_id)
);

CREATE TABLE IF NOT EXISTS memory_review_queue (
    review_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    candidate jsonb NOT NULL,
    suggested_tier text,
    confidence double precision NOT NULL,
    reason text NOT NULL,
    possible_choices jsonb NOT NULL DEFAULT '[]'::jsonb,
    status text NOT NULL DEFAULT 'PENDING',
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    entity_type text NOT NULL,
    canonical_name text NOT NULL,
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    claim_status text NOT NULL DEFAULT 'SELF_REPORTED',
    current_relevance double precision NOT NULL DEFAULT 0.5,
    sensitivity text NOT NULL DEFAULT 'NORMAL_USER_DATA',
    visibility_scope text NOT NULL DEFAULT 'PRIVATE_TO_SOURCE_USER',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    version bigint NOT NULL DEFAULT 1,
    CHECK (entity_type IN ('PERSON','DEVICE','PROJECT','PLACE','ORGANIZATION','GAME','MOVIE','COURSE','SKILL','GOAL','EVENT','ASSET')),
    CHECK (claim_status IN ('SELF_REPORTED','PROVISIONAL','VERIFIED','DISPUTED','REJECTED'))
);
CREATE INDEX IF NOT EXISTS idx_entities_user_type_name
    ON entities(user_id, entity_type, canonical_name) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS entity_aliases (
    alias_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id uuid NOT NULL REFERENCES entities(entity_id),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    alias text NOT NULL,
    pronunciation text,
    confidence double precision NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, entity_id, alias)
);

CREATE TABLE IF NOT EXISTS entity_relationships (
    relationship_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    source_entity_id uuid NOT NULL REFERENCES entities(entity_id),
    target_entity_id uuid NOT NULL REFERENCES entities(entity_id),
    relationship_type text NOT NULL,
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    claim_status text NOT NULL DEFAULT 'SELF_REPORTED',
    visibility_scope text NOT NULL DEFAULT 'PRIVATE_TO_SOURCE_USER',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    UNIQUE (user_id, source_entity_id, target_entity_id, relationship_type)
);

CREATE TABLE IF NOT EXISTS encrypted_user_credentials (
    credential_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    provider text NOT NULL,
    credential_scope text NOT NULL,
    encrypted_secret bytea NOT NULL,
    encryption_version integer NOT NULL DEFAULT 1,
    status text NOT NULL DEFAULT 'UNVERIFIED',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_verified_at timestamptz,
    failure_count integer NOT NULL DEFAULT 0,
    last_failure text,
    disabled_reason text,
    deleted_at timestamptz,
    CHECK (status IN ('ACTIVE','UNVERIFIED','INVALID','DISABLED','EXPIRED','QUOTA_EXHAUSTED','AUTH_ERROR'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_credentials_user_scope_active
    ON encrypted_user_credentials(user_id, provider, credential_scope)
    WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS dynamic_model_registry (
    provider text NOT NULL,
    model_id text NOT NULL,
    display_name text NOT NULL,
    family text,
    credential_scope text NOT NULL,
    credential_owner_id text NOT NULL DEFAULT '*',
    available boolean NOT NULL DEFAULT false,
    free_eligible boolean NOT NULL DEFAULT false,
    free_verified boolean NOT NULL DEFAULT false,
    free_plan_available boolean NOT NULL DEFAULT false,
    reliability_class text NOT NULL DEFAULT 'CANDIDATE',
    routing_allowed boolean NOT NULL DEFAULT false,
    input_modalities jsonb NOT NULL DEFAULT '["text"]'::jsonb,
    output_modalities jsonb NOT NULL DEFAULT '["text"]'::jsonb,
    capabilities jsonb NOT NULL DEFAULT '[]'::jsonb,
    supports_text boolean NOT NULL DEFAULT false,
    supports_image boolean NOT NULL DEFAULT false,
    supports_audio boolean NOT NULL DEFAULT false,
    supports_video boolean NOT NULL DEFAULT false,
    supports_pdf boolean NOT NULL DEFAULT false,
    supports_embeddings boolean NOT NULL DEFAULT false,
    supports_live boolean NOT NULL DEFAULT false,
    supports_tts boolean NOT NULL DEFAULT false,
    supports_image_generation boolean NOT NULL DEFAULT false,
    supports_tools boolean NOT NULL DEFAULT false,
    supports_search boolean NOT NULL DEFAULT false,
    supports_reasoning boolean NOT NULL DEFAULT false,
    supports_structured_output boolean NOT NULL DEFAULT false,
    supports_coding boolean NOT NULL DEFAULT false,
    context_window integer,
    max_output integer,
    quality_scores jsonb NOT NULL DEFAULT '{}'::jsonb,
    latency_avg double precision,
    latency_p95 double precision,
    success_rate double precision,
    privacy_class text NOT NULL DEFAULT 'NORMAL_USER_DATA',
    last_seen timestamptz,
    last_checked timestamptz,
    last_success timestamptz,
    last_failure timestamptz,
    last_benchmarked timestamptz,
    deprecated_at timestamptz,
    shutdown_at timestamptz,
    status text NOT NULL DEFAULT 'CANDIDATE',
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    version bigint NOT NULL DEFAULT 1,
    PRIMARY KEY (provider, model_id, credential_scope, credential_owner_id),
    CHECK (status IN ('CANDIDATE','ACTIVE','COOLDOWN','QUOTA_EXHAUSTED','DEPRECATED','REMOVED','AUTH_FAILED','INCOMPATIBLE','UNKNOWN')),
    CHECK (NOT free_verified OR (free_eligible AND free_plan_available)),
    CHECK (reliability_class IN (
        'VERIFIED_HEALTHY','TEMPORARY_FAILURE','PROBATIONARY_LAST_RESORT',
        'HARD_BLOCKED','REMOVED_OR_INCOMPATIBLE','CANDIDATE'
    )),
    CHECK (NOT routing_allowed OR (available AND free_verified AND free_plan_available))
);
CREATE INDEX IF NOT EXISTS idx_models_routing
    ON dynamic_model_registry(
        status, free_verified, available, credential_scope, credential_owner_id
    );

CREATE TABLE IF NOT EXISTS model_benchmark_runs (
    benchmark_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider text NOT NULL,
    model_id text NOT NULL,
    credential_scope text NOT NULL,
    benchmark_version text NOT NULL,
    status text NOT NULL DEFAULT 'QUEUED',
    dimensions jsonb NOT NULL,
    scores jsonb NOT NULL DEFAULT '{}'::jsonb,
    sample_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
    safe_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    queued_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    UNIQUE (provider, model_id, credential_scope, benchmark_version),
    CHECK (status IN ('QUEUED','RUNNING','COMPLETED','FAILED','CANCELLED'))
);

CREATE TABLE IF NOT EXISTS provider_quota_state (
    provider text NOT NULL,
    model_id text NOT NULL DEFAULT '*',
    credential_scope text NOT NULL,
    credential_owner_id text NOT NULL DEFAULT '*',
    quota_scope text NOT NULL,
    rpm_limit bigint,
    rpd_limit bigint,
    tpm_limit bigint,
    tpd_limit bigint,
    audio_seconds_hour_limit bigint,
    audio_seconds_day_limit bigint,
    remaining bigint,
    reset_at timestamptz,
    retry_after timestamptz,
    last_429 timestamptz,
    cooldown_until timestamptz,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, model_id, credential_scope, credential_owner_id, quota_scope)
);

CREATE TABLE IF NOT EXISTS provider_health (
    provider text NOT NULL,
    model_id text NOT NULL DEFAULT '*',
    credential_scope text NOT NULL,
    credential_owner_id text NOT NULL DEFAULT '*',
    health_state text NOT NULL DEFAULT 'HEALTHY',
    consecutive_failures integer NOT NULL DEFAULT 0,
    success_count bigint NOT NULL DEFAULT 0,
    failure_count bigint NOT NULL DEFAULT 0,
    timeout_count bigint NOT NULL DEFAULT 0,
    rate_limit_count bigint NOT NULL DEFAULT 0,
    circuit_open_until timestamptz,
    last_success timestamptz,
    last_failure timestamptz,
    last_timeout timestamptz,
    failure_class text,
    health_score double precision NOT NULL DEFAULT 0.5,
    version bigint NOT NULL DEFAULT 1,
    PRIMARY KEY (provider, model_id, credential_scope, credential_owner_id),
    CHECK (health_state IN ('HEALTHY','DEGRADED','RATE_LIMITED','QUOTA_EXHAUSTED','AUTH_FAILED','UNAVAILABLE'))
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    goal text NOT NULL,
    task_type text NOT NULL,
    requirements jsonb NOT NULL DEFAULT '{}'::jsonb,
    user_answers jsonb NOT NULL DEFAULT '{}'::jsonb,
    critical_confirmations jsonb NOT NULL DEFAULT '{}'::jsonb,
    memory_capsule text,
    plan jsonb NOT NULL DEFAULT '{}'::jsonb,
    outline jsonb NOT NULL DEFAULT '{}'::jsonb,
    partial_output text,
    artifacts jsonb NOT NULL DEFAULT '[]'::jsonb,
    completed_steps jsonb NOT NULL DEFAULT '[]'::jsonb,
    remaining_steps jsonb NOT NULL DEFAULT '[]'::jsonb,
    current_provider text,
    current_model text,
    provider_attempts integer NOT NULL DEFAULT 0,
    idempotency_key text NOT NULL,
    status text NOT NULL DEFAULT 'PLANNING',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version bigint NOT NULL DEFAULT 1,
    UNIQUE (user_id, idempotency_key),
    CHECK (status IN ('PLANNING','WAITING_OPTIONAL_INPUT','WAITING_REQUIRED_INPUT','RUNNING','REVIEWING','WAITING_CONFIRMATION','EXECUTING','COMPLETED','FAILED_ALL_FREE_ROUTES'))
);

CREATE TABLE IF NOT EXISTS job_checkpoints (
    checkpoint_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL REFERENCES jobs(job_id),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    sequence_no bigint NOT NULL,
    checkpoint jsonb NOT NULL,
    partial_output text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS provider_attempts (
    attempt_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id text NOT NULL,
    job_id uuid REFERENCES jobs(job_id),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    provider text NOT NULL,
    requested_model text NOT NULL,
    actual_model text,
    credential_scope text NOT NULL,
    task_type text NOT NULL,
    start_time timestamptz NOT NULL DEFAULT now(),
    latency_ms bigint,
    success boolean NOT NULL DEFAULT false,
    status_code integer,
    error_class text,
    input_tokens bigint,
    output_tokens bigint,
    audio_usage bigint,
    fallback_number integer NOT NULL DEFAULT 0,
    fallback_reason text,
    routing_metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_provider_attempts_request
    ON provider_attempts(user_id, request_id, start_time);

CREATE TABLE IF NOT EXISTS durable_outbox (
    outbox_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text REFERENCES user_accounts(user_id),
    event_type text NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id text NOT NULL,
    payload jsonb NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    status text NOT NULL DEFAULT 'PENDING',
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    locked_at timestamptz,
    locked_by text,
    last_error_class text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CHECK (status IN ('PENDING','RUNNING','COMPLETED','FAILED','CANCELLED'))
);
CREATE INDEX IF NOT EXISTS idx_outbox_claim
    ON durable_outbox(status, available_at) WHERE status='PENDING';

CREATE TABLE IF NOT EXISTS stored_files (
    file_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    conversation_id text REFERENCES conversations(conversation_id),
    original_name text NOT NULL,
    mime_type text NOT NULL,
    size bigint NOT NULL,
    storage_location text NOT NULL,
    sha256 text NOT NULL,
    status text NOT NULL DEFAULT 'ACTIVE',
    privacy_class text NOT NULL DEFAULT 'PRIVATE',
    created_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    UNIQUE (user_id, sha256, status)
);

CREATE TABLE IF NOT EXISTS file_derivatives (
    derivative_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_file_id uuid NOT NULL REFERENCES stored_files(file_id),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    derivative_type text NOT NULL,
    processor text NOT NULL,
    processor_version text NOT NULL,
    storage_location text,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_file_id, derivative_type, processor, processor_version)
);

CREATE TABLE IF NOT EXISTS ocr_cache (
    ocr_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_file_id uuid NOT NULL REFERENCES stored_files(file_id),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    file_hash text NOT NULL,
    page_number integer NOT NULL DEFAULT 0,
    page_hash text NOT NULL,
    ocr_provider text NOT NULL,
    ocr_version text NOT NULL,
    extracted_text text NOT NULL,
    layout_data jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, page_hash, ocr_provider, ocr_version)
);

CREATE TABLE IF NOT EXISTS biometric_profiles (
    biometric_profile_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    person_id uuid NOT NULL REFERENCES entities(entity_id),
    provider text NOT NULL,
    faceset_reference text NOT NULL,
    provider_face_refs_protected bytea NOT NULL,
    consent_record jsonb NOT NULL,
    status text NOT NULL DEFAULT 'ACTIVE',
    enrolled_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    version bigint NOT NULL DEFAULT 1,
    UNIQUE (user_id, person_id, provider)
);

CREATE TABLE IF NOT EXISTS biometric_person_links (
    link_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    biometric_profile_id uuid NOT NULL REFERENCES biometric_profiles(biometric_profile_id),
    person_id uuid NOT NULL REFERENCES entities(entity_id),
    confidence_threshold double precision NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, biometric_profile_id, person_id)
);

CREATE TABLE IF NOT EXISTS reminders (
    reminder_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    meaning text NOT NULL,
    schedule_json jsonb NOT NULL,
    status text NOT NULL DEFAULT 'SCHEDULED',
    idempotency_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    fired_at timestamptz,
    deleted_at timestamptz,
    UNIQUE (user_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS events (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    event_type text NOT NULL,
    event_date timestamptz,
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'ACTIVE',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz
);

CREATE TABLE IF NOT EXISTS weather_cache (
    cache_key text PRIMARY KEY,
    location jsonb NOT NULL,
    forecast_window tstzrange NOT NULL,
    data_type text NOT NULL,
    provider_payload jsonb NOT NULL,
    retrieved_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS shared_usage_ledger (
    usage_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text NOT NULL REFERENCES user_accounts(user_id),
    provider text NOT NULL,
    model_id text NOT NULL,
    credential_scope text NOT NULL,
    request_count bigint NOT NULL DEFAULT 1,
    input_tokens bigint NOT NULL DEFAULT 0,
    output_tokens bigint NOT NULL DEFAULT 0,
    audio_seconds bigint NOT NULL DEFAULT 0,
    recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_usage_fair_use
    ON shared_usage_ledger(provider, credential_scope, user_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text REFERENCES user_accounts(user_id),
    actor_id text,
    action text NOT NULL,
    object_type text NOT NULL,
    object_id text NOT NULL,
    safe_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    request_id text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_user_created
    ON audit_log(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS backup_metadata (
    backup_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    backup_type text NOT NULL,
    storage_location_reference text NOT NULL,
    encrypted boolean NOT NULL DEFAULT true,
    checksum text,
    database_schema_version text NOT NULL,
    status text NOT NULL,
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    retention_until timestamptz,
    second_location_status text,
    safe_error_class text
);

INSERT INTO schema_migrations (migration_id, checksum, applied_by)
VALUES ('0001_jupiter_control_plane', 'REPLACE_WITH_REVIEWED_SHA256', current_user)
ON CONFLICT (migration_id) DO NOTHING;

COMMIT;
