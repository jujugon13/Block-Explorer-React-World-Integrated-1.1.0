# PostgreSQL 어댑터

PostgreSQL 18.3에 대한 연결·마이그레이션·관계형 원장·UoW와 잠금 동작을 제공한다.

## 담당 명세

| 명세 절 | 내용 |
|---|---|
| `specs/07-concurrency-and-retry.md` §11~12 | 획득 우선순위·리스·잠금·재시도 |
| `specs/09-recovery-and-side-effects.md` §15~16 | 재시작·복구·DB 커밋 부작용 |
| `specs/10-nfr-and-security.md` §17~18 | 트랜잭션 경계·동시성·비밀정보 |

## 담당 acceptance criteria

직접 소유 없음. F17의 `acceptance_ids`는 빈 배열이며 기존 AC를 통합시험으로 재검증한다.

## 의존

| 대상 | 경유 | 비고 |
|---|---|---|
| PostgreSQL 18.3 | psycopg 3 | 접속정보는 환경변수로만 주입 |
| 기능 원장 | `src.shared` 포트 | 도메인 판정은 유지하고 상태 보관만 주입 |

## 이 폴더가 책임지지 않는 것

- 기능별 상태 전이와 HTTP 계약
- 오브젝트 저장소·AI·HTTP 어댑터
