import { useCallback, useEffect, useState } from 'react';
import type { ReactElement, ReactNode } from 'react';
import { api, describeError } from '../app/api';
import { useAuth } from '../app/auth';
import { ConfirmDialog } from '../app/ConfirmDialog';
import { absoluteTime, formatRtt, relativeTime } from '../app/format';
import type {
  NodeListResponse,
  NodePatchResponse,
  NodeRevokeResponse,
  NodeView,
  OkResponse,
} from '../app/types';

type ActionKind = 'disable' | 'enable' | 'disconnect' | 'revoke';

interface PendingAction {
  node: NodeView;
  kind: ActionKind;
}

const STATE_LABELS: Record<string, string> = {
  online: '在线',
  degraded: '降级',
  offline: '离线',
  pending: '待接入',
  rejected: '已拒绝',
  unknown: '未知',
};

function stateClass(state: string): string {
  switch (state) {
    case 'online':
      return 'badge badge-ok';
    case 'degraded':
      return 'badge badge-warn';
    case 'offline':
      return 'badge badge-muted';
    case 'pending':
      return 'badge badge-info';
    case 'rejected':
      return 'badge badge-danger';
    default:
      return 'badge badge-muted';
  }
}

function nodeName(node: NodeView): string {
  return node.label && node.label.length > 0 ? node.label : node.module_id;
}

function toolList(node: NodeView): ReactNode {
  if (node.tool_names.length === 0) return <span className="muted">当前无工具</span>;
  return node.tool_names.map((name, index) => (
    <span key={name}>
      <code>{name}</code>
      {index < node.tool_names.length - 1 ? ' / ' : ''}
    </span>
  ));
}

function confirmTitle(kind: ActionKind, node: NodeView): string {
  const name = nodeName(node);
  switch (kind) {
    case 'disable':
      return `停用 ${name}？`;
    case 'enable':
      return `启用 ${name}？`;
    case 'disconnect':
      return `断开 ${name}？`;
    case 'revoke':
      return `吊销 ${name}？`;
  }
}

function ConfirmBody({ kind, node }: { kind: ActionKind; node: NodeView }): ReactElement {
  if (kind === 'disable') {
    return (
      <p>
        停用 <code>{node.module_id}</code> 将移除 {node.tool_count} 个工具（{toolList(node)}）；
        当前会话不受影响，下一轮对话起模型将看不到它们。
      </p>
    );
  }
  if (kind === 'enable') {
    return (
      <p>
        启用 <code>{node.module_id}</code> 后，其 {node.tool_count} 个工具将在下一轮对话起对模型可见。
      </p>
    );
  }
  if (kind === 'disconnect') {
    return (
      <p>
        断开 <code>{node.module_id}#{node.instance_id}</code> 后该节点会立即掉线并尝试重连；
        已登记的密钥与能力不受影响。
      </p>
    );
  }
  return (
    <p>
      吊销 <code>{node.module_id}#{node.instance_id}</code> 会立即关闭该节点的全部连接并移除其登记密钥；
      节点必须重新配对才能接入。<strong>此操作不可撤销。</strong>
    </p>
  );
}

