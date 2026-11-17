import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent, ReactElement } from 'react';
import { api, describeError } from '../app/api';
import {
  PAGE_LIMIT,
  PRESETS,
  TURN_LIMIT,
  auditJson,
  buildAuditPath,
  clockTime,
  defaultFilters,
  formatDuration,
  formatGap,
  fromDateTimeLocal,
  isFiltered,
  kindLabel,
  rangeBounds,
  rangeLabel,
  statusClass,
  statusLabel,
  summarizeRow,
  toDateTimeLocal,
} from '../app/audit';
import type { AuditFilters, RangePreset } from '../app/audit';
import { useAuth } from '../app/auth';
import { absoluteTime, relativeTime, shortId } from '../app/format';
import type { AuditListResponse, AuditRow } from '../app/types';

/** 天表 id 跨天可重复，key 里必须带 ts；缺 id 时再退到 kind。 */
function rowKey(row: AuditRow): string {
  return `${row.ts}:${row.id ?? 'x'}:${row.kind}`;
}

function sessionLabel(sessionId: string | null | undefined): string {
  return sessionId ? shortId(sessionId) : '—';
}

function JsonBlock({ row }: { row: AuditRow }): ReactElement {
  return (
    <>
      <div className="audit-json-head">
        <span>原始行（只读 · 服务端已脱敏）</span>
        <span className="muted">
          digest <code>{row.args_digest ? shortId(row.args_digest) : '—'}</code>
        </span>
      </div>
      <pre className="audit-json">{auditJson(row)}</pre>
    </>
  );
}

