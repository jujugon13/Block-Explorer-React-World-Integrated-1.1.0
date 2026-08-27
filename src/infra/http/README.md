# HTTP·WebSocket 어댑터

FastAPI·Uvicorn에서 기존 REST·MCP·WebSocket 계약을 그대로 노출한다.

## 담당 명세

| 명세 절 | 내용 |
|---|---|
| `specs/01-purpose-and-boundary.md` FR-SYS-003·006~007 | 실행 프로파일·입력 채널·실행 인자 |
| `specs/02-interfaces.md` §5.1~§5.3 | REST·WebSocket·MCP 공개 전송 경계 |
| `specs/09-recovery-and-side-effects.md` §15 | 프로세스 수명주기·재연결 |
| `specs/10-nfr-and-security.md` §18 | 인증·인가·CORS·비밀정보 |
| `specs/14-search-and-rag.md` S1·S16 | 검색 REST 경계와 교체 AC |

F17이 직접 소유하는 AC는 없다. 따라서 `IMPLEMENTATION_MANIFEST.json`의 F17
`acceptance_ids`는 빈 배열을 유지하며, 아래 항목은 기존 기능 AC를 전송 계층에서
회귀시키기 위한 분류다.

## 191개 AC 전송 분류

분류 근거는 `specs/12-acceptance-criteria.md` §20 및
`specs/14-search-and-rag.md` S16의 관찰 지점과 위 공개 인터페이스 표다.
상태 코드·공통 봉투·응답 본문·응답 헤더·STOMP 프레임 자체를 관찰해야 하는 공개
경계 AC는 실제 Uvicorn TCP를 지난다. 저장소 목록, 원장 상태, 내부 호출 기록,
파서·Worker·보존 작업, 기동 설정처럼 전송 직렬화와 무관한 판정은 기존 자동·모의·정적
수준을 유지한다. 공개 라우트가 정의되지 않은 내부 동작은 관찰 지점에 “응답 코드”가
있더라도 HTTP 그룹에 넣지 않는다(예: `AC-SYNC-003`).

| 분류 | 수 | AC ID |
|---|---:|---|
| Uvicorn TCP | 13 | `AC-AUTH-001~013` |
| Uvicorn TCP | 31 | `AC-DOC-001~010`, `013~014`, `020~027`, `030~040` |
| Uvicorn TCP | 23 | `AC-IDX-001~010`, `020~025`, `030~032`, `060~061`, `065~066` |
| Uvicorn TCP | 6 | `AC-PERM-001~006` |
| Uvicorn TCP | 6 | `AC-COL-001~006` |
| Uvicorn TCP | 2 | `AC-SYNC-006`, `008` |
| Uvicorn TCP | 11 | `AC-MCP-001~011` |
| Uvicorn TCP/WebSocket | 6 | `AC-OPS-001~006` |
| Uvicorn TCP/WebSocket | 8 | `AC-SYS-001~008` |
| Uvicorn TCP | 11 | `AC-RS-01`, `23~25`, `27~28`, `31`, `33~36` |
| **전송 계층 합계** | **117** | 위 항목 |
| 도메인 자동·모의 | 18 | `AC-DOC-011~012`, `028`, `050~064` |
| 도메인 자동·모의 | 22 | `AC-IDX-011~012`, `033~041`, `050~057`, `062~064` |
| 도메인 자동·모의 | 7 | `AC-SYNC-001~005`, `007`, `009` |
| 도메인 정적 | 2 | `AC-SYS-009~010` |
| 도메인 자동·모의 | 25 | `AC-RS-02~22`, `26`, `29~30`, `32` |
| **도메인 합계** | **74** | 위 항목 |
| **전체** | **191** | 전송 117 + 도메인 74 |

## 검증 방식

- REST·MCP 시험은 `src.infra.http.testing.request_over_uvicorn`이 루프백 소켓에
  Uvicorn을 기동하고 실제 HTTP 클라이언트로 요청한다. 응답의 `Server: uvicorn`을
  확인하지 못하면 시험이 실패한다.
- WebSocket 시험은 `websockets` 클라이언트로 Uvicorn의 `/ws`에 실제 RFC 6455
  연결한 뒤 STOMP 프레임과 push 횟수를 관찰한다.
- multipart 시험은 파일 50MB 경계와 요청 전체 60MB 경계를 실제 TCP 본문으로 보낸다.
- 나머지 74개 도메인 AC는 명세의 기존 자동·모의·정적 수준으로 실행하며, Uvicorn을
  지났다고 보고하지 않는다.

## 소유권

| 대상 | 경유 | 비고 |
|---|---|---|
| FastAPI·Uvicorn | ASGI HTTP·WebSocket·lifespan | 전송 어댑터만 담당 |
| 기능 계약 | `src.shared` 포트와 `src/application.py` 조립 | 기능 폴더의 판정 로직은 변경하지 않음 |

이 폴더는 기능별 REST 판정 규칙, 검색·Worker·Sync 내부 구현을 소유하지 않는다.
