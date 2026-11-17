import { useState } from 'react';
import type { FormEvent, ReactElement } from 'react';
import { api, describeError, setAccessToken } from '../../app/api';
import type { PasswordChangeResponse } from '../../app/types';

const MIN_LENGTH = 12;

/**
 * 改密码。旧 + 新 + 确认。
 *
 * 后端换密码后会作废旧 token 版本并发一套新的（当前设备不掉线）；页面把新 access
 * token 放回内存，否则下一次请求就会 401。browser 会话不走这条路 —— 后端对这条
 * 路由做了 device-only，页面也不渲染表单。
 */
export function PasswordCard({ readOnly }: { readOnly: boolean }): ReactElement {
  const [oldPassword, setOldPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [error, setError] = useState('');
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (busy) return;
    setDone(false);
    if (oldPassword.length === 0) {
      setError('请输入当前密码');
      return;
    }
    if (newPassword.length < MIN_LENGTH) {
      setError(`新密码至少 ${MIN_LENGTH} 个字符`);
      return;
    }
    if (newPassword !== confirmPassword) {
      setError('两次输入的新密码不一致');
      return;
    }
    if (newPassword === oldPassword) {
      setError('新密码不能与当前密码相同');
      return;
    }

    setBusy(true);
    setError('');
    try {
      const result = await api<PasswordChangeResponse>('/admin/api/password', {
        method: 'POST',
        body: { old_password: oldPassword, new_password: newPassword },
        // 旧密码错会回 401 AUTH_FAILED；这里quiet 掉，否则全局监听会把用户踢回登录页。
        quietAuthFailure: true,
      });
      if (typeof result.access_token === 'string' && result.access_token.length > 0) {
        setAccessToken(result.access_token);
      }
      setOldPassword('');
      setNewPassword('');
      setConfirmPassword('');
      setDone(true);
    } catch (cause) {
      setError(passwordError(cause));
      setNewPassword('');
      setConfirmPassword('');
    } finally {
      setBusy(false);
    }
  };

  if (readOnly) {
    return (
      <section className="card" aria-labelledby="settings-password">
        <header className="card-head">
          <div>
            <h2 id="settings-password">登录密码</h2>
            <p className="card-sub">旧密码 + 新密码 + 确认；成功后其它设备的登录立即失效。</p>
          </div>
        </header>
        <p className="notice notice-readonly">
          浏览器只读会话不能改密码：后端对 <code>POST /admin/api/password</code> 要求设备凭据。
          请用 Vade App 打开管理面。
        </p>
      </section>
    );
  }

  return (
    <section className="card" aria-labelledby="settings-password">
      <header className="card-head">
        <div>
          <h2 id="settings-password">登录密码</h2>
          <p className="card-sub">
            旧密码 + 新密码 + 确认。至少 {MIN_LENGTH} 个字符，不强制大小写与符号组合。
          </p>
        </div>
      </header>

      <form className="form-grid" onSubmit={(event) => void submit(event)}>
        <label className="field">
          <span className="field-label">当前密码</span>
          <input
            className="input"
            type="password"
            autoComplete="current-password"
            value={oldPassword}
            onChange={(event) => setOldPassword(event.target.value)}
          />
        </label>
        <label className="field">
          <span className="field-label">新密码</span>
          <input
            className="input"
            type="password"
            autoComplete="new-password"
            value={newPassword}
            onChange={(event) => setNewPassword(event.target.value)}
            aria-invalid={error.length > 0}
          />
        </label>
        <label className="field">
          <span className="field-label">确认新密码</span>
          <input
            className="input"
            type="password"
            autoComplete="new-password"
            value={confirmPassword}
            onChange={(event) => setConfirmPassword(event.target.value)}
            aria-invalid={error.length > 0}
          />
        </label>

        <p className="form-error" role="alert">
          {error}
        </p>

        {done ? (
          <p className="notice" role="status">
            密码已更新。其它设备的登录已失效；当前这台设备仍然有效，不用重新登录。
          </p>
        ) : null}

        <div className="card-actions">
          <button type="submit" className="Button Button-primary" disabled={busy}>
            {busy ? '提交中…' : '修改密码'}
          </button>
        </div>
      </form>
    </section>
  );
}

function passwordError(cause: unknown): string {
  const message = describeError(cause);
  if (message === '密码或设备不正确') return '当前密码不正确';
  if (message.length === 0) return '修改失败，请重试';
  return `修改失败：${message}`;
}
