# 외부 AI 어댑터

확정된 OpenAI 임베딩·LLM과 로컬 CrossEncoder를 `src.shared` 포트에 연결한다.

## 담당 명세

| 명세 절 | 내용 |
|---|---|
| `specs/14-search-and-rag.md` S3·S12~S14 | 호출 순서·오류·외부 호출 정책·설정 |
| `specs/09-recovery-and-side-effects.md` §16.3 | 네트워크 부작용 |
| `specs/10-nfr-and-security.md` §18.3 | API 비밀정보 보호 |

## 담당 acceptance criteria

직접 소유 없음. F17의 `acceptance_ids`는 빈 배열이며 기존 AC를 통합시험으로 재검증한다.

## 조합

| 역할 | 구현 |
|---|---|
| 임베딩 | OpenAI `text-embedding-3-small`, `dimensions=1536`, provider `OPENAI` |
| LLM | OpenAI `gpt-4.1-mini` |
| 리랭커 | 로컬 F32 `dragonkue/bge-reranker-v2-m3-ko`, batch 1, 동시 추론 1 |

OpenAI 키는 `OPENAI_API_KEY` 환경변수로만 읽는다. 리랭커는 첫 `score` 호출까지 모델을 로드하지 않으며 `HF_HOME` 또는 `HF_HUB_CACHE`가 설정되면 프로젝트 밖 경로만 허용한다.
