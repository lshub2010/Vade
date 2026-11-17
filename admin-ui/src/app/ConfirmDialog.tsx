import { useEffect, useRef } from 'react';
import type { ReactElement, ReactNode } from 'react';

export interface ConfirmDialogProps {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** 破坏性操作的确认框：正文必须写清影响面，不写「确定吗」。 */
export function ConfirmDialog({
  title,
  children,
  confirmLabel,
  danger = false,
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps): ReactElement {
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    confirmRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') onCancel();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onCancel]);

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onCancel();
      }}
    >
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
        <h2 id="confirm-title" className="modal-title">
          {title}
        </h2>
        <div className="modal-body">{children}</div>
        <div className="modal-actions">
          <button type="button" className="Button Button-ghost" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button
            ref={confirmRef}
            type="button"
            className={danger ? 'Button Button-danger' : 'Button Button-primary'}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? '执行中…' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
