# 인증·토큰

회원가입·로그인·내 정보·로그아웃 REST 등록, 서명 토큰, 무효화 목록과 30초 역할 캐시를 담당한다.

## 담당 명세

| 명세 절 | 내용 |
|---|---|
| `specs/02-interfaces.md` §5.1.1 | 인증 REST 인터페이스 |
| `specs/03-schemas-and-defaults.md` §6.2 | 요청·응답·토큰 스키마 |
| `specs/05-behavior-normal.md` §9.1 | 가입·로그인·로그아웃·인가 순서 |
| `specs/10-nfr-and-security.md` §18.1 | 무상태 토큰과 무효화 목록 |
| `specs/10-nfr-and-security.md` §18.2 | 경로·역할 인가 |

## 담당 acceptance criteria

| ID | 요약 | 검증 수준 | 테스트 |
|---|---|---|---|
| AC-AUTH-001 | 가입 시 USER 역할 부여 | 자동 | `test_AC_AUTH_001_valid_signup_assigns_USER` |
| AC-AUTH-002 | 중복 이메일 거부 | 자동 | `test_AC_AUTH_002_duplicate_email_is_409_before_later_checks` |
| AC-AUTH-003 | 이메일·이름과 같은 비밀번호 거부 | 자동 | `test_AC_AUTH_003_password_equal_to_email_or_name_is_rejected` |
| AC-AUTH-004 | 비밀번호 12~64자 경계 | 자동 | `test_AC_AUTH_004_password_length_11_12_64_65_boundaries` |
| AC-AUTH-005 | 비활성 부서 거부 | 자동 | `test_AC_AUTH_005_inactive_department_is_DEPT_001_400` |
| AC-AUTH-006 | 비활성 계정의 정확한 비밀번호 | 자동 | `test_AC_AUTH_006_inactive_user_with_correct_password_is_403` |
| AC-AUTH-007 | 비밀번호 검증 우선순위 | 자동 | `test_AC_AUTH_007_bad_password_is_401_before_inactive_status` |
| AC-AUTH-008 | 성공 로그인 시각 갱신 | 자동 | `test_AC_AUTH_008_successful_login_updates_last_login_and_me` |
| AC-AUTH-009 | 실패 로그인 시각 불변 | 자동 | `test_AC_AUTH_009_failed_login_does_not_update_last_login` |
| AC-AUTH-010 | 손상 토큰 로그아웃 무효과 | 자동 | `test_AC_AUTH_010_corrupt_token_logout_is_204_without_revocation` |
| AC-AUTH-011 | 로그아웃 토큰 차단 | 모의 | `test_AC_AUTH_011_logout_revokes_token_for_protected_api` |
| AC-AUTH-012 | 캐시 장애 시 fail-open | 모의 | `test_AC_AUTH_012_cache_outage_is_fail_open_for_logged_out_token` |
| AC-AUTH-013 | 역할 회수 최대 30초 반영 | 모의 | `test_AC_AUTH_013_role_revocation_is_visible_no_later_than_30_seconds` |

## 의존

| 대상 | 경유 | 비고 |
|---|---|---|
| 계정·부서·역할 원장 | `src/shared.IdentityDirectory` | 외부 DB 드라이버 직접 사용 없음 |
| TTL 키-값 저장소 | `src/shared.CacheStore` | 장애 시 인증 경로 fail-open |
| 공통 HTTP·오류 봉투 | `src/platform`, `src/shared` | 고정 코드·문구 유지 |

`register_auth_routes(app, auth)`는 duck-typed 라우터에 `/auth/signup`, `/auth/login`,
`/auth/me`, `/auth/logout`을 등록한다. JSON 객체·필수 필드 검증 실패는 `COMMON-002`로 통일한다.

## 이 폴더가 책임지지 않는 것

- 계정·부서·역할 원장의 저장 구현과 관리자 변경 API
- 문서·컬렉션별 도메인 권한 판정
- WebSocket 접속 프레임 인증
