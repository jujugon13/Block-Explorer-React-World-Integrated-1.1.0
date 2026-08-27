-- 키워드 검색(specs/14 S3.1·S4)을 위한 PostgreSQL FTS 색인.
-- BM25 문서 길이는 이미 저장된 document_chunks.token_estimate(공백 기준 토큰 수,
-- FR-IDX-043)를 그대로 쓰므로 길이 전용 컬럼을 새로 만들지 않는다.
-- 텍스트 검색 구성은 'simple'로 고정한다. 형태소 분석기를 쓰면 색인 시점 구성과
-- 질의 시점 구성이 갈릴 때 같은 문서가 조용히 검색되지 않는다.
ALTER TABLE document_chunks
    ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED;

CREATE INDEX ix_document_chunks_search_vector
    ON document_chunks USING gin (search_vector);
