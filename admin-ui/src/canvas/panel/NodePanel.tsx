/**
 * 节点属性面板（T5）：基本信息 + 工具列表（分页 20/页）+ L2 治理动作。
 *
 * 面板里**不画工具节点**；工具只以名字出现（D35）。
 * 动作全部走已有契约：
 * - 停用/启用 → `PATCH /admin/api/nodes/{id}` `{enabled}`，响应带 `impact` 与 `audit_id`
 * - 断开      → `POST /admin/api/nodes/{id}/disconnect`
 * 确认框不许只写「确定吗」：停用必须列出将移除的工具名与数量。
 *
 * 浏览器会话（`readOnly`）下不渲染任何写按钮——不是禁用，是不渲染。
 */

import { useEffect, useMemo, useState } from 'react';
import type { ReactElement } from 'react';
import { api, describeError } from '../../app/api';
import { ConfirmDialog } from '../../app/ConfirmDialog';
import { absoluteTime, formatRtt, relativeTime } from '../../app/format';
import type { NodePatchResponse, NodeView } from '../../app/types';
import { encryptionMark, stateLabel, toolBadge, truncateMiddle } from '../format';

const PAGE_SIZE = 20;
/** 工具名在面板里的最大字符数；超长中间省略，悬浮给全名。 */
const TOOL_NAME_MAX = 38;

type ActionKind = 'disable' | 'enable' | 'disconnect';

interface NodePanelProps {
  node: NodeView;
  readOnly: boolean;
  /** 启停状态不在 NodeView 里，只能记本会话操作成功的结果。 */
  disabled: boolean;
  onClose: () => void;
  onEnabledChange: (id: string, enabled: boolean) => void;
  onRefreshNode: (id: string) => Promise<void>;
  onNotice: (tone: 'ok' | 'error', text: string) => void;
}

