# 운영 대시보드

관계형 DB 스냅숏을 운영 요약으로 집계하고, 커밋된 상태 변경을 WebSocket/STOMP 전송 포트로 알린다.

## 담당 명세

| 명세 절 | 내용 |
|---|---|
| `specs/02-interfaces.md` §5.1.6 `FR-OPS` | ADMIN 운영 REST API |
| `specs/02-interfaces.md` §5.2 `FR-SYS-013` | 대시보드 STOMP 목적지 계약 |
| `specs/03-schemas-and-defaults.md` §6.5 | 운영 요약 응답 스키마 |
| `specs/07-concurrency-and-retry.md` §12.2 | push 변경 플래그·중복 실행 방지 |

## 담당 acceptance criteria

| ID | 요약 | 검증 수준 | 테스트 |
|---|---|---|---|
| AC-OPS-001 | 일반 사용자의 요약 조회 거부 | 자동 | `test_AC_OPS_001_user_cannot_read_summary` |
| AC-OPS-002 | 완료 작업이 없으면 평균은 null | 자동 | `test_AC_OPS_002_empty_completed_jobs_have_null_average` |
| AC-OPS-003 | 일반 사용자 구독 거부 | 모의 | `test_AC_OPS_003_user_subscription_is_rejected` |
| AC-OPS-004 | 클라이언트 전송은 관리자도 거부 | 모의 | `test_AC_OPS_004_client_send_is_always_rejected` |
| AC-OPS-005 | 한 주기 안의 변경을 한 번만 push | 모의 | `test_AC_OPS_005_transitions_are_coalesced_per_tick` |
| AC-OPS-006 | 롤백된 변경은 push하지 않음 | 모의 | `test_AC_OPS_006_rolled_back_transition_is_not_pushed` |

## 외부 포트

| 대상 | 인터페이스 | 비고 |
|---|---|---|
| 기능별 상태 원장 | `CompositeOpsSnapshotReader` | 문서·색인·검색 source를 같은 조회 시점의 immutable 스냅숏으로 결합 |
| STOMP/WebSocket 브로커 | `DashboardBrokerPublisher` | 인메모리 구독만 유지하며 재전송하지 않음 |

검색·답변 push는 D-1에 따라 제거됐으며, 대시보드 신호를 받은 클라이언트는
`GET /admin/dashboard/summary`를 다시 조회한다.
