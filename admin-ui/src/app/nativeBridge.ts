import type { HostSession } from './host';

/**
 * App 内 WebView 的登录桥 —— **本批只留接口与注释，原生侧由 U1 实现**。
 *
 * 约定（顺序不能变）：
 * 1. 密码框由原生 UI 渲染，页面**不**渲染密码框、不接收密码（design.md §7.11 第 8 条）。
 * 2. challenge 获取、设备私钥签名、`POST /admin/api/login` 全部在原生层完成。
 * 3. 原生只把 `{access_token, expires_in}` 交给页面；`refresh_token` 留在原生钥匙串。
 * 4. 页面只把 token 存在内存变量里 —— `localStorage` / `sessionStorage` 一概不写
 *    （§7.11 第 5 条要求 DOM storage 不持久化）。
 * 5. 桥的白名单当前是 `getDeviceInfo()` / `sign()` / `secureStore()` 三个方法
 *    （§7.11 第 3 条）；登录/刷新若要新增方法，必须同时改原生白名单与安全审查清单。
 *    在原生实现之前，`nativeBridge()` 返回 null，页面显示「等待 App 原生登录」而不是假按钮。
 */
export function nativeBridge(): Required<Pick<NonNullable<Window['__AC_BRIDGE__']>, 'login'>> | null {
  if (typeof window === 'undefined') return null;
  const bridge = window.__AC_BRIDGE__;
  return bridge && typeof bridge.login === 'function' ? { login: bridge.login } : null;
}

export async function nativeLogin(): Promise<HostSession> {
  const bridge = nativeBridge();
  if (!bridge) throw new Error('NATIVE_BRIDGE_UNAVAILABLE');
  const session = await bridge.login();
  if (!session || typeof session.access_token !== 'string' || session.access_token.length === 0) {
    throw new Error('NATIVE_BRIDGE_NO_TOKEN');
  }
  return session;
}

/** 冷启动时原生已经注入会话的场景（刷新页面后重新登录由原生代发）。 */
export function injectedSession(): HostSession | null {
  if (typeof window === 'undefined') return null;
  const session = window.__AC_HOST__?.session;
  return session && typeof session.access_token === 'string' && session.access_token.length > 0
    ? session
    : null;
}
