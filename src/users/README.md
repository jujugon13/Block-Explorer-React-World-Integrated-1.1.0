# 계정·부서·역할

계정·부서·역할 원장과 공개 부서 조회, 역할 조회 및 관리자 변경 REST 등록을 담당한다.

## 담당 명세

| 명세 절 | 내용 |
|---|---|
| `specs/02-interfaces.md` §5.1.1 | 부서·역할·관리자 사용자 REST 인터페이스 |
| `specs/03-schemas-and-defaults.md` §6.2 | 계정 응답 스키마 |

## 담당 acceptance criteria

| ID | 요약 | 검증 수준 | 테스트 |
|---|---|---|---|
| AC-SYS-003 | 인증 없는 부서 목록 조회 | 자동 | `test_AC_SYS_003_departments_endpoint_is_public` |

## 의존

| 대상 | 경유 | 비고 |
|---|---|---|
| 공용 계정 레코드·오류 | `src/shared` | 관계형 DB 대역 원장 |
| 공통 응답·경로 인가 | `src/platform` | 부서 조회는 공개 경로 |

`register_user_routes(app, directory)`는 부서·역할·관리자 사용자 목록과 역할 부여·회수,
부서 변경 경로를 등록한다. 관리자 핸들러도 플랫폼과 별도로 `ADMIN`을 재검증한다.

## 이 폴더가 책임지지 않는 것

- 비밀번호 해시와 토큰 발급·검증
- 토큰 무효화 목록과 역할 캐시
- 문서·컬렉션별 권한 판정
