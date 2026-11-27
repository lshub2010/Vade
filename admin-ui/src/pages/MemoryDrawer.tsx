import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactElement } from 'react';
import { ApiError, api } from '../app/api';
import { absoluteTime, relativeTime, shortId } from '../app/format';
import {
  canArchive,
  canPromote,
  canRestore,
  describeMemoryError,
  layerLabel,
  memoryPath,
  memoryStatusMeta,
  previewText,
} from '../app/memory';
import type { MemoryAction } from '../app/memory';
import type { MemoryItem } from '../app/types';

/** supersedes 链最多回溯这么多层：再多就只显示 id，避免一次开十几个请求。 */
const CHAIN_MAX = 5;

interface ChainEntry {
  id: string;
  /** null = 这一环读不到（可能已被清理）。 */
  item: MemoryItem | null;
}

export interface MemoryDrawerProps {
  id: string;
  /** 列表刷新后递增，让抽屉重新读一次详情（状态可能刚变）。 */
  refreshKey: number;
  readOnly: boolean;
  /** 页面上的确认框打开时，Esc 交给确认框处理。 */
  dialogOpen: boolean;
  onClose: () => void;
  /** 打开链上的另一条（换抽屉内容，不新开一层）。 */
  onSelect: (id: string) => void;
  onAction: (item: MemoryItem, action: MemoryAction) => void;
}

