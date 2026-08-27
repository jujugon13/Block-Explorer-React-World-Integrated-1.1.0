# S3 오브젝트 저장소 어댑터

AWS S3를 `src.shared` 저장소 포트에 맞춰 연결한다.

## 담당 명세

| 명세 절 | 내용 |
|---|---|
| `specs/01-purpose-and-boundary.md` FR-SYS-004~005 | 외부 저장소 계약과 활성 저장소 선택 |
| `specs/09-recovery-and-side-effects.md` §16.2 FR-DOC-062 | 객체 저장소 부작용·오류·무결성·후보 객체 정리 |
| `specs/10-nfr-and-security.md` §18.3 | 비밀정보 저장·노출 금지 |

## 담당 acceptance criteria

직접 소유 없음. F17의 `acceptance_ids`는 빈 배열이며 기존 AC를 통합시험으로 재검증한다.

## 의존

| 대상 | 경유 | 비고 |
|---|---|---|
| AWS S3 API | boto3 | 자격증명·리전은 boto3 표준 환경변수 체인 사용 |
| 저장소 계약 | `src.shared.ObjectClient`, `src.shared.ObjectStorage`, `src.shared.ObjectInventory` | 기존 저장소 하위 어댑터 직접 import 금지 |

버킷 이름은 `S3_BUCKET`으로만 주입한다. 자격증명과 리전 값은 코드에서 읽거나 출력하지 않고 boto3가
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`에서 직접 해석한다. 버킷은 외부에서
미리 생성되어 있어야 하며, 조합 시 사전 점검 실패는 기동 실패로 처리한다.

`orphan_recovery_check.py`는 격리된 후보 prefix에 객체를 쓴 자식 프로세스를 실제
`taskkill /F`로 종료한 뒤, 정합성 DRY_RUN·REPAIR에서 미참조 후보만 삭제되고 DB 참조 객체가
보존되는지 검증한다. 자격증명 값은 출력하지 않는다.

## 이 폴더가 책임지지 않는 것

- 버킷 생성
- 문서·버전 트랜잭션
- 자격증명 보관
