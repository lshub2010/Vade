import { useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactElement } from 'react';

export interface StepUpDialogProps {
  title: string;
  lede: string;
  confirmLabel: string;
  busy?: boolean;
  error?: string;
  onSubmit: (password: string) => void;
  onCancel: () => void;
}

/**
 * Step-up：再输一次当前密码。
 *
 * 这不是装饰 —— `POST /admin/api/devices/ticket` 与 `POST /admin/api/devices`
 * 都以 body 里的 password 作为唯一防线（design §7.1.3）：拿着一个已登录会话的人
 * 不该能顺手登记一把新密钥。
 */
export function StepUpDialog({
  title,
  lede,
  confirmLabel,
  busy = false,
  error = '',
  onSubmit,
  onCancel,
}: StepUpDialogProps): ReactElement {
  const [password, setPassword] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') onCancel();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onCancel]);

  const submit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (busy || password.length === 0) return;
    onSubmit(password);
  };

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onCancel();
      }}
    >
      <form className="modal" role="dialog" aria-modal="true" aria-labelledby="stepup-title" onSubmit={submit}>
        <h2 id="stepup-title" className="modal-title">
          {title}
        </h2>
        <div className="modal-body">
          <p className="muted">{lede}</p>
          <label className="field">
            <span className="field-label">当前密码</span>
            <input
              ref={inputRef}
              className="input"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              aria-invalid={error.length > 0}
            />
          </label>
          <p className="form-error" role="alert">
            {error}
          </p>
        </div>
        <div className="modal-actions">
          <button type="button" className="Button Button-ghost" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button type="submit" className="Button Button-primary" disabled={busy || password.length === 0}>
            {busy ? '校验中…' : confirmLabel}
          </button>
        </div>
      </form>
    </div>
  );
}
