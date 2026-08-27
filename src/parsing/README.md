# 변환·청킹

PDF·DOCX·TXT·MD 원본을 경계가 있는 텍스트 구역으로 변환하고 결정적으로 청킹한다.

## 담당 명세

| 명세 절 | 내용 |
|---|---|
| `specs/03-schemas-and-defaults.md` §7.2 | 형식별 텍스트 정규화와 문서 변환 |
| `specs/05-behavior-normal.md` §9.3 FR-IDX-042 | 청크 생성 정상 경로 |
| `specs/05-behavior-normal.md` §9.3 FR-IDX-043 | 결정적 청크 계산 규칙 |

## 담당 acceptance criteria

| ID | 요약 | 검증 수준 | 테스트 |
|---|---|---|---|
| AC-DOC-050 | 비 UTF-8 TXT 거부 | 자동 | `test_AC_DOC_050_non_utf8_txt_is_rejected` |
| AC-DOC-051 | 선두 BOM 하나만 제거 | 자동 | `test_AC_DOC_051_only_one_leading_bom_is_removed` |
| AC-DOC-052 | 개행을 LF로 정규화 | 자동 | `test_AC_DOC_052_crlf_and_cr_become_lf` |
| AC-DOC-053 | 공백 문서 거부 | 자동 | `test_AC_DOC_053_whitespace_only_document_is_rejected` |
| AC-DOC-054 | 암호화 PDF 거부 | 자동 | `test_AC_DOC_054_encrypted_pdf_is_rejected` |
| AC-DOC-055 | 텍스트 없는 PDF의 OCR 필요 오류 | 자동 | `test_AC_DOC_055_scanned_pdf_without_text_is_rejected` |
| AC-DOC-056 | 대체문자 6% PDF 거부 | 자동 | `test_AC_DOC_056_pdf_replacement_ratio_above_limit_is_rejected` |
| AC-DOC-057 | 대체문자 5% PDF 허용 | 자동 | `test_AC_DOC_057_pdf_replacement_ratio_at_limit_is_allowed` |
| AC-DOC-058 | PDF 페이지 경계와 번호 보존 | 자동 | `test_AC_DOC_058_pdf_chunks_do_not_cross_pages` |
| AC-DOC-059 | DOCX 제목 문단이 새 구역 시작 | 자동 | `test_AC_DOC_059_docx_heading_starts_new_section` |
| AC-DOC-060 | DOCX 표의 셀·행 구분 보존 | 자동 | `test_AC_DOC_060_docx_table_uses_tabs_and_newlines` |
| AC-DOC-061 | 같은 입력의 청크 결과 결정성 | 자동 | `test_AC_DOC_061_chunk_indices_ranges_and_hashes_are_deterministic` |
| AC-DOC-062 | 보충 평면 문자를 코드포인트 단위로 처리 | 자동 | `test_AC_DOC_062_supplementary_character_is_not_split` |
| AC-DOC-063 | 겹침만 남은 잉여 청크 방지 | 자동 | `test_AC_DOC_063_exact_multiple_has_no_overlap_only_chunk` |
| AC-DOC-064 | 전진 불가 청크 설정으로 기동 실패 | 정적 | `test_AC_DOC_064_overlap_at_least_chunk_size_fails_validation` |

## 의존

| 대상 | 경유 | 비고 |
|---|---|---|
| 외부 시스템 | 없음 | 원본 바이트를 메모리에서 처리 |
| PDF 읽기 | `pypdf 6.10.0` | 네트워크 호출 없음 |

## 이 폴더가 책임지지 않는 것

- 원본 저장소 조회와 청크 집합·상태·이벤트의 원자 저장
- 색인 작업 소유권과 재요청 판정
- OCR 처리
