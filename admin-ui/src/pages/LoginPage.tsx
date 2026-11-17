import { useState } from 'react';
import type { FormEvent, ReactElement } from 'react';
import { describeError } from '../app/api';
import { useAuth } from '../app/auth';
import { formatFingerprint } from '../app/format';
import { Link } from '../app/Shell';
import { nativeBridge } from '../app/nativeBridge';

export function LoginPage(): ReactElement {
  const { mode, fp8, deviceLabel, readOnly, loginWithPassword, loginWithDevice } = useAuth();
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const bridgeReady = nativeBridge() !== null;

  const submitPassword = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (busy || password.length === 0) return;
    setBusy(true);
    setError('');
    try {
      await loginWithPassword(password);
    } catch (cause) {
      // 不区分「密码错 / 设备未登记 / 签名错」；只有网络与限流错误如实另说。
      setError(describeError(cause));
      setPassword('');
    } finally {
      setBusy(false);
    }
  };

  const submitDevice = async (): Promise<void> => {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await loginWithDevice();
    } catch (cause) {
      const message =
        cause instanceof Error && cause.message === 'NATIVE_BRIDGE_UNAVAILABLE'
          ? '原生桥尚未就绪，请从 App 的登录入口进入'
          : describeError(cause);
      setError(message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-page">
      <section className="auth-card" aria-labelledby="login-title">
        <div className="auth-brand">
          <span className="brand-mark" aria-hidden="true">
            V
          </span>
          <span className="brand-name">Vade 管理面</span>
        </div>

        <h1 id="login-title">登录</h1>
        <p className="auth-lede">
          {readOnly
            ? '这是系统浏览器的只读会话：可以查看节点与审计，不能修改任何配置。'
            : '已绑定设备在此确认身份后可以管理模块与配置。'}
        </p>

        <div className="fingerprint" aria-label="core 身份指纹">
          <div className="fingerprint-label">core 身份指纹</div>
          <div className="fingerprint-value">
            <code>{formatFingerprint(fp8)}</code>
          </div>
          <p className="fingerprint-hint">
            与二维码 / <code>core --print-fingerprint</code> 一致才输入密码。管理面没有端到端加密，密码只靠
            TLS 保护。
          </p>
        </div>

        {readOnly ? (
          <div className="notice notice-readonly" role="status">
            当前为只读模式；要修改配置请用 Vade App 打开。
          </div>
        ) : null}

        {mode === 'browser' ? (
          <form className="auth-form" onSubmit={(event) => void submitPassword(event)} noValidate>
            <label className="field">
              <span className="field-label">密码</span>
              <input
                className="input"
                type="password"
                name="password"
                autoComplete="current-password"
                autoFocus
                required
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                aria-invalid={error.length > 0}
              />
            </label>

            <div className="form-error" role="alert" aria-live="assertive">
              {error}
            </div>

            <button type="submit" className="Button Button-primary Button-block" disabled={busy || password.length === 0}>
              {busy ? '正在登录…' : '登录'}
            </button>
          </form>
        ) : (
          <div className="native-login">
            <p className="native-login-lede">
              密码输入由 App 原生弹窗完成，本页面不渲染密码框，也不接触密码与设备私钥。
            </p>
            {deviceLabel ? (
              <p className="native-device">
                当前设备：<strong>{deviceLabel}</strong>
              </p>
            ) : null}

            <button
              type="button"
              className="Button Button-primary Button-block"
              onClick={() => void submitDevice()}
              disabled={busy || !bridgeReady}
            >
              {busy ? '等待 App 确认…' : '用本机设备登录'}
            </button>

            {bridgeReady ? null : (
              <p className="hint">
                原生桥尚未提供 <code>login()</code>。请在 App 的登录入口完成一次登录后重新打开本页。
              </p>
            )}

            <div className="form-error" role="alert" aria-live="assertive">
              {error}
            </div>

            <p className="hint">
              页面只持有内存中的 access token；refresh token 留在 App 钥匙串，刷新由原生代发。
            </p>
          </div>
        )}

        <footer className="auth-footer">
          <span>
            形态 <code>{mode}</code>
          </span>
          <Link to="/setup">首次配置</Link>
        </footer>
      </section>
    </div>
  );
}
