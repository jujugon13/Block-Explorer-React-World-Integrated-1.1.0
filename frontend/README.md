# VectorShelf Frontend

React + TypeScript/Vite SPA입니다. Node 20 이상이 필요합니다.

```sh
cd frontend
npm ci
npm run dev
```

개발 서버는 `http://localhost:5173`으로 고정되며, 브라우저는 항상 상대 경로를 호출합니다. Vite가 `/api`, `/auth`, `/departments`, `/roles`, `/collections`, `/permissions`, `/admin`, `/mcp`, `/ws`를 기본 백엔드 `http://127.0.0.1:8080`으로 프록시합니다. `/ws`는 WebSocket 프록시입니다.

운영에서는 [nginx/default.conf](nginx/default.conf)처럼 SPA와 API를 같은 출처 reverse proxy로 제공해야 합니다. 현재 백엔드는 정적 SPA를 제공하지 않으며 Compose도 전체 앱 구성이 아닙니다. 운영 도메인은 백엔드 `CORS_ALLOWED_ORIGINS`에 추가해야 합니다. WebSocket 연결이 거부되면 대시보드는 30초 폴링으로 전환합니다.

```sh
npm run typecheck
npm run lint
npm run test
npm run build
```
