# 색인 작업

색인 작업의 획득, 소유권 리스, 실행 시도, 단계 결과, 완료·실패와 운영자 재처리를 담당한다.

## 담당 명세

| 명세 절 | 내용 |
|---|---|
| `§5.1.6 FR-IDX` | 관리자 색인 작업 API |
| `§8.4` | 색인 작업 상태 전이 |
| `§11` | 획득 우선순위·리스·충돌·수동 재처리 |
| `§12` | 주기·백오프·재시도·타임아웃·순서 |
| `§14 멱등` | 시도·청크·임베딩·완료·실패 재생 |
| `specs/15-implementation-contract-addendum.md` A4 | 관리자 REST 경로별 정확 응답 키 집합 |

## 담당 acceptance criteria

| 범위 | ID |
|---|---|
| 획득·소유권 | AC-IDX-001, AC-IDX-002, AC-IDX-003, AC-IDX-004, AC-IDX-005, AC-IDX-006, AC-IDX-007, AC-IDX-008, AC-IDX-009, AC-IDX-010, AC-IDX-011, AC-IDX-012 |
| 멱등성 | AC-IDX-020, AC-IDX-021, AC-IDX-022, AC-IDX-023, AC-IDX-024, AC-IDX-025 |
| 완료·실패·재시도 | AC-IDX-030, AC-IDX-031, AC-IDX-032, AC-IDX-033, AC-IDX-034, AC-IDX-035, AC-IDX-036, AC-IDX-037, AC-IDX-038, AC-IDX-039, AC-IDX-040, AC-IDX-041 |
| 수동 재처리 | AC-IDX-060, AC-IDX-061, AC-IDX-062, AC-IDX-063, AC-IDX-064, AC-IDX-065, AC-IDX-066 |

## 공유 경계

- 문서·청크와는 `src.shared.DocumentIndexPort` DTO 계약으로 결합한다.
- 검색 권한 사전 필터에는 `src.shared.IndexCatalog`만 노출한다.
- `IndexVectorSearcher`는 완료된 현재 버전의 ACTIVE 벡터를 검색용 안정 UUID로 투영한다.
- 임베딩 호출기는 함수로 주입하며 외부 HTTP를 직접 사용하지 않는다.

## 관리자 REST 등록

`register_indexing_routes(app, service, chunk_producer=..., embedder=...)`가
`FR-IDX-001~013`의 13개 관리자 경로를 등록한다. `app`은
`add_route(method, path, handler)`만 제공하면 되며 indexing 패키지는 platform
패키지를 직접 참조하지 않는다.

- 모든 handler가 실행 시점에 `ADMIN` 역할을 다시 확인한다.
- `jobId`, `attemptId`, `documentId`, `workerId`와 UUID `claimToken`은 HTTP
  경계에서 검증한다.
- `chunk_producer(job_id, attempt_id, worker_id, claim_token)`와
  `embedder(chunks, model)`는 주입된 작업 어댑터다.
- 최초 생성과 멱등 재생의 `201`/`200`, 빈 claim의 무본문 `204`는 service의
  `OperationResult`를 그대로 HTTP 상태에 투영한다.
- 작업 목록은 `status`, `documentId`, `workerId`, `page`, `size`를 지원하고
  event 응답은 내부 `metadata`를 노출하지 않는다.

## 호환성 동작

- Q-07: 수동 재처리는 재시도 횟수와 마지막 오류를 유지한다.
- Q-08: 완료된 작업은 소유 노드와 토큰을 유지한다.
- Q-09: 자동 재시도 예약은 문서 버전 상태를 바꾸지 않는다.
