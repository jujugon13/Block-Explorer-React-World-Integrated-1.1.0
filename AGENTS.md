# AGENTS.md — VectorShelf 독립 구현 지침

이 저장소는 **기능명세 팩**이다. 원본 구현 소스는 포함되어 있지 않으며, 참조해서도 안 된다.

## 구성

| 파일 | 역할 | 배포 대상 |
|---|---|---|
| `AGENTS.md` | 구현 지침, 폴더 정책, 진행 순서 | 예 |
| `specs/01`~`13` | 21개 절로 구성된 블랙박스 기능명세서 | 예 |
| `specs/14-search-and-rag.md` | 검색·답변 교체본 | 예 |
| `specs/pipeline/search.pipeline.json` | 검색 파이프라인 실행 정의 (런타임이 읽음) | 예 |
| `specs/manifest.json` | 기능 ↔ 명세 절 ↔ AC ID ↔ 구현 폴더 매핑 | 예 |
| `DECISIONS.md` | 교체 결정 8건과 근거 | 예 |
| `audit/traceability.md` | 추출 완전성 추적표 | **아니오 — 내부 감사 전용** |

## 단일 기준 원칙

1. 구현의 유일한 근거는 `specs/`다. 명세에 없는 동작을 추가하지 않는다.
2. 검색 파이프라인의 **단계 순서·실행 조건·추적 이름**은 `specs/pipeline/search.pipeline.json`이
   유일한 기준이다. 코드에 순서를 하드코딩하지 않고 이 정의를 읽어 해석 실행한다.
3. **[확인 불가]** 또는 **[정적 추정]** 표기 항목은 구현 전에 결정을 문서화한다.
4. 명세와 실제 동작이 다르다고 판단되면 명세를 고치고 코드를 명세에 맞춘다. 반대로 하지 않는다.

---

## 폴더 정책 — 기능 우선(package by feature)

계층(controller/service/repository)이 아니라 **기능 단위**로 최상위 폴더를 나눈다.
각 기능 폴더는 자기 완결적이며, 폴더 간 의존은 `shared/`를 경유하거나 명시적 인터페이스로만 허용한다.

```
src/
  platform/          공통 응답·오류 봉투, 보안 필터, CORS, 예외 매핑
  auth/              인증·토큰·역할 캐시·비밀번호 정책
  users/             계정·부서·역할 원장, 관리자 사용자 API
  documents/         문서 CRUD·버전·메타데이터·논리 삭제
  storage/           파일 저장소 — 어댑터별 하위 폴더
    local/           로컬 파일시스템 어댑터
    minio/           S3 호환 오브젝트 스토리지 어댑터
    s3/              클라우드 오브젝트 스토리지 어댑터
  parsing/           문서 변환(PDF·DOCX·TXT·MD)·청킹
  indexing/          색인 작업 큐·소유권(리스)·시도·완료·실패·수동 재처리
  worker/            처리 노드 등록·생존 신호·폴링·백오프·수명 주기
  embedding/         임베딩 배치·차원 검증·회로 차단기
  collections/       계층형 컬렉션·문서 매핑
  permissions/       권한 원장·접근 캐시·판정 규칙
  search/            검색 파이프라인 해석기 + 단계 핸들러
    steps/           16개 단계 핸들러 (단계당 파일 1개)
  guardrails/        인젝션·개인정보·환각·충실도·숫자 검증
  sync/              Outbox 이벤트·디스패처·정합성 검사
  mcp/               외부 도구 연동·API 키·호출 빈도 제한
  ops/               운영 대시보드·집계·WebSocket push
  shared/            공용 타입, 외부 시스템 어댑터 인터페이스
```

### 규칙

1. **각 기능 폴더에 `README.md`를 둔다.** 담당 명세 절 번호와 acceptance criteria ID를 표로 적고,
   `specs/manifest.json`의 `features` 항목과 내용이 일치해야 한다. 템플릿은 `templates/feature-README.md`.
