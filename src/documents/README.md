# 문서 관리

원본 업로드, 문서·버전 원장, 조회, 메타데이터 변경과 논리 삭제를 제공한다.

## 담당 명세

| 명세 절 | 내용 |
|---|---|
| `specs/02-interfaces.md` §5.1.2 | 문서 REST 계약 |
| `specs/03-schemas-and-defaults.md` §6.3 | 문서 요청·응답 스키마 |
| `specs/03-schemas-and-defaults.md` §7.2 | 파일·입력 정규화 |
| `specs/05-behavior-normal.md` §9.2 | 최초·새 버전 업로드 |
| `specs/08-limits-and-idempotency.md` §14 | 파일 객체 재사용과 중복 요청 |
| `specs/09-recovery-and-side-effects.md` §16.1 | 문서 원장 부작용과 논리 삭제 |

## 담당 acceptance criteria

| ID 범위 | 내용 | 검증 수준 | 테스트 |
|---|---|---|---|
| AC-DOC-001~010, 014 | 업로드·파일 검증 | 자동 | `test_upload.py`의 `test_AC_DOC_*` |
| AC-DOC-020~028 | 버전 생성·동시성·스냅숏 | 자동 | `test_versions.py`의 `test_AC_DOC_*` |
| AC-DOC-030~036, 039~040 | 조회·본문·원본·목록 | 자동 | `test_query.py`의 `test_AC_DOC_*` |

## 의존

| 대상 | 경유 | 비고 |
|---|---|---|
| 파일 저장소 | `src.shared.ObjectStorage` | 활성 어댑터만 주입 |
| 인증 주체 | `src.shared.Principal` | 소유자 식별에 숫자 ID 사용 |

## 이 폴더가 책임지지 않는 것

- 형식별 텍스트 추출과 청크 계산
- 임베딩 생성과 검색
- 저장소 어댑터 구현
