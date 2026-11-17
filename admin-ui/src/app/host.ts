/**
 * 运行形态由宿主注入，不由页面猜、也不读 URL 参数（URL 可被社工发送）。
 *
 * - App 内 WebView：宿主注入 `window.__AC_HOST__ = {mode:'webview', fp8, deviceLabel}`
 *   （只加载 App 内置资源，见 design.md §7.11 第 1 条）。
 * - 系统浏览器：core 托管 `/admin` 时注入 `{mode:'browser', fp8}`。
 * - 都没有时按 `browser` 处理 —— 只读是安全侧默认，即使判断错了也只会少显示按钮；
 *   服务端仍会对写请求返回 `403 READ_ONLY_SESSION`。
 *
 * `fp8` 是 core **身份指纹**（SHA-256(core_identity_pub)[:16]），与 TLS 证书 SPKI 指纹不是一回事。
 */

export type HostMode = 'webview' | 'browser';

export interface HostSession {
  /** 只存内存；refresh_token 永远不进页面。 */
  access_token: string;
  expires_in?: number;
}

export interface HostInjection {
  mode?: HostMode;
  /** core 身份指纹，16 位小写 hex。 */
  fp8?: string;
  deviceLabel?: string;
  /** core 的 admin_host（`host:port`）；只有宿主知道时才有，页面拿不到就不编一个。 */
  adminHost?: string;
  /** 冷启动时原生已经完成登录的情况下可直接注入；页面仍然只放内存。 */
  session?: HostSession;
}

/**
 * App 原生桥（**接口占位**）。
 * 密码框、challenge、设备私钥签名、`POST /admin/api/login` 全部在原生层完成
 * （design.md §7.11 第 8 条：密码输入不走 WebView）。
 * 原生只需把 `{access_token}` 交给页面；`refresh_token` 留在钥匙串。
 */
export interface AdminNativeBridge {
  login?: () => Promise<HostSession>;
  refresh?: () => Promise<HostSession>;
}

declare global {
  interface Window {
    __AC_HOST__?: HostInjection;
    __AC_BRIDGE__?: AdminNativeBridge;
  }
}

export function readHost(): HostInjection {
  if (typeof window === 'undefined') return { mode: 'browser' };
  const injected = window.__AC_HOST__ ?? {};
  const metaMode = metaContent('vade-mode');
  const explicitMode =
    injected.mode === 'webview' || injected.mode === 'browser'
      ? injected.mode
      : metaMode === 'webview' || metaMode === 'browser'
        ? metaMode
        : undefined;
  // CSP 是 `script-src 'self'`，所以 core 改写 index.html 时不能塞 inline script；
  // 它可以用 <meta name="vade-mode"/vade-fp8"> 这两个无脚本通道。
  const fp8 = injected.fp8 ?? metaContent('vade-fp8');
  return {
    ...injected,
    fp8,
    mode: explicitMode ?? (window.__AC_BRIDGE__ ? 'webview' : 'browser'),
  };
}

function metaContent(name: string): string | undefined {
  if (typeof document === 'undefined') return undefined;
  const content = document.querySelector(`meta[name="${name}"]`)?.getAttribute('content')?.trim();
  return content && content.length > 0 ? content : undefined;
}

export function hostMode(): HostMode {
  return readHost().mode === 'webview' ? 'webview' : 'browser';
}
