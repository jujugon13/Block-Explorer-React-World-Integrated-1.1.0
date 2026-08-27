import { useState } from "react";
import { NavLink, Navigate, Outlet, Route, Routes, useNavigate } from "react-router-dom";
import { BrowserRouter } from "react-router-dom";
import { AuthProvider, Guard, isAdmin, useAuth } from "./auth";
import { CollectionsPage, CollectionDetailPage, DashboardPage, DocumentDetailPage, DocumentsPage, JobsPage, LoginPage, McpKeysPage, SearchPage, SignupPage, SyncPage, UsersPage } from "./pages";

const primary = [["/search", "검색"], ["/documents", "문서"], ["/collections", "컬렉션"], ["/mcp-keys", "MCP 키"]] as const;
const admin = [["/admin", "운영"], ["/admin/jobs", "색인 작업"], ["/admin/sync", "동기화"], ["/admin/users", "사용자 관리"]] as const;

function Layout() {
  const { user, logout } = useAuth(); const [open, setOpen] = useState(false); const navigate = useNavigate(); const links = isAdmin(user) ? [...primary, ...admin] : primary;
  return <div className="app-shell"><button className="nav-toggle" onClick={() => setOpen(!open)} aria-expanded={open} aria-controls="sidebar">메뉴</button><aside id="sidebar" className={open ? "open" : ""}><div className="brand"><span>VS</span><strong>VectorShelf</strong></div><nav aria-label="주 메뉴">{links.map(([to, label]) => <NavLink key={to} to={to} onClick={() => setOpen(false)}>{label}</NavLink>)}</nav><footer><p>{user?.name}</p><button className="secondary" onClick={() => logout().catch(() => navigate("/login"))}>로그아웃</button></footer></aside><main className="content"><Outlet /></main></div>;
}

export function App() {
  return <BrowserRouter><AuthProvider><Routes><Route path="/login" element={<LoginPage />} /><Route path="/signup" element={<SignupPage />} /><Route element={<Guard><Layout /></Guard>}><Route path="/search" element={<SearchPage />} /><Route path="/documents" element={<DocumentsPage />} /><Route path="/documents/:documentId" element={<DocumentDetailPage />} /><Route path="/collections" element={<CollectionsPage />} /><Route path="/collections/:collectionId" element={<CollectionDetailPage />} /><Route path="/mcp-keys" element={<McpKeysPage />} /><Route path="/admin" element={<Guard admin><DashboardPage /></Guard>} /><Route path="/admin/jobs" element={<Guard admin><JobsPage /></Guard>} /><Route path="/admin/sync" element={<Guard admin><SyncPage /></Guard>} /><Route path="/admin/users" element={<Guard admin><UsersPage /></Guard>} /></Route><Route path="*" element={<Navigate to="/search" replace />} /></Routes></AuthProvider></BrowserRouter>;
}
