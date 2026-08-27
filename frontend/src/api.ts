export const TOKEN_KEY = "vectorshelf.jwt";

export type ApiErrorBody = {
  success: false;
  status: number;
  code: string;
  message: string;
  method: string;
  path: string;
  timestamp: string;
};

type SuccessEnvelope<T> = { success: true; status: number; timestamp: string; data?: T };
export type SearchResult = { chunk_id: string; document_id: string; content: string; score: number; metadata: Record<string, unknown> | null };
export type SearchResponse = { query: string; answer: string; results: SearchResult[] };

export class ApiError extends Error {
  constructor(public readonly body: ApiErrorBody, public readonly response?: Response) {
    super(body.message);
    this.name = "ApiError";
  }
}

let onUnauthorized: (() => void) | undefined;
export const setUnauthorizedHandler = (handler?: () => void) => { onUnauthorized = handler; };
export const getToken = () => sessionStorage.getItem(TOKEN_KEY);
export const setToken = (token: string) => sessionStorage.setItem(TOKEN_KEY, token);
export const clearToken = () => sessionStorage.removeItem(TOKEN_KEY);

type RequestOptions = Omit<RequestInit, "body" | "headers"> & {
  body?: BodyInit | Record<string, unknown>;
  auth?: boolean;
  headers?: HeadersInit;
};

function errorBody(response: Response, data: unknown): ApiErrorBody {
  if (data && typeof data === "object" && (data as Partial<ApiErrorBody>).success === false) return data as ApiErrorBody;
  return { success: false, status: response.status, code: "HTTP_ERROR", message: response.statusText || "요청을 처리할 수 없습니다.", method: "", path: "", timestamp: "" };
}

function headersFor(options: RequestOptions): Headers {
  const headers = new Headers(options.headers);
  const token = getToken();
  if (options.auth !== false && token) headers.set("Authorization", `Bearer ${token}`);
  if (options.body && !(options.body instanceof FormData) && typeof options.body === "object" && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  return headers;
}

async function responseError(response: Response): Promise<never> {
  let data: unknown;
  try { data = await response.json(); } catch { data = undefined; }
  const body = errorBody(response, data);
  if (response.status === 401) { clearToken(); onUnauthorized?.(); }
  throw new ApiError(body, response);
}

async function call(path: string, options: RequestOptions = {}): Promise<Response> {
  const body = options.body;
  const response = await fetch(path, {
    ...options,
    headers: headersFor(options),
    body: body && !(body instanceof FormData) && typeof body === "object" ? JSON.stringify(body) : body,
  });
  if (!response.ok) return responseError(response);
  return response;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T | undefined> {
  const response = await call(path, options);
  if (response.status === 204) return undefined;
  const envelope = await response.json() as SuccessEnvelope<T>;
  if (!envelope.success) throw new ApiError(errorBody(response, envelope), response);
  return envelope.data;
}

export const apiClient = {
  get: <T>(path: string, auth = true) => request<T>(path, { auth }),
  post: <T>(path: string, body?: BodyInit | Record<string, unknown>, auth = true) => request<T>(path, { method: "POST", body, auth }),
  patch: <T>(path: string, body: Record<string, unknown>) => request<T>(path, { method: "PATCH", body }),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
  blob: async (path: string): Promise<{ blob: Blob; contentType: string; filename: string | undefined }> => {
    const response = await call(path);
    const contentDisposition = response.headers.get("Content-Disposition") || "";
    const filename = /filename\*=UTF-8''([^;]+)/i.exec(contentDisposition)?.[1];
    return { blob: await response.blob(), contentType: response.headers.get("Content-Type") || "application/octet-stream", filename: filename ? decodeURIComponent(filename) : undefined };
  },
  search: async (body: Record<string, unknown>): Promise<{ data: SearchResponse; cache: string | null }> => {
    const response = await call("/api/search", { method: "POST", body });
    return { data: await response.json() as SearchResponse, cache: response.headers.get("X-Cache") };
  },
};

export function uploadDocument(path: string, values: { file: File; title?: string; description?: string; visibility?: string }) {
  const form = new FormData();
  form.append("file", values.file);
  if (values.title !== undefined) form.append("title", values.title);
  if (values.description !== undefined) form.append("description", values.description);
  if (values.visibility !== undefined) form.append("visibility", values.visibility);
  return apiClient.post<Record<string, unknown>>(path, form);
}

export const messageFor = (error: unknown) => {
  if (!(error instanceof ApiError)) return "네트워크 연결 또는 서버 상태를 확인한 후 다시 시도해 주세요.";
  if (error.body.status === 403) return "권한이 없습니다.";
  if (error.body.status === 404 && error.body.path.startsWith("/api/documents/")) return "문서를 찾을 수 없거나 접근할 수 없습니다.";
  if (error.body.status === 422) return "문서를 처리할 수 없습니다. 파일 형식과 내용을 확인해 주세요.";
  if (error.body.status === 429) return "요청 한도를 초과했습니다. 자동으로 재시도하지 않습니다.";
  return error.message;
};
