/**
 * 管理面 API client。
 *
 * - 只认内存里的 access token（App 内 WebView，`Authorization: Bearer`）。
 * - 浏览器只用 `__Host-ac_admin` cookie（`credentials: same-origin` 自动带）。
 * - 401：清内存 token 并通知 AuthProvider 回登录页；登录接口自身不触发全局跳转。
 * - 不缓存：管理面的状态是实时值，`cache: 'no-store'`。
 */

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

let accessToken: string | null = null;
//: Double-submit token from the login/refresh response. Not a bearer secret,
//: but losing it on reload made logout-after-refresh fail silently (review P1).
//: sessionStorage is per-tab and cleared when the tab closes.
const CSRF_STORAGE_KEY = "vade.admin.csrf";
let csrfToken: string | null = (() => {
  try {
    return sessionStorage.getItem(CSRF_STORAGE_KEY);
  } catch {
    return null;
  }
})();
const unauthorizedListeners = new Set<() => void>();

export function setAccessToken(token: string | null): void {
  accessToken = token;
  if (token === null) setCsrfToken(null);
}

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
  try {
    if (token) sessionStorage.setItem(CSRF_STORAGE_KEY, token);
    else sessionStorage.removeItem(CSRF_STORAGE_KEY);
  } catch {
    // Storage disabled: memory only, same as before.
  }
}

export function getCsrfToken(): string | null {
  return csrfToken;
}

export function getAccessToken(): string | null {
  return accessToken;
}

export function onUnauthorized(listener: () => void): () => void {
  unauthorizedListeners.add(listener);
  return () => {
    unauthorizedListeners.delete(listener);
  };
}

function emitUnauthorized(): void {
  for (const listener of unauthorizedListeners) listener();
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE';
  body?: unknown;
  signal?: AbortSignal;
  /** 不附带 Bearer（登录 challenge / login / health / 探测 cookie 会话）。 */
  anonymous?: boolean;
  /** 401 时不触发全局登出（登录页自己处理错误）。 */
  quietAuthFailure?: boolean;
}

function errorCodeFrom(payload: unknown): string | null {
  if (payload && typeof payload === 'object' && 'code' in payload) {
    const code = (payload as { code?: unknown }).code;
    if (typeof code === 'string' && code.length > 0) return code;
  }
  return null;
}

function messageFrom(payload: unknown): string | null {
  if (payload && typeof payload === 'object' && 'message' in payload) {
    const message = (payload as { message?: unknown }).message;
    if (typeof message === 'string' && message.length > 0) return message;
  }
  return null;
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text.length === 0) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? 'GET';
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  if (!options.anonymous && accessToken) headers.Authorization = `Bearer ${accessToken}`;
  if (!options.anonymous && csrfToken && method !== 'GET') headers['X-CSRF-Token'] = csrfToken;

  const response = await fetch(path, {
    method,
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    credentials: 'same-origin',
    cache: 'no-store',
    redirect: 'error',
    signal: options.signal,
  });

  const payload = await readJson(response);

  if (response.status === 401) {
    accessToken = null;
    if (!options.quietAuthFailure) emitUnauthorized();
    throw new ApiError(401, errorCodeFrom(payload) ?? 'AUTH_FAILED', 'AUTH_FAILED');
  }

  if (!response.ok) {
    const code = errorCodeFrom(payload) ?? `HTTP_${response.status}`;
    const message = messageFrom(payload) ?? `请求失败（HTTP ${response.status}）`;
    throw new ApiError(response.status, code, message);
  }

  return payload as T;
}

/** 把异常翻译成给人看的一句话；登录页自己覆盖 `AUTH_FAILED` 的文案。 */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return '密码或设备不正确';
    if (error.status === 403 && error.code === 'READ_ONLY_SESSION') {
      return '当前为只读模式，无法修改配置';
    }
    if (error.status === 429) return '尝试次数过多，请稍后再试';
    if (error.status === 404) return '目标不存在，可能已被移除';
    if (error.status >= 500) return 'core 返回了服务端错误，请稍后再试';
    return error.message;
  }
  if (error instanceof DOMException && error.name === 'AbortError') return '';
  if (error instanceof TypeError) return '无法连接 core，请检查网络与证书';
  if (error instanceof Error) return error.message;
  return '未知错误';
}
