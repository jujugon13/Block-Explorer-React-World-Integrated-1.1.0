# 동기화·정합성

도메인 변경 Outbox의 멱등 발행, 리스 기반 전달, 재시도·회수와 정합성 이슈 운영을 담당한다.

## 담당 명세

| 명세 절 | 내용 |
|---|---|
| `specs/02-interfaces.md` §5.1.6 FR-SYNC | 관리자 동기화 API |
| `specs/04-states.md` §8.7~8.8 | 이벤트·정합성 이슈 상태 전이 |
| `specs/05-behavior-normal.md` §9.4 | 발행 멱등성과 처리 핸들러 계약 |
| `specs/07-concurrency-and-retry.md` §11.1, §12 | 획득 순서·리스·재시도 |
| `specs/09-recovery-and-side-effects.md` §15.1 | 만료 이벤트 회수 |
| `specs/15-implementation-contract-addendum.md` A1 | 관리자 API 최소 응답 필드 |

## 담당 acceptance criteria

| 범위 | ID |
|---|---|
| 발행·멱등 | AC-SYNC-001, AC-SYNC-002, AC-SYNC-003 |
| 전달·재시도 | AC-SYNC-004, AC-SYNC-005, AC-SYNC-006, AC-SYNC-007, AC-SYNC-009 |
| 정합성 이슈 | AC-SYNC-008 |
| 후보 S3 객체 정합성 | AC-SYNC-010 |

## 공유 경계

- 도메인 부작용과 이벤트 완료는 `src.shared.SyncUnitOfWork`가 같은 관계형 DB 커밋으로 묶는다.
- 문서·권한 변경은 `SyncService.transaction()`에 참여해 원장과 Outbox를 함께 롤백할 수 있다.
- `SyncHandlerRegistry`가 5개 이벤트 종류를 명시적으로 처리하고 `SyncDispatcher`가 역할 설정에 따라 폴링·회수·정합성 tick을 실행한다.
- `register_sync_routes`가 `FR-SYNC-001~007` 관리자 경로를 등록한다.
- U-11 중 후보 S3 객체의 `ORPHANED_DATA` 규칙은 `src.shared.ReconciliationDetector`로 주입한다.
  그 밖의 확인되지 않은 이슈 판정 규칙은 만들지 않는다.

## 이 폴더가 책임지지 않는 것

- 문서·권한·색인 원장 자체와 관계형 DB 드라이버 구현
- 스케줄러 스레드 풀 구성과 운영 대시보드 push