export function MemoryDrawer({
  id,
  refreshKey,
  readOnly,
  dialogOpen,
  onClose,
  onSelect,
  onAction,
}: MemoryDrawerProps): ReactElement {
  const [detail, setDetail] = useState<MemoryItem | null>(null);
  const [chain, setChain] = useState<ChainEntry[]>([]);
  const [chainOverflow, setChainOverflow] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [missing, setMissing] = useState(false);
  const panelRef = useRef<HTMLElement>(null);

  const loadDetail = useCallback(
    async (signal?: AbortSignal): Promise<void> => {
      setLoading(true);
      setLoadError('');
      try {
        const item = await api<MemoryItem>(memoryPath(id), { signal });
        if (signal?.aborted) return;
        setDetail(item);
        setMissing(false);

        const seen = new Set<string>();
        const ids = (item.supersedes ?? []).filter((value) => {
          if (typeof value !== 'string' || value.length === 0 || value === item.id) return false;
          if (seen.has(value)) return false;
          seen.add(value);
          return true;
        });
        const limited = ids.slice(0, CHAIN_MAX);
        setChainOverflow(Math.max(0, ids.length - limited.length));

        const entries = await Promise.all(
          limited.map(async (oldId): Promise<ChainEntry> => {
            try {
              return { id: oldId, item: await api<MemoryItem>(memoryPath(oldId), { signal }) };
            } catch {
              // 链上一环读不到不算整体失败：如实显示 id，不编内容。
              return { id: oldId, item: null };
            }
          }),
        );
        if (signal?.aborted) return;
        setChain(entries);
      } catch (cause) {
        if (signal?.aborted) return;
        setDetail(null);
        setChain([]);
        setChainOverflow(0);
        setMissing(cause instanceof ApiError && cause.status === 404);
        setLoadError(describeMemoryError(cause));
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [id],
  );

  useEffect(() => {
    const controller = new AbortController();
    setDetail(null);
    setChain([]);
    void loadDetail(controller.signal);
    return () => controller.abort();
  }, [loadDetail, refreshKey]);

  // 抽屉是模态的：锁住背后的滚动，打开时把焦点移进来。
  useEffect(() => {
    panelRef.current?.focus();
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previous;
    };
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape' && !dialogOpen) onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [dialogOpen, onClose]);

  const status = detail ? memoryStatusMeta(detail.status) : null;

  return (
    <div
      className="drawer-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <aside
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="memory-drawer-title"
        tabIndex={-1}
        ref={panelRef}
      >
        <header className="drawer-head">
          <div className="drawer-title">
            <h2 id="memory-drawer-title">记忆详情</h2>
            <div className="drawer-badges">
              <code className="memory-id">{shortId(id)}</code>
              {detail ? (
                <>
                  <span className={status?.className ?? 'badge badge-muted'} title={status?.hint}>
                    {status?.label ?? detail.status}
                  </span>
                  <span className="badge badge-muted">{layerLabel(detail.layer)}层</span>
                  <span className="badge badge-info">{detail.namespace}</span>
                </>
              ) : null}
            </div>
          </div>
          <button type="button" className="Button Button-ghost Button-sm" onClick={onClose}>
            关闭
          </button>
        </header>

        <div className="drawer-body">
          {loading && !detail ? <p className="muted">正在读取条目…</p> : null}

          {loadError ? (
            <div className="alert" role="alert">
              <span>
                {missing ? '条目已不存在：' : '读取记忆详情失败：'}
                {loadError}
              </span>
              <button type="button" className="Button Button-sm" onClick={() => void loadDetail()}>
                重试
              </button>
            </div>
          ) : null}

          {detail ? (
            <>
              <section className="skill-section" aria-labelledby="memory-text-title">
                <div className="skill-section-head">
                  <h3 id="memory-text-title">正文</h3>
                  <span className="muted">
                    {detail.text && detail.text.length > 0 ? `${detail.text.length} 字符` : '无正文'}
                  </span>
                </div>
                <p className="memory-full-text">
                  {detail.text && detail.text.length > 0 ? detail.text : '（这条记忆没有正文）'}
                </p>
                <p className="hint">
                  记忆正文由模型从对话中抽取，按纯文本显示；管理面不提供编辑，也不回传原始对话。
                </p>
              </section>

              <section className="skill-section" aria-labelledby="memory-meta-title">
                <h3 id="memory-meta-title">元信息</h3>
                <dl className="meta-grid">
                  <div>
                    <dt>层</dt>
                    <dd>{layerLabel(detail.layer)}</dd>
                  </div>
                  <div>
                    <dt>命名空间</dt>
                    <dd>
                      <code>{detail.namespace}</code>
                    </dd>
                  </div>
                  <div>
                    <dt>状态</dt>
                    <dd>{status?.label ?? detail.status}</dd>
                  </div>
                  <div>
                    <dt>创建时间</dt>
                    <dd title={absoluteTime(detail.created_at)}>
                      {relativeTime(detail.created_at)} · {absoluteTime(detail.created_at)}
                    </dd>
                  </div>
                  <div>
                    <dt>过期时间</dt>
                    <dd>
                      {typeof detail.expires_at === 'number'
                        ? `${absoluteTime(detail.expires_at)}（${relativeTime(detail.expires_at)}）`
                        : '不过期'}
                    </dd>
                  </div>
                  <div>
                    <dt>命中次数</dt>
                    <dd>{typeof detail.hit_count === 'number' ? detail.hit_count : '—'}</dd>
                  </div>
                  <div>
                    <dt>会话</dt>
                    <dd>{detail.session_id ? <code>{shortId(detail.session_id)}</code> : '—'}</dd>
                  </div>
                  <div>
                    <dt>轮次</dt>
                    <dd>{detail.turn_id ? <code>{shortId(detail.turn_id)}</code> : '—'}</dd>
                  </div>
                </dl>
              </section>

              <section className="skill-section" aria-labelledby="memory-chain-title">
                <div className="skill-section-head">
                  <h3 id="memory-chain-title">替换链（supersedes）</h3>
                  <span className="muted">{chain.length > 0 ? `${chain.length} 条旧版本` : '无'}</span>
                </div>
                {chain.length === 0 ? (
                  <p className="muted">
                    这条记忆没有被替换过，也没有替换别的条目。
                  </p>
                ) : (
                  <ol className="memory-chain">
                    {chain.map((entry) => {
                      const old = entry.item;
                      return (
                        <li key={entry.id} className="memory-chain-item">
                          {old ? (
                            <>
                              <button
                                type="button"
                                className="link-button"
                                onClick={() => onSelect(old.id)}
                              >
                                <code>{shortId(old.id)}</code>
                              </button>
                              <span className="muted">
                                {layerLabel(old.layer)}层 · {old.namespace} ·{' '}
                                {relativeTime(old.created_at)}
                              </span>
                              <span className="memory-chain-text">
                                {previewText(old.text).preview}
                              </span>
                            </>
                          ) : (
                            <>
                              <code>{shortId(entry.id)}</code>
                              <span className="muted">无法读取：条目可能已被清理</span>
                            </>
                          )}
                        </li>
                      );
                    })}
                  </ol>
                )}
                {chainOverflow > 0 ? (
                  <p className="hint">链上还有 {chainOverflow} 条更早的条目未显示。</p>
                ) : null}
                {detail.supersedes && detail.supersedes.length > 0 ? (
                  <p className="hint">
                    这条记忆替换了比自己更早的版本；链上条目保留但不参与召回。
                  </p>
                ) : null}
              </section>

              <section className="skill-section" aria-labelledby="memory-actions-title">
                <h3 id="memory-actions-title">操作</h3>
                {readOnly ? (
                  <div className="notice notice-readonly" role="status">
                    浏览器会话只能查看：晋升 / 归档 / 恢复按钮不渲染。要治理记忆请用 Vade App 打开管理面。
                  </div>
                ) : (
                  <div className="skill-actions">
                    {canPromote(detail.status) ? (
                      <button
                        type="button"
                        className="Button Button-primary Button-sm"
                        onClick={() => onAction(detail, 'promote')}
                      >
                        晋升
                      </button>
                    ) : null}
                    {canArchive(detail.status) ? (
                      <button
                        type="button"
                        className="Button Button-danger-ghost Button-sm"
                        onClick={() => onAction(detail, 'archive')}
                      >
                        归档
                      </button>
                    ) : null}
                    {canRestore(detail.status) ? (
                      <button
                        type="button"
                        className="Button Button-sm"
                        onClick={() => onAction(detail, 'restore')}
                      >
                        恢复
                      </button>
                    ) : null}
                    {!canPromote(detail.status) &&
                    !canArchive(detail.status) &&
                    !canRestore(detail.status) ? (
                      <p className="muted">当前状态没有可用的治理操作。</p>
                    ) : null}
                    <p className="hint">所有治理动作都会写审计；归档理由必填。</p>
                  </div>
                )}
              </section>
            </>
          ) : null}
        </div>
      </aside>
    </div>
  );
}