2. **외부 시스템 접근은 `shared/`의 어댑터 인터페이스를 통해서만 한다.** 기능 폴더가 DB 드라이버,
   저장소 SDK, HTTP 클라이언트를 직접 다루지 않는다.
3. **저장소 어댑터는 하위 폴더로 분리한다.** `storage/local`, `storage/minio`, `storage/s3` 각각이
   같은 인터페이스를 구현하며, 어느 하나를 지워도 나머지가 컴파일된다.
   설정으로 하나만 활성화되므로(§1 FR-SYS-005) 활성 어댑터 선택 로직은 `storage/` 최상위에 둔다.
4. **검색 단계 핸들러는 파일 1개씩 분리한다.** `search/steps/` 아래에 단계 ID와 같은 이름으로 둔다.
   핸들러는 자기 단계 로직만 알고 **순서·조건·추적 이름은 모른다.**
5. **테스트는 대상 기능 폴더 안에 둔다.** 테스트 이름에 acceptance criteria ID를 포함해 추적 가능하게 한다.
   예: `AC_DOC_003_파일크기_50MB_초과시_거부`
6. **폴더 간 직접 참조 금지.** `documents/`가 `storage/s3/`를 직접 import하지 않는다.
   `shared/`의 저장소 인터페이스만 참조한다.
7. 한 폴더가 500줄을 넘으면 하위 폴더로 쪼갠다. 계층이 아니라 **하위 기능**으로 쪼갠다.

### 폴더 정책 위반 예시

```
✗ src/service/DocumentService.java        계층 기준으로 나눔
✗ src/documents/S3Uploader.java           저장소 구현이 문서 폴더에 섞임
✗ src/storage/StorageService.java         어댑터 3종이 한 파일에
✗ src/search/SearchPipeline.java          16단계가 한 파일에

✓ src/documents/upload.*                  기능 기준
✓ src/storage/s3/adapter.*                어댑터별 분리
✓ src/search/steps/guardrail_input.*      단계별 분리
```

---

## 진행 순서

