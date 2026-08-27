import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, TOKEN_KEY, apiClient, getToken, setToken, uploadDocument } from "./api";

const store = new Map<string, string>();
Object.defineProperty(globalThis, "sessionStorage", { value: { getItem: (key: string) => store.get(key) ?? null, setItem: (key: string, value: string) => store.set(key, value), removeItem: (key: string) => store.delete(key) } });

describe("apiClient", () => {
  beforeEach(() => { store.clear(); vi.restoreAllMocks(); });
  it("unwraps a normal success envelope and handles 204", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ success: true, status: 200, timestamp: "x", data: { id: 1 } }))).mockResolvedValueOnce(new Response(null, { status: 204 })));
    await expect(apiClient.get<{ id: number }>("/items")).resolves.toEqual({ id: 1 });
    await expect(apiClient.delete("/items/1")).resolves.toBeUndefined();
  });
  it("reads raw search success and typed error envelopes", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ query: "q", answer: "", results: [] }), { headers: { "X-Cache": "HIT" } })).mockResolvedValueOnce(new Response(JSON.stringify({ success: false, status: 403, code: "ROLE-002", message: "권한이 없습니다.", method: "GET", path: "/x", timestamp: "x" }), { status: 403 })));
    await expect(apiClient.search({ query: "q" })).resolves.toMatchObject({ data: { answer: "", results: [] }, cache: "HIT" });
    await expect(apiClient.get("/x")).rejects.toBeInstanceOf(ApiError);
  });
  it("keeps FormData content type browser-owned and clears a 401 token", async () => {
    setToken("jwt");
    const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ success: true, status: 201, timestamp: "x", data: {} }), { status: 201 })).mockResolvedValueOnce(new Response(JSON.stringify({ success: false, status: 401, code: "COMMON-007", message: "로그인이 필요합니다.", method: "GET", path: "/x", timestamp: "x" }), { status: 401 }));
    vi.stubGlobal("fetch", fetch);
    await uploadDocument("/api/documents", { file: new File(["x"], "x.txt"), title: "x", visibility: "PRIVATE" });
    expect(new Headers(fetch.mock.calls[0][1].headers).has("Content-Type")).toBe(false);
    await expect(apiClient.get("/x")).rejects.toBeInstanceOf(ApiError);
    expect(getToken()).toBeNull(); expect(store.has(TOKEN_KEY)).toBe(false);
  });
});
