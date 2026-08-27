CREATE TABLE sync_events (
    event_id uuid PRIMARY KEY,
    idempotency_key text NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id text NOT NULL,
    aggregate_version bigint,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL,
    occurred_at timestamptz NOT NULL,
    available_at timestamptz,
    max_retries integer NOT NULL,
    failure_count integer NOT NULL DEFAULT 0,
    owner_name text,
    claim_token uuid,
    locked_at timestamptz,
    lease_expires_at timestamptz,
    processed_at timestamptz,
    failed_at timestamptz,
    error_type text,
    error_message text,
    CONSTRAINT uq_sync_events_idempotency_key UNIQUE (idempotency_key),
    CONSTRAINT ck_sync_events_idempotency_key_nonblank
        CHECK (btrim(idempotency_key) <> ''),
    CONSTRAINT ck_sync_events_aggregate_id_nonblank
        CHECK (btrim(aggregate_id) <> ''),
    CONSTRAINT ck_sync_events_kind
        CHECK (
            (aggregate_type = 'DOCUMENT'
                AND aggregate_version IS NULL
                AND event_type = 'DOCUMENT_DELETED')
            OR
            (aggregate_type = 'DOCUMENT_VERSION'
                AND aggregate_version IS NOT NULL
                AND aggregate_version > 0
                AND event_type IN (
                    'DOCUMENT_VERSION_CREATED',
                    'DOCUMENT_REINDEX_REQUESTED'
                ))
            OR
            (aggregate_type = 'PERMISSION'
                AND aggregate_version IS NULL
                AND event_type = 'PERMISSION_CACHE_REFRESH_REQUESTED')
            OR
            (aggregate_type = 'EMBEDDING_MODEL'
                AND aggregate_version IS NULL
                AND event_type = 'EMBEDDING_MODEL_ACTIVATED')
        ),
    CONSTRAINT ck_sync_events_status
        CHECK (status IN ('PENDING', 'PROCESSING', 'PROCESSED', 'FAILED')),
    CONSTRAINT ck_sync_events_retry_counts
        CHECK (
            max_retries >= 0
            AND failure_count >= 0
            AND failure_count <= max_retries + 1
        ),
    CONSTRAINT ck_sync_events_owner_fields
        CHECK (
            (
                status = 'PROCESSING'
                AND owner_name IS NOT NULL
                AND btrim(owner_name) <> ''
                AND claim_token IS NOT NULL
                AND locked_at IS NOT NULL
                AND lease_expires_at IS NOT NULL
                AND lease_expires_at > locked_at
            )
            OR
            (
                status <> 'PROCESSING'
                AND owner_name IS NULL
                AND claim_token IS NULL
                AND locked_at IS NULL
                AND lease_expires_at IS NULL
            )
        ),
    CONSTRAINT ck_sync_events_processed_at
        CHECK (
            (status = 'PROCESSED' AND processed_at IS NOT NULL)
            OR (status <> 'PROCESSED' AND processed_at IS NULL)
        ),
    CONSTRAINT ck_sync_events_failed_terminal
        CHECK (
            status <> 'FAILED'
            OR (
                failed_at IS NOT NULL
                AND available_at IS NULL
                AND error_type IS NOT NULL
                AND error_message IS NOT NULL
            )
        )
);

CREATE INDEX ix_sync_events_pending_claim
    ON sync_events (available_at, occurred_at, event_id)
    WHERE status = 'PENDING';

CREATE INDEX ix_sync_events_processing_recovery
    ON sync_events (lease_expires_at, event_id)
    WHERE status = 'PROCESSING' AND lease_expires_at IS NOT NULL;

CREATE INDEX ix_sync_events_status_type_occurred
    ON sync_events (status, event_type, occurred_at, event_id);


CREATE TABLE sync_delivery_attempts (
    delivery_attempt_id uuid PRIMARY KEY,
    event_id uuid NOT NULL,
    attempt_no integer NOT NULL,
    status text NOT NULL,
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    error_type text,
    error_message text,
    CONSTRAINT fk_sync_delivery_attempts_event
        FOREIGN KEY (event_id) REFERENCES sync_events (event_id),
    CONSTRAINT uq_sync_delivery_attempts_event_attempt
        UNIQUE (event_id, attempt_no),
    CONSTRAINT ck_sync_delivery_attempts_attempt_no
        CHECK (attempt_no >= 1),
    CONSTRAINT ck_sync_delivery_attempts_status
        CHECK (status IN ('STARTED', 'SUCCEEDED', 'FAILED')),
    CONSTRAINT ck_sync_delivery_attempts_time
        CHECK (ended_at IS NULL OR ended_at >= started_at),
    CONSTRAINT ck_sync_delivery_attempts_result
        CHECK (
            (status = 'STARTED'
                AND ended_at IS NULL
                AND error_type IS NULL
                AND error_message IS NULL)
            OR
            (status = 'SUCCEEDED'
                AND ended_at IS NOT NULL
                AND error_type IS NULL
                AND error_message IS NULL)
            OR
            (status = 'FAILED'
                AND ended_at IS NOT NULL
                AND error_type IS NOT NULL
                AND error_message IS NOT NULL)
        )
);