| 단계 | 범위 | 완료 기준 |
|---|---|---|
| 0 | 결정 확인 | `DECISIONS.md` 읽고 **D-1 전제(외부 API vs 자체 호스팅 LLM)** 확정. `13-unknowns.md` §21.6의 데이터 반출 정책 확인 |
| 1 | `platform` + `shared` | 공통 응답 봉투(§6.1), 오류 매핑(§10.2), 보안 필터·CORS(§18) |
| 2 | `auth` + `users` | `AC-AUTH-*`, `AC-SYS-001~003` 통과 |
| 3 | `storage` (3개 어댑터) | `AC-DOC-011~013`, `AC-DOC-037~038` 통과 |
| 4 | `documents` + `parsing` | `AC-DOC-*` 통과 |
| 5 | `permissions` + `collections` | `AC-PERM-*`, `AC-COL-*` 통과 |
| 6 | `indexing` + `worker` + `embedding` | `AC-IDX-*` 통과 |
| 7 | `search` 해석기 + 16단계 스텁 | 핸들러가 비어 있어도 추적 배열이 정의대로 나옴 |
| 8 | `search` 핸들러 + `guardrails` | `AC-RS-*` 통과 |
| 9 | `sync`, `mcp`, `ops` | 나머지 AC 통과 |
| 10 | `infra` 조합·설정 | F17 `src/infra` 하나만 manifest에 선언한다. `src/infra/README.md`와 내부 `postgres`, `s3`, `ai`, `search`, `http` 폴더 및 각 README를 둔다. 환경변수 누락·형식 오류는 외부 연결 전에 기동 실패하며 배포 구성에서 인메모리 구현으로 조용히 전환하지 않는다. `tools/check_layout.py`를 변경하지 않은 상태에서 위반 0건·경고 0건 및 `PYTHONDONTWRITEBYTECODE=1 bash tools/gate.sh src 10` 통과 |
| 11 | `infra/postgres` — RDS PostgreSQL 영속화 | PostgreSQL 18.3, psycopg 3, 순수 SQL 마이그레이션으로 빈 DB를 구성하고 재실행 시 이미 적용한 변경을 반복하지 않는다. `vector` 확장을 활성화한 뒤 실제 서버 버전과 확장 버전 0.8.1을 별도로 확인한다. 사용자·문서·권한·컬렉션·색인·Worker·Sync·MCP 키·검색 이력이 재시작 후 보존된다. 문서+버전+색인 작업+Outbox, 권한+Outbox, Sync 효과+처리 완료가 각각 한 DB 커밋으로 성공하거나 함께 롤백된다. `FOR UPDATE SKIP LOCKED` 경합 시험과 `gate.sh src 11` 통과 |
| 12 | `infra/s3` — S3 오브젝트 저장소 | 확정된 실제 저장소에서 저장·조회·멱등 삭제·후보 객체 정리·404 구분·크기 검증·provider/bucket 불일치 거부가 기존 저장소 계약과 동일하다. 공급자·endpoint·bucket·리전·인증정보가 없거나 연결 사전 점검이 실패하면 추가 연구 없이 즉시 중단 보고한다. `gate.sh src 12` 통과 |
| 13 | `infra/ai` — 외부 임베딩·LLM·리랭커 | 확정된 공급자·모델로 질의 임베딩, 순서 보존 배치 임베딩, 답변 생성 및 리랭킹을 실제 호출한다. 입력 절단·차원 검증·재시도·`Retry-After`·타임아웃·회로 차단·동시 호출 제한·오류 변환이 대역시험과 실호출 시험에서 동일하다. 공급자·모델·API 키·엔드포인트가 없으면 즉시 중단 보고한다. `gate.sh src 13` 통과 |
| 14 | `infra/search` — pgvector·PostgreSQL FTS | 11단계의 동일 연결 풀과 트랜잭션 경계를 재사용한다. pgvector 0.8.1의 iterative index scan을 명시적으로 선택하고 필터 적용 후에도 요구한 top-k와 정렬 계약을 만족하는지 검증한다. 현재 버전의 `ACTIVE` 벡터만 반환하고 삭제·과거 버전은 `STALE`로 검색에서 제외한다. 빈 권한 후보에서는 검색 SQL을 실행하지 않는다. PostgreSQL 기본 `ts_rank`·`ts_rank_cd`를 BM25로 간주하지 않는다. 실검색 통합시험과 `gate.sh src 14` 통과 |
| 15 | `infra/http` — FastAPI·uvicorn 전환 | uvicorn에서 모든 REST 경로, 상태 코드, 공통 응답 봉투, multipart 50/60MB 경계, 파일 헤더와 CORS가 기존 계약과 동일하다. `/ws` RFC 6455 연결, STOMP `CONNECT` 인증·구독·전송 거부·push, SockJS fallback이 동일하게 동작한다. lifespan이 Worker·Sync·보존 작업을 정확히 한 번 시작·종료한다. 기존 191개 AC 전체 회귀와 `gate.sh src 15` 통과 |
| 16 | 다중 노드·강제 종료·Outbox·DB 장애 통합시험 | 동일 RDS를 사용하는 둘 이상의 애플리케이션 노드가 하나의 작업을 중복 소유하지 않는지 검증한다. claim 직후·외부 효과 직후·DB 커밋 전후에 프로세스를 강제 종료해 만료 리스 회수, 멱등 재처리, Outbox 재생, 커밋 이벤트 무유실과 외부 효과 비중복을 확인한다. Single-AZ에서는 재부팅 또는 연결 차단 후 기존 연결 폐기·재접속·서비스 복구까지만 검증하며 이를 Primary failover로 기록하지 않는다. 실제 Primary 장애조치는 Multi-AZ 시험 DB에서 endpoint 재연결·작업 회수·Outbox 무유실을 확인해야 완료된다. Multi-AZ 환경이 없으면 이 항목은 `BLOCKED`이며 단계 16 전체 통과를 주장하지 않는다. `gate.sh src 16` 통과 |
| 17 | 문서화·배포 ZIP | 16단계 판정이 정리된 뒤 `IMPLEMENTATION.md`와 `WORK_STATUS.md`를 실제 결과에 맞게 갱신한다. 16단계 판정 정리란 모든 항목이 통과·미검증·`BLOCKED`·보류 중 하나로 확정되고 그 근거가 기록된 상태를 말한다. 통과하지 않은 항목을 통과로 적는 것은 여전히 금지다. `audit/` 전체와 접속 비밀을 제외한 배포 ZIP을 만들고 압축 무결성·필수 파일·재압축 해제 후 `gate.sh src 17`을 검증한다. ZIP 파일명·크기와 SHA-256을 출력한다 |