export function NodesPage(): ReactElement {
  const { readOnly } = useAuth();
  const [nodes, setNodes] = useState<NodeView[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [actionError, setActionError] = useState('');
  const [flash, setFlash] = useState('');
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [disabled, setDisabled] = useState<ReadonlySet<string>>(new Set());
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [now, setNow] = useState<number>(() => Date.now());

  const load = useCallback(async (signal?: AbortSignal): Promise<void> => {
    setLoading(true);
    try {
      const data = await api<NodeListResponse>('/admin/api/nodes', { signal });
      setNodes(data.items);
      setLoadError('');
    } catch (cause) {
      if (signal?.aborted) return;
      setLoadError(describeError(cause));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  // 最后心跳是相对时间；页面停留时也要继续走，不然「12 秒前」会停住。
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 15_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!flash) return;
    const timer = window.setTimeout(() => setFlash(''), 8_000);
    return () => window.clearTimeout(timer);
  }, [flash]);

  const runAction = useCallback(
    async (node: NodeView, kind: ActionKind): Promise<void> => {
      const base = `/admin/api/nodes/${encodeURIComponent(node.id)}`;
      setBusyId(node.id);
      setActionError('');
      try {
        if (kind === 'revoke') {
          const result = await api<NodeRevokeResponse>(`${base}/revoke`, { method: 'POST' });
          setFlash(
            `已吊销 ${node.module_id}：关闭 ${result.connections_closed} 个连接` +
              (result.audit_id ? `（审计 ${result.audit_id}）` : ''),
          );
        } else if (kind === 'disconnect') {
          await api<OkResponse>(`${base}/disconnect`, { method: 'POST' });
          setFlash(`已断开 ${node.module_id}；节点会自动尝试重连。`);
        } else {
          const enabled = kind === 'enable';
          const result = await api<NodePatchResponse>(base, { method: 'PATCH', body: { enabled } });
          setDisabled((previous) => {
            const next = new Set(previous);
            if (enabled) next.delete(node.id);
            else next.add(node.id);
            return next;
          });
          setFlash(
            `已${enabled ? '启用' : '停用'} ${node.module_id}：影响 ${result.impact.tool_count} 个工具、` +
              `${result.impact.sessions_affected} 个会话` +
              (result.audit_id ? `；审计 ${result.audit_id}` : ''),
          );
        }
        await load();
      } catch (cause) {
        setActionError(`${node.module_id}：${describeError(cause)}`);
      } finally {
        setBusyId(null);
      }
    },
    [load],
  );

  const toggleTools = (id: string): void => {
    setExpanded((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <section className="page" aria-labelledby="nodes-title">
      <header className="page-head">
        <div>
          <h1 id="nodes-title">节点</h1>
          <p className="muted">
            共 {nodes.length} 个节点 · 数据来自 core 注册表
            {readOnly ? ' · 只读' : ''}
          </p>
        </div>
        <button
          type="button"
          className="Button Button-ghost"
          onClick={() => void load()}
          disabled={loading}
        >
          {loading ? '刷新中…' : '刷新'}
        </button>
      </header>

      {readOnly ? (
        <div className="notice" role="status">
          浏览器会话只能查看。停用 / 启用 / 吊销 / 断开这些写操作在这个形态下不提供 —— 要用 Vade App 打开。
        </div>
      ) : null}

      {loadError ? (
        <div className="alert" role="alert">
          <span>读取节点失败：{loadError}</span>
          <button type="button" className="Button Button-sm" onClick={() => void load()}>
            重试
          </button>
        </div>
      ) : null}

      {actionError ? (
        <div className="alert" role="alert">
          <span>操作失败：{actionError}</span>
          <button type="button" className="Button Button-sm Button-ghost" onClick={() => setActionError('')}>
            知道了
          </button>
        </div>
      ) : null}

      {loading && nodes.length === 0 ? <p className="muted">正在读取节点…</p> : null}

      {!loading && nodes.length === 0 && !loadError ? (
        <div className="empty">
          <h2>还没有模块接上来</h2>
          <p className="muted">在模块侧用配对码接入后，这里会出现它的状态、地址、版本与工具数。</p>
        </div>
      ) : null}

      <ul className="node-list">
        {nodes.map((node) => {
          const isExpanded = expanded.has(node.id);
          const isDisabled = disabled.has(node.id);
          const busy = busyId === node.id;
          return (
            <li key={node.id} className="node-card">
              <div className="node-head">
                <div className="node-title">
                  <h2>{nodeName(node)}</h2>
                  <code className="node-module">{node.module_id}</code>
                </div>
                <div className="node-badges">
                  {node.revoked ? (
                    <span className="badge badge-danger">已吊销</span>
                  ) : (
                    <span className={stateClass(node.state)}>
                      {STATE_LABELS[node.state] ?? node.state}
                    </span>
                  )}
                  {isDisabled ? <span className="badge badge-muted">已停用</span> : null}
                </div>
              </div>

              <dl className="node-meta">
                <div>
                  <dt>实例</dt>
                  <dd>
                    <code>{node.instance_id}</code>
                  </dd>
                </div>
                <div className="node-meta-wide">
                  <dt>地址</dt>
                  <dd>
                    <code>{node.endpoint ?? '—'}</code>
                  </dd>
                </div>
                <div>
                  <dt>版本</dt>
                  <dd>{node.version ?? '—'}</dd>
                </div>
                <div>
                  <dt>RTT</dt>
                  <dd>{formatRtt(node.rtt_ms)}</dd>
                </div>
                <div>
                  <dt>最后心跳</dt>
                  <dd title={absoluteTime(node.last_seen)}>{relativeTime(node.last_seen, now)}</dd>
                </div>
                <div>
                  <dt>工具</dt>
                  <dd>
                    <button
                      type="button"
                      className="link-button"
                      aria-expanded={isExpanded}
                      onClick={() => toggleTools(node.id)}
                    >
                      {node.tool_count} 个{node.tool_count > 0 ? (isExpanded ? ' · 收起' : ' · 查看') : ''}
                    </button>
                  </dd>
                </div>
              </dl>

              {isExpanded && node.tool_names.length > 0 ? (
                <ul className="tool-chips" aria-label="工具列表">
                  {node.tool_names.map((name) => (
                    <li key={name}>
                      <code>{name}</code>
                    </li>
                  ))}
                </ul>
              ) : null}

              {!readOnly && !node.revoked ? (
                <div className="node-actions">
                  <button
                    type="button"
                    className="Button Button-sm"
                    disabled={busy}
                    onClick={() => setPending({ node, kind: isDisabled ? 'enable' : 'disable' })}
                  >
                    {isDisabled ? '启用' : '停用'}
                  </button>
                  <button
                    type="button"
                    className="Button Button-sm"
                    disabled={busy}
                    onClick={() => setPending({ node, kind: 'disconnect' })}
                  >
                    断开
                  </button>
                  <button
                    type="button"
                    className="Button Button-sm Button-danger-ghost"
                    disabled={busy}
                    onClick={() => setPending({ node, kind: 'revoke' })}
                  >
                    吊销
                  </button>
                </div>
              ) : null}
            </li>
          );
        })}
      </ul>

      {pending ? (
        <ConfirmDialog
          title={confirmTitle(pending.kind, pending.node)}
          confirmLabel={
            pending.kind === 'disable'
              ? '停用'
              : pending.kind === 'enable'
                ? '启用'
                : pending.kind === 'disconnect'
                  ? '断开'
                  : '吊销'
          }
          danger={pending.kind === 'revoke' || pending.kind === 'disable'}
          onCancel={() => setPending(null)}
          onConfirm={() => {
            const action = pending;
            setPending(null);
            void runAction(action.node, action.kind);
          }}
        >
          <ConfirmBody kind={pending.kind} node={pending.node} />
        </ConfirmDialog>
      ) : null}

      {flash ? (
        <div className="toast" role="status">
          {flash}
        </div>
      ) : null}
    </section>
  );
}
