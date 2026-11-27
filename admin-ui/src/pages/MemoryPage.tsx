import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent, ReactElement } from 'react';
import { ApiError, api } from '../app/api';
import { useAuth } from '../app/auth';
import { ConfirmDialog } from '../app/ConfirmDialog';
import { absoluteTime, relativeTime, shortId } from '../app/format';
import {
  MEMORY_LIMITS,
  buildMemoryPath,
  canArchive,
  canPromote,
  canRestore,
  defaultMemoryFilters,
  describeMemoryError,
  isFiltered,
  layerLabel,
  memoryActionLabel,
  memoryActionPath,
  memoryStatusMeta,
  previewText,
} from '../app/memory';
import type { MemoryAction, MemoryFilters } from '../app/memory';
import type { MemoryItem, MemoryListResponse, MemoryStats } from '../app/types';
import { MemoryDrawer } from './MemoryDrawer';

interface ActionTarget {
  item: MemoryItem;
  action: MemoryAction;
}

function LayerBadge({ layer }: { layer: string }): ReactElement {
  return (
    <span className={layer === 'global' ? 'badge badge-info' : 'badge badge-muted'}>
      {layerLabel(layer)}层
    </span>
  );
}

function MemoryRow({
  item,
  readOnly,
  now,
  onOpen,
  onAction,
}: {
  item: MemoryItem;
  readOnly: boolean;
  now: number;
  onOpen: (id: string) => void;
  onAction: (item: MemoryItem, action: MemoryAction) => void;
}): ReactElement {
  const status = memoryStatusMeta(item.status);
  const { preview, truncated } = previewText(item.text);

  return (
    <li className="memory-item">
      <div className="memory-item-head">
        <div className="memory-item-badges">
          <span className={status.className} title={status.hint}>
            {status.label}
          </span>
          <LayerBadge layer={item.layer} />
          <span className="badge badge-muted" title="命名空间">
            {item.namespace}
          </span>
        </div>
        <span className="memory-item-time" title={absoluteTime(item.created_at)}>
          {relativeTime(item.created_at, now)}
        </span>
      </div>

      <p className="memory-item-text">
        {preview}
        {truncated ? <span className="muted">（全文在详情里）</span> : null}
      </p>

      <div className="memory-item-foot">
        <code className="memory-id" title={item.id}>
          {shortId(item.id)}
        </code>
        <div className="memory-item-actions">
          <button type="button" className="Button Button-sm Button-ghost" onClick={() => onOpen(item.id)}>
            详情
          </button>
          {readOnly ? null : (
            <>
              {canPromote(item.status) ? (
                <button
                  type="button"
                  className="Button Button-sm"
                  onClick={() => onAction(item, 'promote')}
                >
                  晋升
                </button>
              ) : null}
              {canArchive(item.status) ? (
                <button
                  type="button"
                  className="Button Button-sm Button-danger-ghost"
                  onClick={() => onAction(item, 'archive')}
                >
                  归档
                </button>
              ) : null}
              {canRestore(item.status) ? (
                <button
                  type="button"
                  className="Button Button-sm"
                  onClick={() => onAction(item, 'restore')}
                >
                  恢复
                </button>
              ) : null}
            </>
          )}
        </div>
      </div>
    </li>
  );
}

