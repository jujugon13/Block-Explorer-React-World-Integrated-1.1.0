# 검색 파이프라인

`specs/pipeline/search.pipeline.json`을 읽어 단계 순서·실행 조건·추적 이름을 해석하고 단계 ID와 같은 모듈을 동적으로 실행한다.

## 담당 명세

| 명세 절 | 내용 |
|---|---|
| `specs/14-search-and-rag.md` S1~S4 | 검색 인터페이스·스키마·16단계 순서·점수 |
| `specs/14-search-and-rag.md` S10~S13 | 캐시·오버라이드·오류·외부 호출 정책 |
| `specs/pipeline/search.pipeline.json` | 순서·조건·추적 이름의 단일 실행 정의 |
| `specs/pipeline/search.implementation.json` | S2/D-7 충돌을 해소한 요청·캐시 설정 보정 |

## 담당 acceptance criteria

8단계에서 아래 검색 동작을 구현하고 검증한다.

| 범위 | ID |
|---|---|
| 검색 파이프라인 | AC-RS-05, AC-RS-06, AC-RS-07, AC-RS-08, AC-RS-20, AC-RS-21, AC-RS-22, AC-RS-24, AC-RS-25, AC-RS-26, AC-RS-27, AC-RS-28, AC-RS-29, AC-RS-30, AC-RS-31, AC-RS-32, AC-RS-33, AC-RS-34, AC-RS-35, AC-RS-36 |

## 의존

| 대상 | 경유 | 비고 |
|---|---|---|
| 실행 정의 | JSON 파일 | 코드에 단계 목록을 두지 않음 |
| 단계 구현 | `src.search.steps.<단계 ID>` | 해석기가 동적 import |
| 캐시·벡터·키워드·LLM·이력 | `src.shared` 프로토콜 | 공급자 SDK를 직접 참조하지 않음 |
| 운영 대시보드 | `OpsSearchSnapshotSource` | 요청 시각만 투영하며 질의·답변 원문은 제외 |
| 권한·색인 상태 | `src.shared.PermissionReader`, `IndexCatalog` | 사전 필터와 라이브 재검증 모두 필수 |

정수 문서 원장 ID는 검색 경계에서 안정적인 UUID 문자열로 투영하며, 권한 판정은 같은 UUID를
다시 원장 ID로 해석한다. 인메모리 색인에는 `IndexVectorSearcher`를 사용하고 키워드 엔진은
배포 환경의 shared 포트 구현을 주입한다.

## 실행 경계

- 16개 단계 파일은 자기 단계 로직만 담당하며 순서·조건·추적 이름을 알지 못한다.
- 해석기가 JSON 정의를 정렬·조건 판정하고 추적을 구성한다.
- 일반 검색은 요청자별 캐시와 적중 시 권한 재검증을 사용하고, 디버그 검색만 추적을 반환한다.
- 생성은 외부 API 동기 호출이며 전체 25초 데드라인을 넘으면 결과만 유지하고 답변을 비운다.
