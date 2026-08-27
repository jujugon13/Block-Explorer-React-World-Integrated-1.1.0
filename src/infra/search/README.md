# PostgreSQL 검색 어댑터

벡터 검색은 pgvector로, 키워드 검색은 PostgreSQL FTS + Okapi BM25로 shared 검색 포트에
맞춰 제공한다.

## 담당 명세

| 명세 절 | 내용 |
|---|---|
| `specs/14-search-and-rag.md` S3.1·S4·S12~S14 | 모드별 검색·점수·오류·외부 호출 정책·설정 |
| `specs/10-nfr-and-security.md` §17 | 트랜잭션·동시성·성능 경계 |

## 담당 acceptance criteria

직접 소유 없음. F17의 `acceptance_ids`는 빈 배열이며 기존 AC를 통합시험으로 재검증한다.
이 폴더가 재검증에 기여하는 AC는 `AC-RS-05`·`AC-RS-06`·`AC-RS-08`·`AC-RS-22`·`AC-RS-29`·
`AC-RS-30`이며, 모두 `src/search`의 파이프라인 시험이 판정한다.

## 조합

| 역할 | 구현 | 포트 |
|---|---|---|
| 벡터 검색 | `PostgresVectorSearcher` — pgvector HNSW 코사인 | `src.shared.VectorSearcher` |
| 키워드 검색 | `PostgresKeywordSearcher` — `tsvector` + Okapi BM25 | `src.shared.KeywordSearcher` |

## 키워드 검색 구현 결정

- **엔진은 PostgreSQL FTS다.** S14의 `keyword_engine` 기본값은 `elasticsearch`지만 S17이
  "인프라 도입 여부와 색인 이중화 비용은 이 명세의 범위 밖"이라고 남겼고, `specs/manifest.json`
  F17의 외부 의존은 `PostgreSQL FTS`다. 별도 검색 클러스터를 도입하면 청크 원장과 색인이
  이중화되어 정합성 이슈가 하나 더 생긴다. 설정값 자체는 저장·노출 계약이므로 바꾸지 않는다.
- **점수는 Okapi BM25 원점수다**(S4). `k1=1.2`, `b=0.75`로 Lucene·Elasticsearch 기본값을
  따른다. `cascading_bm25_threshold` `3.0`과 `cascading_min_doc_score` `1.0`이 그 척도로
  보정된 값이기 때문이다. PostgreSQL 기본 순위 함수(`ts_rank`·`ts_rank_cd`)는 BM25가 아니므로
  쓰지 않는다.
- **문서 길이는 `document_chunks.token_estimate`다.** FR-IDX-043이 정의한 공백 기준 토큰 수이며
  이미 저장돼 있다. 길이 전용 컬럼을 새로 만들지 않는다.
- **BM25 통계(N·평균 길이·문서 빈도)의 모집단은 4.5단계 권한 사전 필터가 통과시킨 색인 완료 청크
  집합 하나다.** 모집단을 전역으로 두고 후보만 필터하면 IDF와 점수 규모가 어긋난다. 대신 같은
  질의라도 요청자의 열람 범위가 다르면 점수가 달라진다. 평균 문서 길이는 이 모집단 전체를
  집계해야 하므로 질의마다 허용 청크 집합을 한 번 훑는다. 후보 추출은 GIN 색인을 타지만 이
  집계는 타지 않는다 — 모집단이 커지면 여기가 먼저 느려진다.
- **텍스트 검색 구성은 `simple` 고정이다.** 색인 시점(생성 컬럼)과 질의 시점이 반드시 같은 구성을
  써야 하며, 형태소 분석기를 나중에 갈아끼우면 재색인 없이 조용히 검색 누락이 생긴다.
- **청크·문서 식별자는 벡터 어댑터와 동일한 `chunk_search_id`·`document_search_id`를 쓴다.**
  S17이 남긴 "청크 식별자 체계가 다르면 6단계 중복 제거가 동작하지 않는다"는 미확인 사항을
  두 어댑터가 같은 함수를 쓰는 것으로 해소한다. `metadata` 키도 동일하다.
- **필터는 벡터 어댑터와 글자 그대로 같다** — 허용 문서 ID, `documents.status = 'INDEXED'`,
  `deleted_at IS NULL`, `current_version_id = document_version_id`, `versions.status = 'INDEXED'`.
- **요청 타임아웃 30초는 `SET LOCAL statement_timeout`으로 강제하고 질의 후 되돌린다**(S13).
  검색은 하나의 트랜잭션(§17.1)이라 되돌리지 않으면 이후 문장까지 묶인다.
- **재시도 대상은 연결·타임아웃·프로토콜뿐이다**(S13). SQLSTATE `08*`·`53300`·`57014`·`57P0*`와
  psycopg `OperationalError`·`InterfaceError`만 `KeywordTransportError`로 올려 앱 재시도
  3회(`src/search/calls.py`의 `keyword_search`)를 받고, 나머지는 즉시 `SearchUnavailable`
  (503 `SEARCH_SERVICE_ERROR`)로 끝낸다.

## 스키마

`0010_keyword_search_fts.sql`이 `document_chunks.search_vector`를
`GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED`로 추가하고 GIN 색인을 만든다.
생성 컬럼이므로 청크 저장 경로(`indexing_store_content.save_chunks`)는 바뀌지 않는다.

## 의존

| 대상 | 경유 | 비고 |
|---|---|---|
| pgvector 0.8.1 | 11단계 PostgreSQL 연결·UoW | 별도 연결 경계 생성 금지 |
| PostgreSQL FTS | 같은 UoW | 별도 연결 경계 생성 금지 |
| 검색 계약 | `src.shared.VectorSearcher`·`src.shared.KeywordSearcher` | 검색 기능 폴더 직접 참조 금지 |

## 이 폴더가 책임지지 않는 것

- 파이프라인 단계 순서·조건·추적 이름과 모드 선택
- 순위 융합(RRF)·리랭킹·품질 게이트
- 앱 재시도 횟수와 대기 계산(`src/search/calls.py`가 소유)
