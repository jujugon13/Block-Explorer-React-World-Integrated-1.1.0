CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS vectorshelf_schema_migrations (
    version integer NOT NULL,
    filename text NOT NULL,
    sha256 character(64) NOT NULL,
    applied_at timestamp with time zone NOT NULL,
    CONSTRAINT pk_vectorshelf_schema_migrations PRIMARY KEY (version),
    CONSTRAINT uq_vectorshelf_schema_migrations_filename UNIQUE (filename),
    CONSTRAINT ck_vectorshelf_schema_migrations_version_positive
        CHECK (version > 0),
    CONSTRAINT ck_vectorshelf_schema_migrations_sha256_lower_hex
        CHECK (sha256 ~ '^[0-9a-f]{64}$')
);