각 단계는 이전 단계의 acceptance criteria가 회귀하지 않는 상태로만 종료한다.

**7단계를 8단계보다 먼저 하는 이유**: 파이프라인 뼈대가 먼저 서면 이후 작업이 "단계 하나 채우기"로
쪼개지고, 회귀도 단계 단위로 잡힌다.

---

## 검증 수준별 실행 방침

| 수준 | 실행 방법 |
|---|---|
| 자동 | 외부 의존 없이 단위 테스트. CI 필수 게이트 |
| 모의 | 외부 서비스를 대역으로 대체. CI 필수 게이트 |
| 정적 | 코드 리뷰 체크리스트로 확인하고 리뷰 기록을 남김 |
| 외부 | 통합 환경에서 주기 실행. CI 게이트 제외, 릴리스 전 1회 필수 |

추가 게이트:

| 명령 | 검사 내용 |
|---|---|
| `python3 tools/check_layout.py src` | 폴더 정책 위반 |
| `python3 tools/check_coverage.py src` | AC 커버리지 — 테스트 이름에서 직접 센다 |
| `python3 tools/verify_pipeline.py --trace <debug응답> specs/pipeline/search.pipeline.json` | 런타임 추적이 정의를 벗어났는지 |
| `bash tools/gate.sh src` | 위 셋 + 테스트 실행을 한 번에 |

## 완료 판정

**자기 보고를 완료로 인정하지 않는다.** 단계 종료는 `bash tools/gate.sh src` 가 통과했을 때만 인정한다.
판정용 프롬프트는 `PROMPTS.md` §6~§10에 있다.

- §6 단계 완료 판정 — 매 단계 끝날 때
- §7 기능 단위 완료 판정 — 폴더 하나가 끝났을 때. **비직관 동작을 조용히 고쳤는지** 확인한다
- §8 검색 파이프라인 완료 판정 — 단계 순서 하드코딩과 권한 단계 우회를 잡는다
- §9 전체 완료 판정 — 릴리스 전 1회
- §10 테스트 신뢰성 검증 — 아무것도 검증하지 않는 테스트를 잡는다

---

## 하지 말아야 할 것

- 명세에 없는 필드를 응답에 추가하지 않는다. 추가 필드 허용은 **요청**에만 적용된다.
- 사용자 표시 문자열을 임의로 다듬지 않는다. §10.1 오류 메시지와 §14 고정 문구는 그대로 쓴다.
- 검색 단계 순서를 성능을 이유로 바꾸지 않는다. **추적 배열 순서가 외부 계약이다.**
- 권한 사전 필터(4.5단계)와 라이브 재검증(6.5단계)에 비활성 스위치를 만들지 않는다.
- 계층 기준으로 폴더를 나누지 않는다. 리팩터링하고 싶으면 이 문서를 먼저 고친다.
