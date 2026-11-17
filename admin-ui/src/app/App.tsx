import { Suspense, lazy } from 'react';
import { useEffect } from 'react';
import type { ReactElement, ReactNode } from 'react';
import { AuthProvider, useAuth } from './auth';
import { isRoute, navigate, useRoute } from './router';
import { Shell } from './Shell';
import { LoginPage } from '../pages/LoginPage';
import { NodesPage } from '../pages/NodesPage';
import { SkillsPage } from '../pages/SkillsPage';
import { AuditPage } from '../pages/AuditPage';
import { SetupPage } from '../pages/SetupPage';
import { SettingsPage } from '../pages/SettingsPage';

/**
 * 拓扑画布懒加载：`@xyflow/react` 加自研节点约 100KB gzip，
 * 首屏（节点页）不该为它买单。dist 仍是单份静态资源，hash 路由下不需要额外配置。
 */
const TopologyPage = lazy(() => import('../canvas'));

function BootScreen({ label = '正在检查登录状态…' }: { label?: string }): ReactElement {
  return (
    <div className="boot" role="status" aria-live="polite">
      <span className="boot-mark" aria-hidden="true">
        V
      </span>
      <span className="muted">{label}</span>
    </div>
  );
}

function Redirect({ to }: { to: string }): ReactElement {
  useEffect(() => {
    navigate(to, { replace: true });
  }, [to]);
  return <BootScreen label="正在跳转…" />;
}

function pageFor(path: string): ReactNode {
  switch (path) {
    case '/nodes':
      return <NodesPage />;
    case '/topology':
      // Suspense 放在 pageFor 里而不是 Shell 外面：切走时只卸载画布，页头不动。
      return (
        <Suspense fallback={<BootScreen label="拓扑加载中…" />}>
          <TopologyPage />
        </Suspense>
      );
    case '/skills':
      return <SkillsPage />;
    case '/audit':
      return <AuditPage />;
    case '/settings':
      return <SettingsPage />;
    default:
      return <Redirect to="/nodes" />;
  }
}

const TITLES: Record<string, string> = {
  '/nodes': '节点',
  '/topology': '拓扑',
  '/skills': '技能',
  '/audit': '审计',
  '/settings': '设置',
  '/setup': '首次配置',
  '/login': '登录',
};

function Routes(): ReactElement {
  const { phase } = useAuth();
  const path = useRoute();

  useEffect(() => {
    const title = TITLES[path] ?? '管理面';
    document.title = `${title} · Vade 管理面`;
  }, [path]);

  if (path === '/login') {
    return phase === 'authed' ? <Redirect to="/nodes" /> : <LoginPage />;
  }

  // setup 页自己就是「未初始化」入口，不做鉴权守卫。
  if (path === '/setup') return <SetupPage />;

  if (phase === 'checking') return <BootScreen />;
  if (phase !== 'authed') return <Redirect to="/login" />;
  if (!isRoute(path) || path === '/login' || path === '/setup') return <Redirect to="/nodes" />;

  return <Shell path={path}>{pageFor(path)}</Shell>;
}

export function App(): ReactElement {
  return (
    <AuthProvider>
      <Routes />
    </AuthProvider>
  );
}