function stateBadgeClass(state: string): string {
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

/** bridge 节点与上游之间是明文；识别方式与 graph.ts 的形状判定一致（module_id / 能力标记）。 */
function isBridge(node: NodeView): boolean {
  const caps = node.capabilities.map((cap) => cap.toLowerCase());
  return node.module_id.toLowerCase().includes('mcp') || caps.includes('bridge') || caps.includes('mcp');
}

function encryptionCopy(authMode: string | null): { title: string; detail: string } {
  switch (authMode) {
    case 'module_key':
      return {
        title: '端到端加密（Noise IK，module_key）',
        detail: '模块与 core 之间为端到端加密；管理面看到的是 core 的白名单视图。',
      };
    case 'device_key':
      return {
        title: '端到端加密（Noise IK，device_key）',
        detail: '设备与 core 之间为端到端加密；管理面看到的是 core 的白名单视图。',
      };
    case 'browser_session':
      return {
        title: '仅 TLS（browser_session，无端到端）',
        detail: '浏览器会话只有传输层 TLS；管理面本身不持有模块密钥。',
      };
    default:
      return { title: '加密方式未登记', detail: 'core 没有给出 auth_mode，这里不做任何加密暗示。' };
  }
}

function ConfirmBody({ kind, node }: { kind: ActionKind; node: NodeView }): ReactElement {
  if (kind === 'disable') {
    return (
      <div>
        <p>
          停用 <code>{node.module_id}</code> 将从工具的可见表中移除{' '}
          <strong>{node.tool_count}</strong> 个工具，当前会话不受影响、下一轮对话起生效：
        </p>
        {node.tool_names.length > 0 ? (
          <ul className="topo-impact-tools">
            {node.tool_names.map((name) => (
              <li key={name}>
                <code>{name}</code>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">该节点当前没有登记工具。</p>
        )}
      </div>
    );
  }
  if (kind === 'enable') {
    return (
      <p>
        启用 <code>{node.module_id}</code> 后，其 <strong>{node.tool_count}</strong>{' '}
        个工具将在下一轮对话起对模型可见；不会重启模块进程。
      </p>
    );
  }
  return (
    <p>
      断开 <code>
        {node.module_id}#{node.instance_id}
      </code>
      ：立即关闭该节点的连接，模块会自动尝试重连；已登记的密钥与工具不受影响。
    </p>
  );
}

export function NodePanel({
  node,
  readOnly,
  disabled,
  onClose,
  onEnabledChange,
  onRefreshNode,
  onNotice,
}: NodePanelProps): ReactElement {
  const [page, setPage] = useState(0);
  const [confirm, setConfirm] = useState<ActionKind | null>(null);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState<number>(() => Date.now());

  // 换节点时回到第一页，否则会停在上一台设备的第 3 页。
  useEffect(() => {
    setPage(0);
  }, [node.id]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 15_000);
    return () => window.clearInterval(timer);
  }, []);

  const names = node.tool_names;
  const totalPages = Math.max(1, Math.ceil(names.length / PAGE_SIZE));
  const currentPage = Math.min(page, totalPages - 1);
  const pageNames = useMemo(
    () => names.slice(currentPage * PAGE_SIZE, currentPage * PAGE_SIZE + PAGE_SIZE),
    [names, currentPage],
  );

  const lock = encryptionMark(node.auth_mode);
  const encryption = encryptionCopy(node.auth_mode);
  const title = node.label && node.label.length > 0 ? node.label : node.module_id;

  async function runAction(kind: ActionKind): Promise<void> {
    setBusy(true);
    try {
      if (kind === 'disconnect') {
        const result = await api<{ ok: boolean; audit_id?: string | null }>(
          `/admin/api/nodes/${encodeURIComponent(node.id)}/disconnect`,
          { method: 'POST' },
        );
        onNotice(
          'ok',
          `已断开 ${node.module_id}；模块会自动尝试重连` +
            (result.audit_id ? `（审计 ${result.audit_id}）` : ''),
        );
      } else {
        const enabled = kind === 'enable';
        const result = await api<NodePatchResponse>(`/admin/api/nodes/${encodeURIComponent(node.id)}`, {
          method: 'PATCH',
          body: { enabled },
        });
        onEnabledChange(node.id, enabled);
        // impact 是后端返回的，原样展示；前端不自己数。
        onNotice(
          'ok',
          `已${enabled ? '启用' : '停用'} ${node.module_id}：影响 ${result.impact.tool_count} 个工具、` +
            `${result.impact.sessions_affected} 个会话` +
            (result.audit_id ? `（审计 ${result.audit_id}）` : ''),
        );
      }
      await onRefreshNode(node.id);
    } catch (caught) {
      onNotice('error', `操作失败：${describeError(caught)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <aside className="topo-panel" aria-labelledby="topo-panel-title">
      <header className="topo-panel-head">
        <div className="topo-panel-title-wrap">
          <h2 id="topo-panel-title" className="topo-panel-title" title={node.module_id}>
            {title}
          </h2>
          <code className="topo-panel-module">{node.module_id}</code>
        </div>
        <button type="button" className="topo-panel-close" aria-label="关闭面板" onClick={onClose}>
          ×
        </button>
      </header>

      <div className="topo-panel-badges">
        <span className={stateBadgeClass(node.state)}>{stateLabel(node.state)}</span>
        {disabled ? <span className="badge badge-muted">已停用</span> : null}
        {node.revoked ? <span className="badge badge-danger">已吊销</span> : null}
        {isBridge(node) ? (
          <span className="badge badge-warn" title="桥接节点与上游服务之间为明文传输">
            上游明文
          </span>
        ) : null}
      </div>

      <section className="topo-panel-section" aria-labelledby="topo-panel-basic">
        <h3 id="topo-panel-basic">基本信息</h3>
        <dl className="topo-panel-dl">
          <div>
            <dt>实例</dt>
            <dd>
              <code>{node.instance_id || '—'}</code>
            </dd>
          </div>
          <div>
            <dt>地址</dt>
            <dd>
              <code className="topo-panel-endpoint">{node.endpoint ?? '地址未登记'}</code>
            </dd>
          </div>
          <div>
            <dt>协议 / 版本</dt>
            <dd>
              <code>{node.protocol}</code> · {node.version ?? '—'}
            </dd>
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
            <dt>能力标记</dt>
            <dd>
              {node.capabilities.length === 0 ? (
                <span className="muted">未声明</span>
              ) : (
                <span className="topo-panel-caps">
                  {node.capabilities.map((cap) => (
                    <code key={cap}>{cap}</code>
                  ))}
                </span>
              )}
            </dd>
          </div>
        </dl>
      </section>

      <section className="topo-panel-section" aria-labelledby="topo-panel-crypto">
        <h3 id="topo-panel-crypto">传输加密</h3>
        <p className="topo-panel-crypto">
          <span className={`topo-lock topo-lock-${lock.kind}`} aria-hidden="true" />
          <span>{encryption.title}</span>
        </p>
        <p className="muted topo-panel-crypto-detail">{encryption.detail}</p>
      </section>

      <section className="topo-panel-section" aria-labelledby="topo-panel-tools">
        <div className="topo-panel-section-head">
          <h3 id="topo-panel-tools">工具</h3>
          <span className="muted">{toolBadge(node.tool_count)}</span>
        </div>
        {names.length === 0 ? (
          <p className="muted">当前无工具。</p>
        ) : (
          <>
            <ul className="topo-tool-list">
              {pageNames.map((name) => (
                <li key={name}>
                  <code className="topo-tool-name" title={name} aria-label={name}>
                    {truncateMiddle(name, TOOL_NAME_MAX)}
                  </code>
                </li>
              ))}
            </ul>
            {totalPages > 1 ? (
              <div className="topo-tool-pager">
                <button
                  type="button"
                  className="Button Button-ghost Button-sm"
                  onClick={() => setPage((value) => Math.max(0, value - 1))}
                  disabled={currentPage === 0}
                >
                  上一页
                </button>
                <span className="muted">
                  第 {currentPage + 1}/{totalPages} 页 · 共 {names.length} 个
                </span>
                <button
                  type="button"
                  className="Button Button-ghost Button-sm"
                  onClick={() => setPage((value) => Math.min(totalPages - 1, value + 1))}
                  disabled={currentPage >= totalPages - 1}
                >
                  下一页
                </button>
              </div>
            ) : (
              <p className="muted topo-tool-total">共 {names.length} 个</p>
            )}
          </>
        )}
      </section>

      <section className="topo-panel-section" aria-labelledby="topo-panel-actions">
        <h3 id="topo-panel-actions">治理</h3>
        {readOnly ? (
          <div className="notice topo-panel-notice" role="status">
            浏览器会话只能查看。停用 / 启用 / 断开需要 Vade App。
          </div>
        ) : node.revoked ? (
          <p className="muted">该节点已被吊销，需要重新配对才能接入；这里不再提供操作。</p>
        ) : node.state === 'pending' ? (
          <p className="muted">待接入的邀请位没有可治理的连接；模块完成配对后在这里管理。</p>
        ) : (
          <div className="topo-panel-actions">
            <button
              type="button"
              className="Button Button-sm"
              disabled={busy}
              onClick={() => setConfirm(disabled ? 'enable' : 'disable')}
            >
              {disabled ? '启用' : '停用'}
            </button>
            <button
              type="button"
              className="Button Button-sm Button-ghost"
              disabled={busy}
              onClick={() => setConfirm('disconnect')}
            >
              断开
            </button>
          </div>
        )}
      </section>

      {confirm ? (
        <ConfirmDialog
          title={
            confirm === 'disable'
              ? `停用 ${title}？`
              : confirm === 'enable'
                ? `启用 ${title}？`
                : `断开 ${title}？`
          }
          confirmLabel={confirm === 'disable' ? '停用' : confirm === 'enable' ? '启用' : '断开'}
          danger={confirm === 'disable'}
          busy={busy}
          onCancel={() => setConfirm(null)}
          onConfirm={() => {
            const action = confirm;
            setConfirm(null);
            void runAction(action);
          }}
        >
          <ConfirmBody kind={confirm} node={node} />
        </ConfirmDialog>
      ) : null}
    </aside>
  );
}