CREATE TABLE consistency_issues (
    issue_id uuid PRIMARY KEY,
    issue_type text NOT NULL,
    severity text NOT NULL,
    status text NOT NULL,
    safe_to_repair boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    ignored_reason text,
    CONSTRAINT ck_consistency_issues_type
        CHECK (issue_type IN (
            'MISSING_JOB',
            'MISSING_CHUNKS',
            'MISSING_EMBEDDINGS',
            'MODEL_MISMATCH',
            'INVALID_CURRENT_VERSION',
            'STALLED_VERSION',
            'DELETED_DOCUMENT_RESIDUE',
            'ORPHANED_DATA'
        )),
    CONSTRAINT ck_consistency_issues_severity
        CHECK (severity IN ('WARNING', 'ERROR', 'CRITICAL')),
    CONSTRAINT ck_consistency_issues_status
        CHECK (status IN ('OPEN', 'REPAIRING', 'RESOLVED', 'IGNORED')),
    CONSTRAINT ck_consistency_issues_time
        CHECK (updated_at >= created_at),
    CONSTRAINT ck_consistency_issues_repairable
        CHECK (status <> 'REPAIRING' OR safe_to_repair),
    CONSTRAINT ck_consistency_issues_ignored_reason
        CHECK (
            (status = 'IGNORED'
                AND ignored_reason IS NOT NULL
                AND btrim(ignored_reason) <> '')
            OR (status <> 'IGNORED' AND ignored_reason IS NULL)
        )
);

CREATE INDEX ix_consistency_issues_admin_query
    ON consistency_issues (
        status,
        issue_type,
        severity,
        created_at,
        issue_id
    );


CREATE TABLE operator_actions (
    action_id uuid PRIMARY KEY,
    action_type text NOT NULL,
    target_type text NOT NULL,
    target_id text NOT NULL,
    actor_id text NOT NULL,
    occurred_at timestamptz NOT NULL,
    reason text,
    CONSTRAINT ck_operator_actions_action_type
        CHECK (action_type IN (
            'EVENT_RETRIED',
            'ISSUE_IGNORED',
            'ISSUE_REPAIR_REQUESTED',
            'RECONCILIATION_REQUESTED'
        )),
    CONSTRAINT ck_operator_actions_target_type
        CHECK (target_type IN (
            'SYNC_EVENT',
            'CONSISTENCY_ISSUE',
            'RECONCILIATION'
        )),
    CONSTRAINT ck_operator_actions_identifiers_nonblank
        CHECK (btrim(target_id) <> '' AND btrim(actor_id) <> ''),
    CONSTRAINT ck_operator_actions_reason
        CHECK (
            (action_type = 'ISSUE_IGNORED'
                AND reason IS NOT NULL
                AND btrim(reason) <> '')
            OR (action_type <> 'ISSUE_IGNORED' AND reason IS NULL)
        )
);

CREATE INDEX ix_operator_actions_target
    ON operator_actions (target_type, target_id, occurred_at, action_id);

CREATE INDEX ix_operator_actions_actor
    ON operator_actions (actor_id, occurred_at, action_id);


CREATE TABLE reconciliation_runs (
    reconciliation_id uuid PRIMARY KEY,
    mode text NOT NULL,
    cursor text,
    status text NOT NULL,
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    CONSTRAINT ck_reconciliation_runs_mode
        CHECK (mode IN ('DRY_RUN', 'REPAIR')),
    CONSTRAINT ck_reconciliation_runs_status
        CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED')),
    CONSTRAINT ck_reconciliation_runs_completion
        CHECK (
            (status = 'RUNNING' AND completed_at IS NULL)
            OR (
                status IN ('COMPLETED', 'FAILED')
                AND completed_at IS NOT NULL
                AND completed_at >= started_at
            )
        )
);

CREATE INDEX ix_reconciliation_runs_status_started
    ON reconciliation_runs (status, started_at, reconciliation_id);
