DO $$
DECLARE
    selected_model_id bigint;
    active_model_count bigint;
    index_started_at timestamptz;
BEGIN
    SELECT count(*), min(embedding_model_id)
      INTO active_model_count, selected_model_id
      FROM embedding_models
     WHERE active AND searchable;

    IF active_model_count = 0 THEN
        INSERT INTO embedding_models (
            model_name, dimension, active, searchable, provider, model_version
        ) VALUES (
            'text-embedding-3-small', 1536, true, true,
            'OPENAI', 'text-embedding-3-small'
        )
        RETURNING embedding_model_id INTO selected_model_id;
    ELSIF active_model_count <> 1 OR NOT EXISTS (
        SELECT 1
          FROM embedding_models
         WHERE embedding_model_id = selected_model_id
           AND provider = 'OPENAI'
           AND model_name = 'text-embedding-3-small'
           AND model_version = 'text-embedding-3-small'
           AND dimension = 1536
           AND active
           AND searchable
    ) THEN
        RAISE EXCEPTION 'embedding model registry must contain exactly one matching active model';
    END IF;

    index_started_at := clock_timestamp();
    EXECUTE format(
        'CREATE INDEX ix_document_vectors_hnsw_active_openai_1536 '
        'ON document_vectors USING hnsw '
        '((embedding::vector(1536)) vector_cosine_ops) '
        'WHERE embedding_model_id = %s AND status = ''ACTIVE''',
        selected_model_id
    );
    RAISE NOTICE 'hnsw_index_build_elapsed_ms=%',
        round(extract(epoch FROM clock_timestamp() - index_started_at) * 1000, 3);
END
$$;