export function MemoryPage(): ReactElement {
  const { readOnly } = useAuth();
  const [applied, setApplied] = useState<MemoryFilters>(defaultMemoryFilters);
  const [draft, setDraft] = useState<MemoryFilters>(defaultMemoryFilters);
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState('');
  const [moreError, setMoreError] = useState('');
  /** 501：这个构建没装记忆模块。和「条目为空」是两件事，文案不能共用。 */
  const [unsupported, setUnsupported] = useState(false);
  const [stats, setStats] = useState<MemoryStats | null>(null);
  const [statsLoading, setStatsLoading] = useState(true);
  const [statsError, setStatsError] = useState('');
  const [openId, setOpenId] = useState<string | null>(null);
  const [target, setTarget] = useState<ActionTarget | null>(null);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const [flash, setFlash] = useState('');
  const [reloadKey, setReloadKey] = useState(0);
  const [now, setNow] = useState<number>(() => Date.now());
  const inflight = useRef<AbortController | null>(null);

  // 相对时间要自己走，不然「3 分钟前」会停在那儿。
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  // 条件变化 = 新查询：清空旧行与游标，从第一页开始。
  useEffect(() => {
    inflight.current?.abort();
    const controller = new AbortController();
    inflight.current = controller;

    setLoading(true);
    setError('');
    setMoreError('');
    setItems([]);
    setNextCursor(null);

    api<MemoryListResponse>(buildMemoryPath(applied, null), { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        setItems(data.items);
        setNextCursor(data.next_cursor);
        setUnsupported(false);
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        if (cause instanceof ApiError && cause.status === 501) {
          setUnsupported(true);
          return;
        }
        setError(describeMemoryError(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });

    return () => controller.abort();
  }, [applied, reloadKey]);

  useEffect(() => {
    const controller = new AbortController();
    setStatsLoading(true);
    setStatsError('');
    api<MemoryStats>('/admin/api/memory/stats', { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        setStats(data);
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        // 配额 501 不把整页判成「未安装」：能不能治理以列表为准。
        if (cause instanceof ApiError && cause.status === 501) {
          setStats(null);
          setStatsError('本构建未提供记忆配额（501 UNSUPPORTED）');
          return;
        }
        setStats(null);
        setStatsError(describeMemoryError(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setStatsLoading(false);
      });
    return () => controller.abort();
  }, [reloadKey]);

  useEffect(() => {
    if (!flash) return;
    const timer = window.setTimeout(() => setFlash(''), 8_000);
    return () => window.clearTimeout(timer);
  }, [flash]);

  const loadMore = useCallback(async (): Promise<void> => {
    if (nextCursor === null || loading || loadingMore) return;
    const controller = new AbortController();
    inflight.current = controller;
    setLoadingMore(true);
    setMoreError('');
    try {
      const data = await api<MemoryListResponse>(buildMemoryPath(applied, nextCursor), {
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      setItems((previous) => {
        const seen = new Set(previous.map((item) => item.id));
        return [...previous, ...data.items.filter((item) => !seen.has(item.id))];
      });
      setNextCursor(data.next_cursor);
    } catch (cause) {
      if (controller.signal.aborted) return;
      setMoreError(describeMemoryError(cause));
    } finally {
      if (!controller.signal.aborted) setLoadingMore(false);
    }
  }, [applied, loading, loadingMore, nextCursor]);

  const submitFilters = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    setApplied(draft);
  };

  const resetFilters = (): void => {
    const fresh = defaultMemoryFilters();
    setDraft(fresh);
    setApplied(fresh);
  };

  const requestAction = useCallback((item: MemoryItem, action: MemoryAction): void => {
    setTarget({ item, action });
    setReason('');
    setActionError('');
  }, []);

  const closeDialog = useCallback((): void => {
    if (busy) return;
    setTarget(null);
    setReason('');
    setActionError('');
  }, [busy]);

  // 归档弹窗自己处理 Esc；ConfirmDialog 处理另外两种。
  useEffect(() => {
    if (target === null || target.action !== 'archive') return;
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape' && !busy) closeDialog();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [target, busy, closeDialog]);

  const submitAction = useCallback(async (): Promise<void> => {
    if (target === null || busy) return;
    if (target.action === 'archive' && reason.trim().length === 0) {
      setActionError('归档理由必填：写清为什么要移除这条记忆，它会进审计。');
      return;
    }
    setBusy(true);
    setActionError('');
    try {
      await api<{ ok: boolean }>(memoryActionPath(target.item.id, target.action), {
        method: 'POST',
        body: target.action === 'archive' ? { reason: reason.trim() } : undefined,
      });
      const label = memoryActionLabel(target.action);
      setTarget(null);
      setReason('');
      setFlash(`已${label} ${shortId(target.item.id)}：列表与配额已刷新（写审计）`);
      setReloadKey((value) => value + 1);
    } catch (cause) {
      setActionError(describeMemoryError(cause));
      // 404 / 409 说明状态已经变了：刷新列表，别让人对着旧状态重试。
      if (cause instanceof ApiError && (cause.status === 404 || cause.status === 409)) {
        setReloadKey((value) => value + 1);
      }
    } finally {
      setBusy(false);
    }
  }, [busy, reason, target]);

  const filtered = isFiltered(applied);
  const limit = applied.limit;
  const globalCount = stats?.global_count ?? 0;
  const globalLimit = stats?.global_limit ?? 0;
  const atLimit = globalLimit > 0 && globalCount >= globalLimit;
  const usedPercent = globalLimit > 0 ? Math.min(100, Math.round((globalCount / globalLimit) * 100)) : 0;

  const summary = useMemo(() => {
    if (unsupported) return '记忆模块未安装';
    if (loading) return '正在读取…';
    const parts = [`已加载 ${items.length} 条`];
    if (filtered) parts.push('已应用筛选');
    if (nextCursor !== null) parts.push('还有更多');
    return parts.join(' · ');
  }, [filtered, items.length, loading, nextCursor, unsupported]);

  return (
    <section className="page" aria-labelledby="memory-title">
      <header className="page-head">
        <div>
          <h1 id="memory-title">记忆</h1>
          <p className="muted">
            staging 晋升后才进入全局召回 · 归档需理由且可恢复 · 治理动作都写审计
            {readOnly ? ' · 只读' : ''}
          </p>
        </div>
        <button
          type="button"
          className="Button Button-ghost"
          onClick={() => setReloadKey((value) => value + 1)}
          disabled={loading}
        >
          {loading ? '刷新中…' : '刷新'}
        </button>
      </header>

      {readOnly && !unsupported ? (
        <div className="notice notice-readonly" role="status">
          浏览器会话只能查看：列表与详情可读，晋升 / 归档 / 恢复按钮不渲染。要治理记忆请用 Vade App 打开管理面。
        </div>
      ) : null}

      {statsError ? (
        <div className="alert" role="alert">
          <span>记忆配额不可用：{statsError}</span>
          <button
            type="button"
            className="Button Button-sm"
            onClick={() => setReloadKey((value) => value + 1)}
          >
            重试
          </button>
        </div>
      ) : null}

      {!unsupported && stats ? (
        <div className="memory-stats" aria-label="记忆配额">
          <div className="memory-stat">
            <span className="memory-stat-label">全局条目</span>
            <span className="memory-stat-value">
              {globalCount}
              <span className="muted"> / {globalLimit}</span>
            </span>
            <div className={atLimit ? 'progress progress-full' : 'progress'}>
              <span className="progress-fill" style={{ width: `${usedPercent}%` }} />
            </div>
            <span className="memory-stat-hint">
              {atLimit
                ? '已达配额：新的全局条目会被淘汰或转入 staging'
                : globalLimit > 0
                  ? `剩余 ${Math.max(0, globalLimit - globalCount)} 条`
                  : '配额未报告'}
            </span>
          </div>
          <div className="memory-stat">
            <span className="memory-stat-label">待确认 staging</span>
            <span className="memory-stat-value">{stats.staging_count}</span>
            <span className="memory-stat-hint">不参与召回，晋升后生效</span>
          </div>
          <div className="memory-stat">
            <span className="memory-stat-label">已归档</span>
            <span className="memory-stat-value">{stats.archived_count}</span>
            <span className="memory-stat-hint">内容保留，可恢复</span>
          </div>
          <div className="memory-stat">
            <span className="memory-stat-label">最早条目</span>
            <span className="memory-stat-value" title={absoluteTime(stats.oldest_at)}>
              {stats.oldest_at === null ? '—' : relativeTime(stats.oldest_at, now)}
            </span>
            <span className="memory-stat-hint">
              {stats.oldest_at === null ? '空库' : absoluteTime(stats.oldest_at)}
            </span>
          </div>
        </div>
      ) : null}

      {!unsupported && statsLoading && stats === null && !statsError ? (
        <p className="muted">正在读取配额…</p>
      ) : null}

      {!unsupported ? (
        <form className="audit-filters" onSubmit={submitFilters} aria-label="记忆筛选">
          <div className="audit-filter-grid">
            <label className="field">
              <span className="field-label">层</span>
              <select
                className="input"
                value={draft.layer}
                onChange={(event) =>
                  setDraft({ ...draft, layer: event.target.value as MemoryFilters['layer'] })
                }
              >
                <option value="">全部层</option>
                <option value="session">会话 session</option>
                <option value="global">全局 global</option>
              </select>
            </label>
            <label className="field">
              <span className="field-label">状态</span>
              <select
                className="input"
                value={draft.status}
                onChange={(event) =>
                  setDraft({ ...draft, status: event.target.value as MemoryFilters['status'] })
                }
              >
                <option value="">全部状态</option>
                <option value="staging">待确认 staging</option>
                <option value="active">生效中 active</option>
                <option value="archived">已归档 archived</option>
                <option value="superseded">已被替换 superseded</option>
              </select>
            </label>
            <label className="field">
              <span className="field-label">命名空间</span>
              <input
                className="input"
                type="text"
                placeholder="如 default"
                autoComplete="off"
                spellCheck={false}
                value={draft.namespace}
                onChange={(event) => setDraft({ ...draft, namespace: event.target.value })}
              />
            </label>
            <label className="field">
              <span className="field-label">关键字</span>
              <input
                className="input"
                type="search"
                placeholder="在正文里匹配"
                autoComplete="off"
                spellCheck={false}
                value={draft.q}
                onChange={(event) => setDraft({ ...draft, q: event.target.value })}
              />
            </label>
            <label className="field">
              <span className="field-label">每页</span>
              <select
                className="input"
                value={String(draft.limit)}
                onChange={(event) =>
                  setDraft({
                    ...draft,
                    limit: Number(event.target.value) as MemoryFilters['limit'],
                  })
                }
              >
                {MEMORY_LIMITS.map((value) => (
                  <option key={value} value={String(value)}>
                    {value} 条
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div className="audit-filter-actions">
            <button type="submit" className="Button Button-primary" disabled={loading}>
              查询
            </button>
            <button type="button" className="Button Button-ghost" onClick={resetFilters}>
              重置
            </button>
            <span className="hint">
              改条件后点「查询」：游标重置，从第一页开始；每页 {limit} 条。
            </span>
          </div>
        </form>
      ) : null}

      {error ? (
        <div className="alert" role="alert">
          <span>读取记忆列表失败：{error}</span>
          <button
            type="button"
            className="Button Button-sm"
            onClick={() => setReloadKey((value) => value + 1)}
          >
            重试
          </button>
        </div>
      ) : null}

      {!unsupported ? (
        <p className="audit-statusline" role="status">
          <span>
            {applied.layer === '' ? '全部层' : `${layerLabel(applied.layer)}层`}
            {applied.status === '' ? ' · 全部状态' : ` · ${memoryStatusMeta(applied.status).label}`}
            {applied.namespace.trim() ? ` · 命名空间 ${applied.namespace.trim()}` : ''}
            {applied.q.trim() ? ` · 关键字「${applied.q.trim()}」` : ''}
          </span>
          <span>{summary}</span>
        </p>
      ) : null}

      {unsupported ? (
        <div className="empty empty-left">
          <h2>记忆模块未安装</h2>
          <p>
            core 返回 <code>501 UNSUPPORTED</code>：这个构建没有装配记忆后端，也没有可治理的条目。
            换一个带记忆模块的构建后这里会显示列表与配额，现在不渲染假控件、不显示 0 条配额。
          </p>
          <p className="hint">
            契约已冻结（<code>schema/admin-api.yaml</code> 的 <code>/admin/api/memory</code>）；本页只依赖
            列表、详情、配额与晋升 / 归档 / 恢复六个端点。
          </p>
        </div>
      ) : null}

      {!unsupported && loading && items.length === 0 && !error ? (
        <p className="muted">正在读取记忆…</p>
      ) : null}

      {!unsupported && !loading && items.length === 0 && !error ? (
        <div className="empty empty-left">
          <h2>{filtered ? '没有符合条件的记忆' : '还没有记忆条目'}</h2>
          <p>
            {filtered
              ? '层、状态、命名空间、关键字都可能把结果筛空；清空筛选再看全部。'
              : '会话记忆与全局记忆都由 core 自动写入。staging 条目要晋升后才进入全局召回；归档过的条目会移到「已归档」。'}
          </p>
          {filtered ? (
            <button type="button" className="Button Button-sm" onClick={resetFilters}>
              清空筛选
            </button>
          ) : null}
        </div>
      ) : null}

      {!unsupported && items.length > 0 ? (
        <>
          <ul className="memory-list">
            {items.map((item) => (
              <MemoryRow
                key={item.id}
                item={item}
                readOnly={readOnly}
                now={now}
                onOpen={setOpenId}
                onAction={requestAction}
              />
            ))}
          </ul>

          <div className="audit-footer">
            <span className="muted">
              {nextCursor !== null
                ? `已加载 ${items.length} 条，还有更早的条目`
                : `已加载 ${items.length} 条，已到最早一条`}
            </span>
            {nextCursor !== null ? (
              <button
                type="button"
                className="Button Button-sm"
                onClick={() => void loadMore()}
                disabled={loadingMore}
              >
                {loadingMore ? '加载中…' : '加载更多'}
              </button>
            ) : null}
          </div>

          {moreError ? (
            <div className="alert" role="alert">
              <span>加载更多失败：{moreError}</span>
              <button type="button" className="Button Button-sm" onClick={() => void loadMore()}>
                重试
              </button>
            </div>
          ) : null}
        </>
      ) : null}

      {openId ? (
        <MemoryDrawer
          id={openId}
          refreshKey={reloadKey}
          readOnly={readOnly}
          dialogOpen={target !== null}
          onClose={() => setOpenId(null)}
          onSelect={setOpenId}
          onAction={requestAction}
        />
      ) : null}

      {target !== null && target.action === 'archive' ? (
        <div
          className="modal-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeDialog();
          }}
        >
          <div className="modal" role="dialog" aria-modal="true" aria-labelledby="memory-archive-title">
            <h2 id="memory-archive-title" className="modal-title">
              归档记忆
            </h2>
            <div className="modal-body">
              <p>
                归档后这条记忆不再参与召回，内容保留，可随时恢复。操作会写审计，理由会一起记下。
              </p>
              <p className="muted">
                <code>{shortId(target.item.id)}</code> · {previewText(target.item.text).preview}
              </p>
              <label className="field">
                <span className="field-label">归档理由（必填）</span>
                <textarea
                  className="input textarea"
                  rows={3}
                  maxLength={300}
                  autoFocus
                  value={reason}
                  placeholder="如：与现行偏好冲突，由用户确认移除"
                  onChange={(event) => setReason(event.target.value)}
                />
              </label>
              <p className="hint">{reason.trim().length}/300 字符</p>
              {actionError ? (
                <p className="form-error" role="alert">
                  {actionError}
                </p>
              ) : null}
            </div>
            <div className="modal-actions">
              <button type="button" className="Button Button-ghost" onClick={closeDialog} disabled={busy}>
                取消
              </button>
              <button
                type="button"
                className="Button Button-danger"
                onClick={() => void submitAction()}
                disabled={busy || reason.trim().length === 0}
              >
                {busy ? '执行中…' : '归档'}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {target !== null && target.action !== 'archive' ? (
        <ConfirmDialog
          title={target.action === 'promote' ? '晋升这条记忆？' : '恢复这条记忆？'}
          confirmLabel={target.action === 'promote' ? '晋升' : '恢复'}
          busy={busy}
          onConfirm={() => void submitAction()}
          onCancel={closeDialog}
        >
          <p>
            {target.action === 'promote'
              ? '晋升后状态从 staging 变为 active，从下一轮对话起参与全局召回；不改变正文。操作写审计。'
              : '恢复后状态回到 active，重新参与召回；正文与创建时间不变。操作写审计。'}
          </p>
          <p className="muted">
            <code>{shortId(target.item.id)}</code> · {layerLabel(target.item.layer)}层 ·{' '}
            {target.item.namespace}
          </p>
          <p className="muted">{previewText(target.item.text).preview}</p>
          {actionError ? (
            <p className="form-error" role="alert">
              {actionError}
            </p>
          ) : null}
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
