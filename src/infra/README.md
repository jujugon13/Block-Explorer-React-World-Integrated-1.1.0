# 배포 인프라 어댑터

외부 시스템 연결, shared 포트 구현, 배포 조합과 프로세스 수명주기 경계를 제공한다.

## 담당 명세

| 명세 절 | 내용 |
|---|---|
| `specs/01-purpose-and-boundary.md` FR-SYS-003~007 | 실행 역할·외부 구성요소·입력 채널·실행 인자 |
| `specs/07-concurrency-and-retry.md` §11~12 | 경합·리스·재시도·주기·순서 |
| `specs/08-limits-and-idempotency.md` FR-SYS-030 | 시작 시 설정 검증 |
| `specs/09-recovery-and-side-effects.md` §15~16 | 재시작·복구와 외부 부작용 |
| `specs/10-nfr-and-security.md` §17~18 | 트랜잭션·동시성·비밀정보 |
| `specs/14-search-and-rag.md` S3·S6·S12~S14 | 검색 실행·외부 호출·오류·설정 |

## 담당 acceptance criteria

F17에 새 acceptance criteria를 배정하지 않는다. 기존 191개 기준은 각 기능 폴더가 계속 소유한다.

| ID | 요약 | 검증 |
|---|---|---|
| 없음 | 기존 기능 계약을 배포 어댑터에서도 동일하게 보존 | 단계별 전체 게이트와 `IT_<영역>_<번호>_<설명>` 통합시험 |

## 내부 어댑터

| 경로 | 책임 | 구현 단계 |
|---|---|---:|
| `postgres` | psycopg 연결·순수 SQL 마이그레이션·원장·UoW·잠금·재연결 | 11 |
| `s3` | S3 클라이언트·저장소 오류 변환 | 12 |
| `ai` | OpenAI 임베딩·LLM과 로컬 CrossEncoder 리랭커 | 13 |
| `search` | pgvector·PostgreSQL FTS 검색 | 14 |
| `http` | FastAPI·uvicorn ASGI와 WebSocket 전송 | 15 |
| `cache` | Redis 호환 키-값 캐시 어댑터 | 10 |
| `settings.py` / `runtime.py` | TOML·환경 변수 설정 해석과 실제 프로세스 조립 | 10, 15 |

## 의존 경계

- 기능 폴더는 외부 드라이버·SDK·HTTP 클라이언트를 직접 사용하지 않는다.
- 이 폴더의 어댑터는 `src.shared`의 포트만 구현하거나 참조한다.
- `src.storage.local`, `src.storage.minio`, `src.storage.s3`를 직접 import하지 않는다.
- 접속 비밀은 환경변수로만 읽고 문서·로그·예외에 값을 남기지 않는다.

## 이 폴더가 책임지지 않는 것

- 기능별 상태 전이와 공개 계약
- acceptance criteria의 재정의
- 12단계 이후 어댑터의 선행 구현
