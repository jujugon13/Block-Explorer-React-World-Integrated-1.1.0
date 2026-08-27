# 플랫폼·공통

공통 HTTP 응답·오류 봉투, 경로 보안 필터, CORS와 WebSocket/STOMP 프레임 경계를 제공한다.

## 담당 명세

| 명세 절 | 내용 |
|---|---|
| `specs/03-schemas-and-defaults.md` §6.1 | 성공·오류·204 응답 봉투 |
| `specs/06-behavior-errors.md` §10.2 | 프레임워크 수준 예외 매핑 |
| manifest §18.5 / `specs/10-nfr-and-security.md` §18.3 (`FR-SYS-065`) | CORS 허용 목록과 프리플라이트 정책 |
| `specs/02-interfaces.md` §5.2 | 공개 HTTP handshake와 STOMP CONNECT 계약 |
| `specs/10-nfr-and-security.md` §18.1~18.2 (`FR-SYS-061~062`) | CONNECT 인증과 목적지 인가 |
| `specs/08-limits-and-idempotency.md` §13.1 (`FR-SYS-030`) | 기동 시 설정 경계 검증 |

## 담당 acceptance criteria

| ID | 요약 | 검증 수준 | 테스트 |
|---|---|---|---|
| AC-SYS-001 | 미인증 보호 경로의 공통 401 봉투 | 자동 | `test_AC_SYS_001_protected_path_has_401_body` |
| AC-SYS-002 | 일반 사용자의 관리자 경로 접근 거부 | 자동 | `test_AC_SYS_002_user_cannot_access_admin_path` |
| AC-SYS-004 | CONNECT 토큰 누락·손상 시 연결 거부 | 모의 | `test_AC_SYS_004_handshake_is_open_but_CONNECT_requires_native_bearer` |
| AC-SYS-005 | 허용 목록 밖 오리진의 프리플라이트 거부 | 자동 | `test_AC_SYS_005_cors_preflight_policy` |
| AC-SYS-006 | 미지원 메서드의 공통 405 봉투 | 자동 | `test_AC_SYS_006_method_and_framework_error_mapping` |
| AC-SYS-007 | 본문 검증의 첫 위반만 반환 | 자동 | `test_AC_SYS_007_only_first_body_violation_is_exposed` |
| AC-SYS-008 | null 키 생략, falsy 값·페이지 스키마 보존, 204 무본문 | 자동 | `test_AC_SYS_008_empty_success_omits_data` |
| AC-SYS-009 | JWT 비밀키 누락 시 실제 조합 기동 실패 | 정적 | `test_AC_SYS_009_missing_or_blank_jwt_secret_fails_startup` |
| AC-SYS-010 | 외부 저장소 네임스페이스 누락 시 실제 조합 기동 실패 | 정적 | `test_AC_SYS_010_external_storage_requires_bucket_namespace` |

## 의존

| 대상 | 경유 | 비고 |
|---|---|---|
| 공용 HTTP 타입·인증 주체 | `src/shared` | 외부 시스템 의존 없음 |

## 이 폴더가 책임지지 않는 것

- 토큰 발급·서명·무효화 및 역할 조회
- 문서·검색 등 도메인 API
- 호스팅 서버 자체의 RFC 6455 소켓 업그레이드 처리. `/ws/info`와 SockJS XHR fallback,
  STOMP 프레임 인증·구독·전송 거부 및 브로커 전달은 이 폴더가 제공한다.
