/**
 * 拓扑页：注册表的一次性快照 + 画布。
 *
 * 数据只来自 `GET /admin/api/nodes`（C-08 白名单字段），前端不推导状态、不拼地址。
 * SSE 增量（T4）与属性面板（T5）下一批接；现在刷新靠页面上的「重新拉取」。
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ReactElement } from 'react';
import { api, describeError } from '../app/api';
import type { NodeListResponse, NodeView } from '../app/types';
import { FlowCanvas } from './FlowCanvas';
import { stateLabel } from './format';

const STATES = ['online', 'degraded', 'offline', 'rejected', 'pending'] as const;

function Legend(): ReactElement {
  return (
    <div className="topo-legend" aria-label="状态图例：颜色与线型双重编码">
      <span className="topo-legend-title">状态</span>
      <ul className="topo-legend-list">
        {STATES.map((state) => (
          <li key={state} className={`topo-legend-item topo-state-${state}`}>
            <svg width="42" height="10" viewBox="0 0 42 10" aria-hidden="true" focusable="false">
              <line className={`topo-edge topo-edge-${state}`} x1="1" y1="5" x2="41" y2="5" />
            </svg>
            <span>{stateLabel(state)}</span>
          </li>
        ))}
      </ul>
      <span className="topo-legend-note">同一状态同时用颜色与线型表达，灰度截图仍可区分。</span>
    </div>
  );
}

export function TopologyPage(): ReactElement {
  const [items, setItems] = useState<NodeView[]>([]);
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [error, setError] = useState('');
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    const controller = new AbortController();

    async function load(): Promise<void> {
      setStatus('loading');
      setError('');
      try {
        const data = await api<NodeListResponse>('/admin/api/nodes', { signal: controller.signal });
        if (controller.signal.aborted) return;
        setItems(Array.isArray(data.items) ? data.items : []);
        setStatus('ready');
      } catch (caught) {
        if (controller.signal.aborted) return;
        const message = describeError(caught);
        if (message.length === 0) return; // AbortError
        setError(message);
        setStatus('error');
      }
    }

    void load();
    return () => controller.abort();
  }, [reloadKey]);

  const online = useMemo(() => items.filter((item) => item.state === 'online').length, [items]);
  const reload = useCallback(() => setReloadKey((key) => key + 1), []);

  return (
    <section className="page topo-page" aria-labelledby="topology-title">
      <header className="page-head">
        <div>
          <h1 id="topology-title">拓扑</h1>
          <p>
            以 core 为中心的连接图。一个模块一个节点，工具只在节点上计数量、不画进画布。
            {status === 'ready' ? (
              <>
                {' '}
                共 <strong>{items.length}</strong> 个节点，在线 <strong>{online}</strong> 个。
              </>
            ) : null}
          </p>
        </div>
        <div className="topo-head-actions">
          <button
            type="button"
            className="Button Button-ghost Button-sm"
            onClick={reload}
            disabled={status === 'loading'}
          >
            重新拉取
          </button>
        </div>
      </header>

      {status === 'error' ? (
        <div className="empty empty-left" role="alert">
          <h2>读不到注册表</h2>
          <p>{error}</p>
          <button type="button" className="Button Button-sm" onClick={reload}>
            重试
          </button>
        </div>
      ) : null}

      {status === 'loading' ? (
        <div className="topo-skeleton" role="status" aria-live="polite">
          <span className="topo-skeleton-ring" aria-hidden="true" />
          <span className="muted">拓扑加载中…</span>
        </div>
      ) : null}

      {status === 'ready' ? (
        <>
          {items.length === 0 ? (
            <p className="muted topo-hint">
              还没有模块接入，画布上只有 core。模块启动并完成配对后会出现在这里。
            </p>
          ) : null}
          <FlowCanvas items={items} />
          <Legend />
        </>
      ) : null}
    </section>
  );
}
