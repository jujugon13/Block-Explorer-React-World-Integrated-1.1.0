# VectorShelf

VectorShelf는 문서 CRUD, 버전 관리, 권한 제어, 자동 임베딩, 하이브리드 검색과 RAG를 하나의 서비스로 제공하는 고가용성 문서 지식관리 플랫폼입니다. 백엔드는 Python/FastAPI, 프론트엔드는 React/TypeScript로 구성되어 있습니다.

## 주요 기능

- PDF, DOCX, TXT, Markdown 문서 업로드·수정·논리 삭제와 버전 관리
- 업로드된 문서의 자동 파싱·청킹·OpenAI 임베딩
- PostgreSQL FTS와 pgvector를 결합한 하이브리드 검색
- OpenAI 답변 생성과 로컬 CrossEncoder 리랭킹
- 사용자·부서·역할 기반 문서/컬렉션 권한 제어
- Outbox, 작업 리스, 재시도와 장애 후 작업 회수
- MCP 검색 도구, 운영 대시보드, WebSocket/STOMP 알림
- React 기반 검색·문서·컬렉션·권한·운영 화면

## 실행 구성

| 구성요소 | 요구사항 |
| --- | --- |
| API | Python 3.12, FastAPI, Uvicorn |
| 원장·검색 | PostgreSQL **18.3**, pgvector **0.8.1**, `READ COMMITTED` |
| 캐시 | Redis 7 |
| AI | OpenAI `text-embedding-3-small`, `gpt-4.1-mini` |
| 리랭커 | `dragonkue/bge-reranker-v2-m3-ko` |
| 파일 저장소 | 로컬 파일시스템(기본) 또는 AWS S3 |
| 웹 UI | Node.js 20+, React 19, Vite 7 |

PostgreSQL과 pgvector 버전은 시작 시 정확히 검사합니다. 다른 버전이면 애플리케이션이 실패 종료합니다. `compose.yml`은 개발용 Redis만 실행하며 PostgreSQL, API, 프론트엔드를 포함하는 전체 배포 스택은 아닙니다.

## 빠른 시작

### 1. 백엔드 설치

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

Windows PowerShell에서는 가상환경을 다음과 같이 활성화합니다.

```powershell
.\.venv\Scripts\Activate.ps1
Copy-Item .env.example .env
```

`.env`에서 최소한 다음 값을 실제 배포 환경에 맞게 바꿉니다.

- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`
- `REDIS_PASSWORD`, `REDIS_URL`
- `OPENAI_API_KEY`
- `VECTORSHELF_JWT__SECRET`

JWT 비밀값은 다음처럼 생성할 수 있습니다.

```sh
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### 2. Redis와 데이터베이스 준비

```sh
docker compose up -d redis
python -m src.infra.postgres.preflight
python -m src.infra.postgres.migrate
```

마이그레이션은 애플리케이션 시작 시에도 자동 적용되며, 이미 적용된 파일은 체크섬을 확인한 뒤 건너뜁니다.

새 데이터베이스에는 기본 계정이나 비밀번호를 만들지 않습니다. 회원가입 전에 운영자가 최소 부서와 역할 원장을 준비해야 합니다.

```sql
INSERT INTO departments (name, status)
VALUES ('Default', 'ACTIVE');

INSERT INTO roles (role_code, name, status)
VALUES ('USER', 'User', 'ACTIVE'), ('ADMIN', 'Administrator', 'ACTIVE')
ON CONFLICT (role_code) DO NOTHING;
```

첫 사용자가 가입한 뒤 그 사용자를 최초 관리자로 승인할 때만 다음 초기화 쿼리를 실행합니다. 이메일은 실제 가입 계정으로 바꾸십시오.

```sql
INSERT INTO user_roles (user_id, role_code, granted_by_user_id, granted_at)
SELECT user_id, 'ADMIN', user_id, CURRENT_TIMESTAMP
FROM users
WHERE email = 'admin@example.com'
ON CONFLICT (user_id, role_code) DO NOTHING;
```

### 3. API 실행

```sh
python -m src
```

기본 주소는 `http://127.0.0.1:8080`입니다. 시작 과정에서 PostgreSQL 마이그레이션·기능 사전점검, Redis 구성, OpenAI 구성과 저장소 구성을 검증합니다.

### 4. 프론트엔드 실행

별도 터미널에서 실행합니다.

```sh
cd frontend
npm ci
npm run dev
```

브라우저에서 `http://localhost:5173`을 엽니다. 개발 서버는 기본적으로 `http://127.0.0.1:8080`의 API와 WebSocket을 프록시합니다. 다른 백엔드를 사용하려면 `VITE_BACKEND_URL`을 설정하십시오.

## 운영 배포

프론트엔드 정적 파일을 빌드합니다.

```sh
cd frontend
npm ci
npm run build
```

생성된 `frontend/dist/`를 웹 서버로 제공하고, [frontend/nginx/default.conf](frontend/nginx/default.conf)를 참고해 API와 `/ws`를 같은 출처로 reverse proxy합니다. API는 비밀값을 저장소에 커밋하지 말고 배포 플랫폼의 환경변수 또는 비밀 저장소로 주입해 실행하십시오.

운영 웹 주소는 `CORS_ALLOWED_ORIGINS`에 `https://host[:port]` 형식으로 설정합니다. 여러 주소는 쉼표로 구분하며, 백엔드 CORS와 WebSocket 검사가 동일한 목록을 사용합니다.

S3를 사용할 때는 `.env`에서 `VECTORSHELF_STORAGE__TYPE=s3`로 바꾸고 `S3_BUCKET`, AWS 표준 자격증명과 리전을 설정합니다. 버킷 생성과 정책 구성은 애플리케이션 밖에서 수행합니다.

## 테스트

백엔드의 기본 게이트는 외부 RDS/S3에 장애를 일으키지 않는 비파괴 검사입니다.

```sh
PYTHONDONTWRITEBYTECODE=1 bash tools/gate.sh src 17
```

프론트엔드는 다음 순서로 확인합니다.

```sh
cd frontend
npm ci
npm run typecheck
npm run lint
npm test
npm run build
```

`tools/stage16_external_gate.sh`는 승인된 테스트용 RDS/S3를 실제로 재부팅하거나 failover할 수 있습니다. 대상 검증과 명시적 승인 환경변수 없이 실행하지 마십시오.

## 저장소 구조

```text
src/        기능별 백엔드와 인프라 어댑터
frontend/   React/Vite SPA와 Nginx 예시
specs/      외부 동작 명세와 acceptance criteria
tools/      레이아웃·커버리지·회귀·장애시험 도구
templates/  기능 문서 템플릿
```

상세 구현 범위와 검증 결과는 [IMPLEMENTATION.md](IMPLEMENTATION.md), [WORK_STATUS.md](WORK_STATUS.md), 인터페이스 계약은 [specs/](specs/)에서 확인할 수 있습니다.

## 보안

- `.env`, 클라우드 자격증명, API 키, 데이터베이스 비밀번호를 커밋하지 마십시오.
- `5432`, `6379`, `8008` 같은 내부 포트를 인터넷 전체에 공개하지 마십시오.
- `data/`에는 업로드 문서가 저장될 수 있으므로 저장소에 포함하지 마십시오.
- 공개 전 `git status --ignored`로 제외 파일을 다시 확인하십시오.
