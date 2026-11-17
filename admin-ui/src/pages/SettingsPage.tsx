import { useCallback, useEffect, useState } from 'react';
import type { ReactElement } from 'react';
import { ApiError, api, describeError } from '../app/api';
import { useAuth } from '../app/auth';
import { ConfirmDialog } from '../app/ConfirmDialog';
import type { ConfigItem, ConfigListResponse, ConfigUsage } from '../app/types';
import { DevicesCard } from './settings/DevicesCard';
import { PasswordCard } from './settings/PasswordCard';
import { PolicyCard } from './settings/PolicyCard';
import { ReadOnlyCard } from './settings/ReadOnlyCard';

/**
 * `/settings`：改密码、设备、登出全部、记忆与进化策略、只读生效配置。
 *
 * 浏览器形态不渲染任何写控件（`readOnly` 由宿主注入的 mode 决定）；服务端也会对
 * 写请求回 403 READ_ONLY_SESSION —— 页面少画按钮只是不给人「点了 403」的界面。
 */
export function SettingsPage(): ReactElement {
  const { readOnly, fp8 } = useAuth();
  const [items, setItems] = useState<ConfigItem[] | null>(null);
  const [configError, setConfigError] = useState('');
  const [unsupported, setUnsupported] = useState(false);
  const [usage, setUsage] = useState<ConfigUsage | null>(null);
  const [usageError, setUsageError] = useState('');

  const loadConfig = useCallback(async (): Promise<void> => {
    try {
      const result = await api<ConfigListResponse>('/admin/api/config');
      setItems(result.items);
      setConfigError('');
      setUnsupported(false);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 501) {
        setUnsupported(true);
        setItems(null);
        return;
      }
      setConfigError(describeError(cause));
    }
  }, []);

  const loadUsage = useCallback(async (): Promise<void> => {
    try {
      const result = await api<ConfigUsage>('/admin/api/config/usage');
      setUsage(result);
      setUsageError('');
    } catch (cause) {
      setUsage(null);
      setUsageError(describeError(cause));
    }
  }, []);

  useEffect(() => {
    void loadConfig();
    void loadUsage();
  }, [loadConfig, loadUsage]);

  const onSaved = useCallback(async (): Promise<void> => {
    // 只重新拉数据，不 reload 页面：其它行未保存的草稿留在各自的 state 里。
    await loadConfig();
    await loadUsage();
  }, [loadConfig, loadUsage]);

  return (
    <section className="page" aria-labelledby="settings-title">
      <header className="page-head">
        <div>
          <h1 id="settings-title">设置</h1>
          <p className="muted">
            密码、已绑定设备、登出全部、记忆与进化策略，以及只读的生效配置
            {readOnly ? ' · 当前会话只读' : ''}
          </p>
        </div>
        <span className={readOnly ? 'pill pill-readonly' : 'pill pill-full'}>
          {readOnly ? '只读' : '可写'}
        </span>
      </header>

      <PasswordCard readOnly={readOnly} />

      <DevicesCard readOnly={readOnly} />

      <LogoutAllCard readOnly={readOnly} />

      {configError ? (
        <div className="alert" role="alert">
          <span>读取配置失败：{configError}</span>
          <button type="button" className="Button Button-sm" onClick={() => void loadConfig()}>
            重试
          </button>
        </div>
      ) : null}

      {unsupported ? (
        <div className="empty empty-left">
          <h2>本构建没有配置接口</h2>
          <p>
            core 对 <code>GET /admin/api/config</code> 回了 <code>501 UNSUPPORTED</code>：这个构建没装配
            管理面配置。策略与只读配置两块不渲染，避免画一堆改不动的控件。
          </p>
        </div>
      ) : null}

      {items ? (
        <>
          <PolicyCard
            items={items}
            readOnly={readOnly}
            usage={usage}
            usageError={usageError}
            onSaved={onSaved}
            onUsageRefresh={() => void loadUsage()}
            fp8={fp8}
          />
          <ReadOnlyCard items={items} />
        </>
      ) : null}

      {!items && !configError && !unsupported ? <p className="muted">正在读取配置…</p> : null}
    </section>
  );
}

function LogoutAllCard({ readOnly }: { readOnly: boolean }): ReactElement {
  const { logout } = useAuth();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  if (readOnly) {
    return (
      <section className="card" aria-labelledby="settings-logout-all">
        <header className="card-head">
          <div>
            <h2 id="settings-logout-all">登出全部设备</h2>
            <p className="card-sub">注销这台 core 上所有设备的登录会话（包括当前这台）。</p>
          </div>
        </header>
        <p className="notice notice-readonly">
          只读会话不能执行「登出全部」：这条路由要求设备凭据，避免一个被偷的 cookie 把所有人踢下线。
        </p>
      </section>
    );
  }

  const run = async (): Promise<void> => {
    setBusy(true);
    setError('');
    try {
      await api<{ ok: boolean; revoked: number }>('/admin/api/logout-all', {
        method: 'POST',
        body: {},
        quietAuthFailure: true,
      });
    } catch (cause) {
      setError(describeError(cause));
      setBusy(false);
      return;
    }
    setBusy(false);
    setOpen(false);
    // 会话已经被服务端作废：清本地 token 并回登录页（logout 里的 POST /logout 会 401，正好被 quiet 掉）。
    await logout();
  };

  return (
    <section className="card" aria-labelledby="settings-logout-all">
      <header className="card-head">
        <div>
          <h2 id="settings-logout-all">登出全部设备</h2>
          <p className="card-sub">
            把所有设备的登录会话一起作废，需要重新输入密码。绑定关系（设备公钥）不受影响。
          </p>
        </div>
        <div className="card-head-actions">
          <button type="button" className="Button Button-sm Button-danger-ghost" onClick={() => setOpen(true)}>
            登出全部
          </button>
        </div>
      </header>

      {error ? (
        <p className="form-error" role="alert">
          登出全部失败：{error}
        </p>
      ) : null}

      {open ? (
        <ConfirmDialog
          title="登出所有设备？"
          confirmLabel="登出全部"
          danger
          busy={busy}
          onConfirm={() => void run()}
          onCancel={() => {
            if (!busy) setOpen(false);
          }}
        >
          <p>
            这会注销这台 core 上的<strong>所有登录会话，包括当前这台设备</strong>。当前页面会立刻回到登录页，
            需要重新输入密码；进行中的页面操作会丢失。设备绑定关系保持不变。
          </p>
        </ConfirmDialog>
      ) : null}
    </section>
  );
}
