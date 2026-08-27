ALTER TABLE embedding_models
    RENAME COLUMN name TO model_name;

ALTER TABLE embedding_models
    ADD COLUMN provider varchar(32),
    ADD COLUMN model_version varchar(255),
    ADD CONSTRAINT ck_embedding_models_provider
        CHECK (provider IS NULL OR provider = 'OPENAI'),
    ADD CONSTRAINT ck_embedding_models_model_version_nonblank
        CHECK (model_version IS NULL OR btrim(model_version) <> '');
