import type { ReactElement, ReactNode } from 'react';
import { useAuth } from './auth';
import { formatFingerprint } from './format';
import { navigate } from './router';

const NAV = [
  { path: '/nodes', label: '节点' },
  { path: '/topology', label: '拓扑' },
  { path: '/skills', label: '技能' },
  { path: '/audit', label: '审计' },
  { path: '/settings', label: '设置' },
] as const;

export function Link({
  to,
  children,
  className,
}: {
  to: string;
  children: ReactNode;
  className?: string;
}): ReactElement {
  return (
    <a
      href={`#${to}`}
      className={className}
      onClick={(event) => {
        // 让 hashchange 统一驱动路由；阻止默认跳转避免把当前滚动位置丢掉两次。
        event.preventDefault();
        navigate(to);
      }}
    >
      {children}
    </a>
  );
}

export function Shell({ path, children }: { path: string; children: ReactNode }): ReactElement {
  const { mode, fp8, readOnly, logout } = useAuth();

  return (
    <div className="shell">
      <header className="shell-header">
        <div className="shell-brand">
          <span className="brand-mark" aria-hidden="true">
            V
          </span>
          <span className="brand-name">Vade 管理面</span>
        </div>

        <nav className="shell-nav" aria-label="主导航">
          {NAV.map((item) => (
            <Link
              key={item.path}
              to={item.path}
              className={path === item.path ? 'nav-link nav-link-active' : 'nav-link'}
            >
              {item.label}
            </Link>
          ))}
        </nav>

        <div className="shell-meta">
          <span className={readOnly ? 'pill pill-readonly' : 'pill pill-full'}>
            {readOnly ? '浏览器 · 只读' : 'App · 可写'}
          </span>
          <span className="fp8-inline" title="core 身份指纹（SHA-256(core_identity_pub)[:16]）">
            fp8 <code>{formatFingerprint(fp8)}</code>
          </span>
          <button type="button" className="Button Button-ghost Button-sm" onClick={() => void logout()}>
            退出
          </button>
        </div>
      </header>

      {readOnly ? (
        <div className="readonly-banner" role="status">
          当前为只读模式：系统浏览器会话只能查看。要修改配置，请用 Vade App 打开管理面。
        </div>
      ) : null}

      <main className="shell-main" id="main">
        {children}
      </main>

      <footer className="shell-footer">
        <span>
          形态 <code>{mode}</code>
        </span>
        <span>管理面没有端到端加密，凭据只靠 TLS 保护。</span>
      </footer>
    </div>
  );
}
