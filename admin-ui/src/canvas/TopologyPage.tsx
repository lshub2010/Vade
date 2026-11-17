/**
 * 拓扑页：注册表快照 + SSE 增量 + 拖拽布局 + 右侧属性面板。
 *
 * 分工：
 * - useTopology：`GET /admin/api/nodes` 一次、`/admin/api/stream` 增量、断线退避。
 * - useLayout：`GET/PATCH /admin/api/layout`，rev 乐观锁，409 拉最新并提示。
 * - NodePanel：双击节点后展开；工具分页、停用/启用/断开。
 *
 * 本页不推导状态、不拼地址、不渲染任何密钥字段。
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ReactElement } from 'react';
import { useAuth } from '../app/auth';
import { FlowCanvas } from './FlowCanvas';
import { stateLabel } from './format';
import { NodePanel } from './panel/NodePanel';
import { useLayout } from './state/useLayout';
import { useTopology } from './state/useTopology';

const STATES = ['online', 'degraded', 'offline', 'rejected', 'pending'] as const;

type ToastTone = 'ok' | 'warn' | 'error';

interface Toast {
  tone: ToastTone;
  text: string;
  id: number;
}

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
  const { readOnly } = useAuth();
  const [toast, setToast] = useState<Toast | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const notify = useCallback((tone: ToastTone, text: string): void => {
    setToast({ tone, text, id: Date.now() });
  }, []);

  const layout = useLayout({ onNotice: ({ tone, text }) => notify(tone, text) });
  const topo = useTopology();

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), toast.tone === 'ok' ? 6_000 : 12_000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  // 节点被移除（SSE node.remove / 吊销）时收起面板，不留一张空壳。
  useEffect(() => {
    if (selectedId !== null && topo.byId[selectedId] === undefined) setSelectedId(null);
  }, [topo.byId, selectedId]);

  const online = useMemo(() => topo.items.filter((item) => item.state === 'online').length, [topo.items]);
  const selected = selectedId ? (topo.byId[selectedId] ?? null) : null;

  const liveLabel =
    topo.live === 'live'
      ? '实时'
      : topo.live === 'reconnecting'
        ? '重连中'
        : topo.live === 'polling'
          ? '轮询'
          : topo.live === 'connecting'
            ? '连接中'
            : '—';

  return (
    <section className="page topo-page" aria-labelledby="topology-title">
      <header className="page-head">
        <div>
          <h1 id="topology-title">拓扑</h1>
          <p>
            以 core 为中心的连接图。一个模块一个节点，工具只在节点上计数量、不画进画布。
            {topo.status === 'ready' ? (
              <>
                {' '}
                共 <strong>{topo.items.length}</strong> 个节点，在线 <strong>{online}</strong> 个。
              </>
            ) : null}
            {' '}
            <span className={`topo-live-chip topo-live-${topo.live}`} title="实时事件流状态">
              <span className="topo-live-dot" aria-hidden="true" />
              {liveLabel}
            </span>
          </p>
        </div>
        <div className="topo-head-actions">
          <span className="muted topo-save-state" role="status" aria-live="polite">
            {layout.status === 'ready'
              ? layout.saving
                ? '布局保存中…'
                : `布局已保存 · rev ${layout.rev}`
              : layout.status === 'error'
                ? '布局不可用'
                : '布局加载中…'}
          </span>
          <button
            type="button"
            className="Button Button-ghost Button-sm"
            onClick={() => void layout.autoArrange()}
            disabled={layout.status !== 'ready' || layout.saving || topo.status !== 'ready'}
            title="调用后端 POST /admin/api/layout/auto 重排并落库"
          >
            自动排列
          </button>
          <button
            type="button"
            className="Button Button-ghost Button-sm"
            onClick={topo.reload}
            disabled={topo.status === 'loading'}
          >
            重新拉取
          </button>
        </div>
      </header>

      {topo.live === 'reconnecting' ? (
        <div className="topo-live-banner" role="alert">
          <span>
            实时连接已断开，正在重连
            {topo.retryIn !== null ? `（${topo.retryIn}s 后重试）` : ''}。画布保留最后一次数据。
          </span>
          <button type="button" className="Button Button-sm Button-ghost" onClick={topo.reload}>
            立即重连
          </button>
        </div>
      ) : null}

      {topo.live === 'polling' ? (
        <div className="topo-live-banner topo-live-banner-info" role="status">
          <span>实时事件流不可用，已改为每 5 秒轮询注册表。画布不会清空。</span>
          <button type="button" className="Button Button-sm Button-ghost" onClick={topo.reload}>
            改用实时连接
          </button>
        </div>
      ) : null}

      {layout.status === 'error' ? (
        <div className="topo-live-banner topo-live-banner-warn" role="alert">
          <span>{layout.error}——画布可用，但拖拽已停用，避免覆盖未知布局。</span>
          <button type="button" className="Button Button-sm Button-ghost" onClick={layout.reload}>
            重试
          </button>
        </div>
      ) : null}

      {topo.status === 'error' ? (
        <div className="empty empty-left" role="alert">
          <h2>读不到注册表</h2>
          <p>{topo.error}</p>
          <button type="button" className="Button Button-sm" onClick={topo.reload}>
            重试
          </button>
        </div>
      ) : null}

      {topo.status === 'loading' ? (
        <div className="topo-skeleton" role="status" aria-live="polite">
          <span className="topo-skeleton-ring" aria-hidden="true" />
          <span className="muted">拓扑加载中…</span>
        </div>
      ) : null}

      {topo.status === 'ready' ? (
        <>
          {topo.items.length === 0 ? (
            <p className="muted topo-hint">
              还没有模块接入，画布上只有 core。模块启动并完成配对后会出现在这里。
            </p>
          ) : (
            <p className="muted topo-hint">
              双击节点查看工具与治理操作；拖动节点保存位置（拖完才写一次，不会在拖动中连续请求）。
            </p>
          )}
          <div className={selected ? 'topo-body topo-body-panel' : 'topo-body'}>
            <FlowCanvas
              items={topo.items}
              positions={layout.status === 'ready' ? layout.positions : undefined}
              edgeStates={topo.edgeStates}
              layoutReady={layout.status === 'ready'}
              onMoveNode={layout.savePosition}
              onOpenNode={setSelectedId}
            />
            {selected ? (
              <NodePanel
                node={selected}
                readOnly={readOnly}
                disabled={topo.disabledIds.has(selected.id)}
                onClose={() => setSelectedId(null)}
                onEnabledChange={topo.markEnabled}
                onRefreshNode={topo.refreshNode}
                onNotice={notify}
              />
            ) : null}
          </div>
          <Legend />
        </>
      ) : null}

      {toast ? (
        <div className={`toast topo-toast topo-toast-${toast.tone}`} role="status">
          <span>{toast.text}</span>
          <button type="button" className="Button Button-sm Button-ghost" onClick={() => setToast(null)}>
            知道了
          </button>
        </div>
      ) : null}
    </section>
  );
}
