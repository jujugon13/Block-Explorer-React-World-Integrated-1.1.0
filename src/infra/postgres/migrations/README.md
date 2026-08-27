# PostgreSQL 마이그레이션

SQL 파일은 `0001`부터 빈 번호 없이 파일명 순서로 적용한다. 이미 적용된 파일의 이름이나 SHA-256이 바뀌면 기동을 중단한다.

| 순서 | 파일 | 범위 |
|---:|---|---|
| 1 | `0001_bootstrap.sql` | pgvector 확장, 적용 이력 |
| 2 | `0002_identity.sql` | 부서·역할·사용자 |
| 3 | `0003_documents.sql` | 파일·문서·버전 |
| 4 | `0004_access.sql` | 컬렉션·권한·접근 캐시 |
| 5 | `0005_indexing.sql` | 색인·Worker·청크·벡터 |
| 6 | `0006_sync.sql` | Outbox·전달 시도·정합성 |
| 7 | `0007_mcp_search_history.sql` | MCP 키·검색 이력 |
| 8 | `0008_embedding_model_registry.sql` | 임베딩 공급자·모델 버전 원장 열 |
| 9 | `0009_vector_search_hnsw.sql` | 활성 OpenAI 1536차원 벡터의 partial expression HNSW |

접속정보는 `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` 환경변수로만 읽고 출력하거나 적용 이력에 저장하지 않는다.