export function AuditPage(): ReactElement {
  const { readOnly } = useAuth();
  const [applied, setApplied] = useState<AuditFilters>(defaultFilters);
  const [draft, setDraft] = useState<AuditFilters>(defaultFilters);
  const [items, setItems] = useState<AuditRow[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState('');
  const [moreError, setMoreError] = useState('');
  const [formError, setFormError] = useState('');
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  /** 刷新按钮不改条件，只让同一个查询重跑一次。 */
  const [reloadKey, setReloadKey] = useState(0);
  const [now, setNow] = useState<number>(() => Date.now());
  const inflight = useRef<AbortController | null>(null);

  const turnMode = applied.turnId.trim().length > 0;

  // 相对时间要自己走，不然「12 秒前」会停在那儿。
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 15_000);
    return () => window.clearInterval(timer);
  }, []);

  // 条件变化 = 新查询：清空旧行、从第一页开始。turn 回放一次取满 500。
  useEffect(() => {
    inflight.current?.abort();
    const controller = new AbortController();
    inflight.current = controller;

    const limit = applied.turnId.trim().length > 0 ? TURN_LIMIT : PAGE_LIMIT;
    setLoading(true);
    setError('');
    setMoreError('');
    setItems([]);
    setNextCursor(null);
    setExpanded(new Set());

    api<AuditListResponse>(buildAuditPath(applied, null, limit), { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        setItems(data.items);
        setNextCursor(data.next_cursor);
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setError(describeError(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });

    return () => controller.abort();
  }, [applied, reloadKey]);

  const loadMore = useCallback(async (): Promise<void> => {
    if (nextCursor === null || loading || loadingMore) return;
    const controller = new AbortController();
    inflight.current = controller;
    setLoadingMore(true);
    setMoreError('');
    try {
      const data = await api<AuditListResponse>(
        buildAuditPath(applied, nextCursor, PAGE_LIMIT),
        { signal: controller.signal },
      );
      if (controller.signal.aborted) return;
      setItems((previous) => {
        const seen = new Set(previous.map(rowKey));
        return [...previous, ...data.items.filter((row) => !seen.has(rowKey(row)))];
      });
      setNextCursor(data.next_cursor);
    } catch (cause) {
      if (controller.signal.aborted) return;
      setMoreError(describeError(cause));
    } finally {
      if (!controller.signal.aborted) setLoadingMore(false);
    }
  }, [applied, loading, loadingMore, nextCursor]);

  const apply = useCallback((next: AuditFilters): void => {
    setDraft(next);
    setApplied(next);
    setFormError('');
  }, []);

  const choosePreset = useCallback(
    (preset: RangePreset): void => {
      apply({ ...draft, range: { kind: 'preset', preset } });
    },
    [apply, draft],
  );

  /** 切到自定义只是把面板展开；真正查询还要点「查询」。 */
  const chooseCustom = useCallback((): void => {
    if (draft.range.kind === 'custom') return;
    const bounds = rangeBounds(draft.range);
    setDraft({
      ...draft,
      range: {
        kind: 'custom',
        from: bounds.from ?? Math.floor(Date.now() / 1_000) - 86_400,
        to: bounds.to,
      },
    });
  }, [draft]);

  /**
   * 点某一行的「轮次」：只按 turn_id 查、时间放开到全部。
   * 回放要的是完整链，留着 24h 窗口会把更早的步骤截掉，看起来像链断了。
   */
  const replayTurn = useCallback((turnId: string): void => {
    const next: AuditFilters = {
      ...defaultFilters(),
      range: { kind: 'preset', preset: 'all' },
      turnId,
    };
    setDraft(next);
    setApplied(next);
    setFormError('');
  }, []);

  const submit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    const bounds = rangeBounds(draft.range);
    if (bounds.from !== null && bounds.to !== null && bounds.from > bounds.to) {
      setFormError('开始时间晚于结束时间，调整后再查询。');
      return;
    }
    apply(draft);
  };

  const reset = (): void => {
    const fresh = defaultFilters();
    setDraft(fresh);
    setApplied(fresh);
    setFormError('');
  };

  const toggle = useCallback((key: string): void => {
    setExpanded((previous) => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  /** 只有 turn 回放按升序；列表恒为 reader 的 ts DESC。 */
  const visible = useMemo(() => {
    const sessionId = applied.sessionId.trim();
    const rows = sessionId.length > 0 ? items.filter((row) => row.session_id === sessionId) : items;
    if (!turnMode) return rows;
    return [...rows].sort((left, right) => left.ts - right.ts || (left.id ?? 0) - (right.id ?? 0));
  }, [applied.sessionId, items, turnMode]);

  const moduleOptions = useMemo(() => {
    const names = items
      .map((row) => row.module_id)
      .filter((value): value is string => typeof value === 'string' && value.length > 0);
    return [...new Set(names)].sort();
  }, [items]);

  const kindOptions = useMemo(() => {
    const seen = items.map((row) => row.kind).filter((value) => value.length > 0);
    return [...new Set(seen)].sort();
  }, [items]);

  const truncated = turnMode && nextCursor !== null;
  const filtered = isFiltered(applied);
  const rangeCapped = turnMode && !(applied.range.kind === 'preset' && applied.range.preset === 'all');

  return (
    <section className="page" aria-labelledby="audit-title">
      <header className="page-head">
        <div>
          <h1 id="audit-title">审计</h1>
          <p className="muted">
            只追加的审计流 · {turnMode ? '轮次回放（按时间升序）' : '时间倒序（最新在上）'} · 本页只读
            {readOnly ? ' · 浏览器会话' : ''}
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

      <form className="audit-filters" onSubmit={submit} aria-label="审计筛选">
        <div className="audit-range">
          <span className="audit-range-label">时间范围</span>
          {PRESETS.map((preset) => (
            <button
              key={preset.id}
              type="button"
              className="audit-preset"
              aria-pressed={applied.range.kind === 'preset' && applied.range.preset === preset.id}
              onClick={() => choosePreset(preset.id)}
            >
              {preset.label}
            </button>
          ))}
          <button
            type="button"
            className="audit-preset"
            aria-pressed={applied.range.kind === 'custom'}
            onClick={chooseCustom}
          >
            自定义
          </button>
        </div>

        {draft.range.kind === 'custom' ? (
          <div className="audit-custom">
            <label className="field">
              <span className="field-label">从</span>
              <input
                className="input"
                type="datetime-local"
                step={1}
                value={toDateTimeLocal(draft.range.from)}
                onChange={(event) => {
                  if (draft.range.kind !== 'custom') return;
                  setDraft({
                    ...draft,
                    range: { ...draft.range, from: fromDateTimeLocal(event.target.value) },
                  });
                }}
              />
            </label>
            <label className="field">
              <span className="field-label">到（留空 = 现在）</span>
              <input
                className="input"
                type="datetime-local"
                step={1}
                value={toDateTimeLocal(draft.range.to)}
                onChange={(event) => {
                  if (draft.range.kind !== 'custom') return;
                  setDraft({
                    ...draft,
                    range: { ...draft.range, to: fromDateTimeLocal(event.target.value) },
                  });
                }}
              />
            </label>
          </div>
        ) : null}

        <div className="audit-filter-grid">
          <label className="field">
            <span className="field-label">模块 ID</span>
            <input
              className="input"
              type="text"
              list="audit-module-options"
              placeholder="com.example.fs"
              autoComplete="off"
              spellCheck={false}
              value={draft.moduleId}
              onChange={(event) => setDraft({ ...draft, moduleId: event.target.value })}
            />
          </label>
          <label className="field">
            <span className="field-label">类型 kind</span>
            <input
              className="input"
              type="text"
              list="audit-kind-options"
              placeholder="tool_call"
              autoComplete="off"
              spellCheck={false}
              value={draft.kind}
              onChange={(event) => setDraft({ ...draft, kind: event.target.value })}
            />
          </label>
          <label className="field">
            <span className="field-label">轮次 turn_id</span>
            <input
              className="input"
              type="text"
              placeholder="整条链按时间升序回放"
              autoComplete="off"
              spellCheck={false}
              value={draft.turnId}
              onChange={(event) => setDraft({ ...draft, turnId: event.target.value })}
            />
          </label>
          <label className="field">
            <span className="field-label">会话 session_id</span>
            <input
              className="input"
              type="text"
              placeholder="只在已加载的行里过滤"
              autoComplete="off"
              spellCheck={false}
              value={draft.sessionId}
              onChange={(event) => setDraft({ ...draft, sessionId: event.target.value })}
            />
          </label>
        </div>

        <div className="audit-filter-actions">
          <button type="submit" className="Button Button-primary">
            查询
          </button>
          <button type="button" className="Button Button-ghost" onClick={reset}>
            重置
          </button>
          <span className="hint">
            预设立即生效；自定义时间与文本条件点「查询」。每页 {PAGE_LIMIT} 条，游标分页。
          </span>
        </div>

        {formError ? (
          <p className="form-error" role="alert">
            {formError}
          </p>
        ) : null}

        <datalist id="audit-module-options">
          {moduleOptions.map((name) => (
            <option key={name} value={name} />
          ))}
        </datalist>
        <datalist id="audit-kind-options">
          {kindOptions.map((name) => (
            <option key={name} value={name} />
          ))}
        </datalist>
      </form>

      {error ? (
        <div className="alert" role="alert">
          <span>读取审计失败：{error}</span>
          <button
            type="button"
            className="Button Button-sm"
            onClick={() => setReloadKey((value) => value + 1)}
          >
            重试
          </button>
        </div>
      ) : null}

      <p className="audit-statusline" role="status">
        <span>
          范围 {rangeLabel(applied.range)}
          {applied.moduleId.trim() ? ` · 模块 ${applied.moduleId.trim()}` : ''}
          {applied.kind.trim() ? ` · kind ${applied.kind.trim()}` : ''}
          {applied.turnId.trim() ? ` · 轮次 ${shortId(applied.turnId.trim())}` : ''}
          {applied.sessionId.trim() ? ` · 会话 ${shortId(applied.sessionId.trim())}（仅已加载行）` : ''}
        </span>
        <span>
          {loading
            ? '正在读取…'
            : `已加载 ${visible.length} 条${applied.sessionId.trim() ? ` / ${items.length}` : ''}`}
          {!loading && !turnMode && nextCursor !== null ? ' · 还有更早的记录' : ''}
          {!loading && !turnMode && nextCursor === null && items.length > 0 ? ' · 已到最早一条' : ''}
        </span>
      </p>

      {loading && items.length === 0 && !error ? <p className="muted">正在读取审计…</p> : null}

      {!loading && !error && items.length === 0 ? (
        <div className="empty empty-left">
          <h2>{filtered ? '没有符合条件的记录' : '还没有审计记录'}</h2>
          <p>
            {filtered
              ? '时间范围、模块、kind、轮次都可能把结果筛空；核对条件后重试。session_id 只过滤已加载的行，空结果不代表服务端没有。'
              : '审计只追加：管理面登录、配置变更、审批与工具调用都会落一行。core 还没写入任何事件时这里是空的。'}
          </p>
          {filtered ? (
            <button type="button" className="Button Button-sm" onClick={reset}>
              清空筛选
            </button>
          ) : null}
        </div>
      ) : null}

      {turnMode && visible.length > 0 ? (
        <section className="audit-replay" aria-labelledby="audit-replay-title">
          <div className="audit-replay-head">
            <div>
              <h2 id="audit-replay-title">轮次回放</h2>
              <p className="muted">
                <code>{applied.turnId.trim()}</code> · {visible.length} 步 · 按 ts 升序（LLM 调用 → 工具 →
                结果 → 下一轮）
              </p>
            </div>
            <button type="button" className="Button Button-ghost Button-sm" onClick={reset}>
              退出回放
            </button>
          </div>

          {truncated ? (
            <div className="notice notice-readonly" role="status">
              这一轮超过 {TURN_LIMIT} 条，只取到最新的 {TURN_LIMIT} 条：链首可能缺失。按 kind 或模块再收窄。
            </div>
          ) : null}

          {rangeCapped ? (
            <div className="notice notice-readonly" role="status">
              当前时间范围（{rangeLabel(applied.range)}）可能截断链首。
              <button
                type="button"
                className="link-button"
                onClick={() => apply({ ...draft, range: { kind: 'preset', preset: 'all' } })}
              >
                改为全部时间
              </button>
            </div>
          ) : null}

          <ol className="audit-chain">
            {visible.map((row, index) => {
              const key = rowKey(row);
              const open = expanded.has(key);
              const summary = summarizeRow(row);
              const previous = index > 0 ? visible[index - 1] : null;
              const gap = previous ? row.ts - previous.ts : null;
              return (
                <li className="audit-step" key={key}>
                  <span className="audit-step-no" aria-hidden="true">
                    {index + 1}
                  </span>
                  <div className="audit-step-body">
                    <div className="audit-step-head">
                      <code className="audit-kind" title={kindLabel(row.kind)}>
                        {row.kind}
                      </code>
                      <span className={statusClass(row.status)}>{statusLabel(row.status)}</span>
                      <span className="audit-step-time" title={absoluteTime(row.ts)}>
                        {clockTime(row.ts)}
                      </span>
                      {gap !== null ? <span className="muted">+{formatGap(gap)}</span> : null}
                    </div>
                    <p className="audit-step-title">
                      {summary.title}
                      {summary.detail ? <span className="muted"> · {summary.detail}</span> : null}
                    </p>
                    <p className="audit-step-meta muted">
                      模块 {row.module_id ? <code>{row.module_id}</code> : '—'} · 会话{' '}
                      <code>{sessionLabel(row.session_id)}</code>
                      {typeof row.duration_ms === 'number' ? ` · ${formatDuration(row.duration_ms)}` : ''}
                    </p>
                    <button
                      type="button"
                      className="link-button"
                      aria-expanded={open}
                      onClick={() => toggle(key)}
                    >
                      {open ? '收起 JSON' : '原始 JSON'}
                    </button>
                    {open ? <JsonBlock row={row} /> : null}
                  </div>
                </li>
              );
            })}
          </ol>
        </section>
      ) : null}

      {!turnMode && items.length > 0 ? (
        <>
          <div className="audit-table-wrap">
            <table className="audit-table">
              <thead>
                <tr>
                  <th scope="col">时间</th>
                  <th scope="col">类型</th>
                  <th scope="col">模块</th>
                  <th scope="col">会话 / 轮次</th>
                  <th scope="col">摘要</th>
                  <th scope="col">原始</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((row) => {
                  const key = rowKey(row);
                  const open = expanded.has(key);
                  const summary = summarizeRow(row);
                  return (
                    <Fragment key={key}>
                      <tr
                        className={open ? 'audit-row audit-row-open' : 'audit-row'}
                        onClick={() => toggle(key)}
                      >
                        <td data-label="时间">
                          <span className="audit-ts-cell">
                            <span className="audit-ts" title={absoluteTime(row.ts)}>
                              {clockTime(row.ts)}
                            </span>
                            <span className="audit-rel muted">{relativeTime(row.ts, now)}</span>
                          </span>
                        </td>
                        <td data-label="类型">
                          <span className="audit-kind-cell">
                            <code className="audit-kind" title={kindLabel(row.kind)}>
                              {row.kind}
                            </code>
                            <span className={statusClass(row.status)}>{statusLabel(row.status)}</span>
                          </span>
                        </td>
                        <td data-label="模块">
                          <span className="audit-module-cell">
                            {row.module_id ? (
                              <code>{row.module_id}</code>
                            ) : (
                              <span className="muted">—</span>
                            )}
                            {row.instance_id ? (
                              <span className="audit-instance muted">
                                {shortId(row.instance_id)}
                              </span>
                            ) : null}
                          </span>
                        </td>
                        <td data-label="会话 / 轮次">
                          <span className="audit-ids-cell">
                            <code className="audit-id">{sessionLabel(row.session_id)}</code>
                            {row.turn_id ? (
                              <button
                                type="button"
                                className="link-button audit-turn-link"
                                title="按这一轮回放完整链"
                                onClick={(event) => {
                                  event.stopPropagation();
                                  replayTurn(row.turn_id as string);
                                }}
                              >
                                轮次 {shortId(row.turn_id)}
                              </button>
                            ) : null}
                          </span>
                        </td>
                        <td data-label="摘要">
                          <span className="audit-summary-cell">
                            <span className="audit-summary-primary">{summary.title}</span>
                            {summary.detail ? (
                              <span className="audit-summary-sub muted">{summary.detail}</span>
                            ) : null}
                          </span>
                        </td>
                        <td data-label="原始 JSON" className="audit-json-cell">
                          <button
                            type="button"
                            className="Button Button-sm Button-ghost audit-expand"
                            aria-expanded={open}
                            onClick={(event) => {
                              event.stopPropagation();
                              toggle(key);
                            }}
                          >
                            {open ? '收起' : 'JSON'}
                          </button>
                        </td>
                      </tr>
                      {open ? (
                        <tr className="audit-json-row">
                          <td colSpan={6}>
                            <JsonBlock row={row} />
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="audit-footer">
            <span className="muted">
              审计只读：本页没有任何写操作，浏览器与 App 形态都只能查询。
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
    </section>
  );
}
