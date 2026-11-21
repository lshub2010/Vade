import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactElement, ReactNode } from 'react';
import { api, onUnauthorized, setAccessToken, setCsrfToken } from './api';
import { hostMode, readHost } from './host';
import type { HostMode } from './host';
import { injectedSession, nativeLogin } from './nativeBridge';
import type {
  HealthResponse,
  LoginChallenge,
  LoginResponse,
  NodeListResponse,
  OkResponse,
} from './types';

export type AuthPhase = 'checking' | 'anon' | 'authed';

export interface AuthApi {
  phase: AuthPhase;
  mode: HostMode;
  /** core 身份指纹；拿不到就是 null，页面如实显示「未提供」，不编一个。 */
  fp8: string | null;
  deviceLabel: string | null;
  /** 浏览器 = 只读。写控件一律不渲染。 */
  readOnly: boolean;
  loginWithPassword: (password: string) => Promise<void>;
  loginWithDevice: () => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthApi | null>(null);

export function AuthProvider({ children }: { children: ReactNode }): ReactElement {
  const [phase, setPhase] = useState<AuthPhase>('checking');
  const [fp8, setFp8] = useState<string | null>(null);
  const [mode] = useState<HostMode>(() => hostMode());
  const [deviceLabel] = useState<string | null>(() => readHost().deviceLabel ?? null);

  // 身份指纹：宿主注入优先；否则问 /admin/api/health（契约目前不含 fp8，没有就显示「未提供」）。
  useEffect(() => {
    const injected = readHost().fp8;
    if (injected) {
      setFp8(injected);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const health = await api<HealthResponse>('/admin/api/health', {
          anonymous: true,
          quietAuthFailure: true,
        });
        if (!cancelled && typeof health.fp8 === 'string' && health.fp8.length > 0) {
          setFp8(health.fp8);
        }
      } catch {
        // 拿不到指纹不是致命错误：页面会显示「未提供」，并保留核对提示。
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // 冷启动：webview 用原生注入的会话；browser 用 cookie 探测（只发一个 GET）。
  useEffect(() => {
    const host = hostMode();
    if (host === 'webview') {
      const session = injectedSession();
      if (session) {
        setAccessToken(session.access_token);
        if (session.csrf_token) setCsrfToken(session.csrf_token);
        setPhase('authed');
      } else {
        setPhase('anon');
      }
      return;
    }

    let cancelled = false;
    void (async () => {
      try {
        await api<NodeListResponse>('/admin/api/nodes', {
          anonymous: true,
          quietAuthFailure: true,
        });
        if (!cancelled) setPhase('authed');
      } catch {
        if (!cancelled) setPhase('anon');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // 任意业务请求 401 → 清 token 回登录页（登录页自己的 401 已 quiet）。
  useEffect(
    () =>
      onUnauthorized(() => {
        setAccessToken(null);
        setPhase('anon');
      }),
    [],
  );

  const loginWithPassword = useCallback(async (password: string): Promise<void> => {
    const challenge = await api<LoginChallenge>('/admin/api/login/challenge', {
      method: 'POST',
      body: {},
      anonymous: true,
      quietAuthFailure: true,
    });
    const result = await api<LoginResponse>('/admin/api/login', {
      method: 'POST',
      body: { challenge_id: challenge.challenge_id, password },
      anonymous: true,
      quietAuthFailure: true,
    });
    // browser 模式响应体不含 token，鉴权靠 Set-Cookie；device 模式才有 Bearer。
    // 两种模式都返回 `csrf_token`：非 GET 请求的 double-submit 必须带上它。
    // 先设 access（null 会清掉 csrf），再设 csrf。
    setAccessToken('access_token' in result ? result.access_token : null);
    if (result.csrf_token) setCsrfToken(result.csrf_token);
    setPhase('authed');
  }, []);

  const loginWithDevice = useCallback(async (): Promise<void> => {
    const session = await nativeLogin();
    setAccessToken(session.access_token);
    if (session.csrf_token) setCsrfToken(session.csrf_token);
    setPhase('authed');
  }, []);

  const logout = useCallback(async (): Promise<void> => {
    try {
      await api<OkResponse>('/admin/api/logout', { method: 'POST', quietAuthFailure: true });
    } catch {
      // 登出失败也要清本地状态：token 只在内存里，清掉就是登出。
    }
    setAccessToken(null);
    setPhase('anon');
  }, []);

  const value = useMemo<AuthApi>(
    () => ({
      phase,
      mode,
      fp8,
      deviceLabel,
      readOnly: mode === 'browser',
      loginWithPassword,
      loginWithDevice,
      logout,
    }),
    [phase, mode, fp8, deviceLabel, loginWithPassword, loginWithDevice, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthApi {
  const value = useContext(AuthContext);
  if (!value) throw new Error('useAuth must be used inside <AuthProvider>');
  return value;
}
