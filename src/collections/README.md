# 컬렉션

계층형 컬렉션과 문서 매핑, 소유자 전용 변경 및 연쇄 논리 삭제를 제공한다.

## 담당 명세

| 명세 절 | 내용 |
|---|---|
| `specs/02-interfaces.md` §5.1.3 | 컬렉션 API |
| `specs/15-implementation-contract-addendum.md` A3 | 컬렉션·권한 REST 상태와 정확 응답 projection |

## 담당 acceptance criteria

| ID | 요약 | 검증 수준 | 테스트 |
|---|---|---|---|
| AC-COL-001 | 위임된 관리 권한은 삭제 소유자 검사를 대체하지 않음 | 자동 | `test_AC_COL_001_*` |
| AC-COL-002 | 다른 소유자의 하위까지 연쇄 삭제 | 자동 | `test_AC_COL_002_*` |
| AC-COL-003 | 중복 문서 매핑 거부 | 자동 | `test_AC_COL_003_*` |
| AC-COL-004 | 자식 조회는 직계 한 단계만 반환 | 자동 | `test_AC_COL_004_*` |
| AC-COL-005 | 생성 공개 범위 기본값은 PRIVATE | 자동 | `test_AC_COL_005_*` |
| AC-COL-006 | 수정 시 COLLECTION 공개 범위 거부 | 자동 | `test_AC_COL_006_*` |

## 의존

| 대상 | 경유 | 비고 |
|---|---|---|
| 문서·권한 | `src.shared` 접근 계약 | 기능 폴더를 직접 참조하지 않음 |

## 이 폴더가 책임지지 않는 것

- 문서 객체 저장과 삭제
- 권한 원장 및 접근 캐시의 내부 구현
