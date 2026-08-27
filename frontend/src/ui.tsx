import { useEffect, useRef } from "react";
import { ApiError, messageFor } from "./api";

export function Notice({ error, retry }: { error: unknown; retry?: () => void }) {
  const status = error instanceof ApiError ? error.body.status : undefined;
  return <div className="notice error" role="alert"><p>{messageFor(error)}</p>{status === 409 && <p>서버 상태가 변경되었을 수 있습니다. 다시 조회해 주세요.</p>}{(status === 503 || retry) && retry && <button onClick={retry}>다시 시도</button>}</div>;
}

export function Loading({ label = "불러오는 중…" }: { label?: string }) { return <p className="loading" aria-live="polite">{label}</p>; }
export function Empty({ children }: { children: React.ReactNode }) { return <p className="empty">{children}</p>; }
export function Status({ value }: { value: string | null | undefined }) { return <span className={`status status-${(value || "unknown").toLowerCase()}`}>{value || "—"}</span>; }
export function PageTitle({ title, actions, children }: { title: string; actions?: React.ReactNode; children?: React.ReactNode }) { return <header className="page-title"><div><h1>{title}</h1>{children}</div>{actions && <div className="page-actions">{actions}</div>}</header>; }

export function Dialog({ title, children, onClose }: { title: string; children: React.ReactNode; onClose: () => void }) {
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    close.current?.focus();
    const key = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [onClose]);
  return <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}><section className="dialog" role="dialog" aria-modal="true" aria-labelledby="dialog-title"><header><h2 id="dialog-title">{title}</h2><button ref={close} className="icon-button" onClick={onClose} aria-label="닫기">×</button></header>{children}</section></div>;
}

export function Pager({ page, onPage }: { page: { page: number; totalPages: number; first: boolean; last: boolean }; onPage: (page: number) => void }) {
  if (page.totalPages < 2) return null;
  return <nav className="pager" aria-label="페이지 이동"><button disabled={page.first} onClick={() => onPage(page.page - 1)}>이전</button><span>{page.page + 1} / {page.totalPages}</span><button disabled={page.last} onClick={() => onPage(page.page + 1)}>다음</button></nav>;
}

export function usePolling(active: boolean, action: () => void, milliseconds = 5000) {
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(action, milliseconds);
    return () => window.clearInterval(id);
  }, [active, action, milliseconds]);
}
