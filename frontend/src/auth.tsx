import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { apiClient, clearToken, getToken, setToken, setUnauthorizedHandler } from "./api";
import type { User } from "./types";
import { Loading } from "./ui";

type AuthValue = { user: User | null; ready: boolean; login: (email: string, password: string) => Promise<void>; logout: () => Promise<void>; refresh: () => Promise<void> };
const AuthContext = createContext<AuthValue | undefined>(undefined);

export const isAdmin = (user: User | null) => Boolean(user?.roles.includes("ADMIN"));
export const canAccess = (user: User | null, adminOnly: boolean) => Boolean(user && (!adminOnly || isAdmin(user)));

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const navigate = useNavigate();
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const refresh = useCallback(async () => { const me = await apiClient.get<User>("/auth/me"); setUser(me ?? null); }, []);
  useEffect(() => {
    setUnauthorizedHandler(() => { setUser(null); navigate("/login", { replace: true }); });
    if (!getToken()) { setReady(true); return () => setUnauthorizedHandler(); }
    refresh().catch(() => clearToken()).finally(() => setReady(true));
    return () => setUnauthorizedHandler();
  }, [navigate, refresh]);
  const value = useMemo<AuthValue>(() => ({
    user, ready, refresh,
    login: async (email, password) => { const data = await apiClient.post<{ accessToken: string }>("/auth/login", { email, password }, false); if (!data?.accessToken) throw new Error("로그인 토큰이 없습니다."); setToken(data.accessToken); await refresh(); },
    logout: async () => { try { await apiClient.post("/auth/logout"); } finally { clearToken(); setUser(null); navigate("/login", { replace: true }); } },
  }), [navigate, ready, refresh, user]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
export const useAuth = () => { const value = useContext(AuthContext); if (!value) throw new Error("AuthProvider가 필요합니다."); return value; };

export function Guard({ admin = false, children }: { admin?: boolean; children: React.ReactNode }) {
  const { user, ready } = useAuth();
  const location = useLocation();
  if (!ready) return <Loading label="세션을 확인하는 중…" />;
  if (!user) return <NavigateLogin from={location.pathname} />;
  if (admin && !isAdmin(user)) return <section className="notice error" role="alert">권한이 없습니다.</section>;
  return <>{children}</>;
}
function NavigateLogin({ from }: { from: string }) { const navigate = useNavigate(); useEffect(() => { navigate("/login", { replace: true, state: { from } }); }, [from, navigate]); return <Loading />; }
