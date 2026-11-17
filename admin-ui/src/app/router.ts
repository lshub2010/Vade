/** 极小的 hash 路由：同一份 dist 在 `/admin/`、`app://admin/`、`file://…/index.html` 下都能跑。 */

import { useEffect, useState } from 'react';

// 第一批只注册这五条；`/skills`、`/memory`、`/topology` 归后续批次（canvas 由 C5b 提供）。
export const ROUTES = ['/nodes', '/audit', '/settings', '/setup', '/login'] as const;

export type RoutePath = (typeof ROUTES)[number];

export function isRoute(path: string): path is RoutePath {
  return (ROUTES as readonly string[]).includes(path);
}

export function currentPath(): string {
  const raw = window.location.hash.replace(/^#/, '');
  const path = raw.split('?')[0] ?? '';
  return path.startsWith('/') ? path : '/nodes';
}

export function navigate(path: string, options: { replace?: boolean } = {}): void {
  const hash = `#${path}`;
  if (options.replace) {
    const url = `${window.location.pathname}${window.location.search}${hash}`;
    window.location.replace(url);
  } else {
    window.location.hash = path;
  }
}

export function useRoute(): string {
  const [path, setPath] = useState<string>(() => currentPath());

  useEffect(() => {
    const onChange = (): void => setPath(currentPath());
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);

  return path;
}
