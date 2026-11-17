import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactElement, ReactNode } from 'react';
import { ApiError, api } from '../app/api';
import { ConfirmDialog } from '../app/ConfirmDialog';
import { parseUnifiedDiff } from '../app/diff';
import type { DiffCell, DiffRowKind } from '../app/diff';
import { absoluteTime, relativeTime } from '../app/format';
import { describeSkillError } from '../app/skillError';
import { originLabel, statusMeta } from '../app/skillMeta';
import type { OkResponse, SkillDetail, SkillHistoryEntry, SkillSummary } from '../app/types';

type DialogKind = 'approve' | 'reject' | 'rollback' | null;

export interface SkillDrawerProps {
  name: string;
  readOnly: boolean;
  onClose: () => void;
  /** 写操作成功后通知列表刷新；空字符串表示「只刷新，不提示」。 */
  onChanged: (message: string) => void;
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

function DiffCellView({
  cell,
  side,
  kind,
}: {
  cell: DiffCell | null;
  side: 'left' | 'right';
  kind: DiffRowKind;
}): ReactElement {
  if (cell === null) {
    return <div className={`diff-cell diff-cell-${side} diff-empty`} aria-hidden="true" />;
  }
  // 左侧只在删除/改动时标红，右侧只在新增/改动时标绿；相等行两边都不上色。
  const tone = kind === 'equal' ? 'equal' : side === 'left' ? 'del' : 'add';
  return (
    <div className={`diff-cell diff-cell-${side} diff-${tone}`}>
      <span className="diff-no">{cell.line ?? ''}</span>
      <span className="diff-text">{cell.text.length > 0 ? cell.text : '\u00a0'}</span>
    </div>
  );
}

/** 后端算好的 unified diff → 左旧右新两栏；配对不上的一侧留空。 */
function DiffView({ text }: { text: string }): ReactElement {
  const parsed = useMemo(() => parseUnifiedDiff(text), [text]);

  if (parsed.hunks.length === 0) {
    return <p className="muted">后端返回的 diff 里没有可显示的行（可能只剩文件头）。</p>;
  }

  return (
    <div className="diff" aria-label="提案 diff：左旧右新，按行高亮">
      <div className="diff-head" aria-hidden="true">
        <div className="diff-head-cell">
          旧 · <code>{parsed.from.length > 0 ? parsed.from : '当前版本'}</code>
        </div>
        <div className="diff-head-cell">
          新 · <code>{parsed.to.length > 0 ? parsed.to : '待审提案'}</code>
        </div>
      </div>
      <div className="diff-body">
        {parsed.hunks.map((hunk, hunkIndex) => (
          <div className="diff-hunk" key={`${hunk.header}:${hunkIndex}`}>
            <div className="diff-hunk-head">{hunk.header}</div>
            {hunk.rows.map((row, rowIndex) => (
              <div className={`diff-row diff-row-${row.kind}`} key={`${rowIndex}`}>
                <DiffCellView cell={row.left} side="left" kind={row.kind} />
                <DiffCellView cell={row.right} side="right" kind={row.kind} />
              </div>
            ))}
          </div>
        ))}
      </div>
      {parsed.truncated ? (
        <p className="hint">diff 在 20k 字符处被后端截断；未显示的部分以「正文」一节为准。</p>
      ) : null}
    </div>
  );
}

function MetaRow({ label, children }: { label: string; children: ReactNode }): ReactElement {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

/** 驳回弹窗：理由必填，空着不让提交（后端也会拒，前端只是别让人白跑一趟）。 */
function RejectDialog({
  name,
  revision,
  busy,
  onCancel,
  onConfirm,
}: {
  name: string;
  revision: number;
  busy: boolean;
  onCancel: () => void;
  onConfirm: (reason: string) => void;
}): ReactElement {
  const [reason, setReason] = useState('');
  const [touched, setTouched] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const trimmed = reason.trim();
  const invalid = touched && trimmed.length === 0;

  useEffect(() => {
    inputRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') onCancel();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onCancel]);

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onCancel();
      }}
    >
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="reject-title">
        <h2 id="reject-title" className="modal-title">
          驳回 {name} 的提案？
        </h2>
        <div className="modal-body">
          <p>
            提案 rev {revision} 会被移到 <code>.rejected/</code>，7 天后清理；当前生效的版本保持不变。
            驳回是终态，之后要生效只能重新提交提案。
          </p>
          <label className="field-label" htmlFor="reject-reason">
            驳回理由（必填，会写进审计）
          </label>
          <textarea
            id="reject-reason"
            ref={inputRef}
            className="input textarea"
            rows={3}
            value={reason}
            aria-invalid={invalid}
            aria-describedby={invalid ? 'reject-reason-error' : undefined}
            onChange={(event) => {
              setReason(event.target.value);
              setTouched(true);
            }}
            placeholder="例如：requires_tools 里的 fs.write 在当前部署中不存在"
          />
          {invalid ? (
            <p className="form-error" id="reject-reason-error" role="alert">
              理由不能为空。写清为什么不该通过，别人以后才看得懂。
            </p>
          ) : null}
        </div>
        <div className="modal-actions">
          <button type="button" className="Button Button-ghost" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button
            type="button"
            className="Button Button-danger"
            disabled={busy || trimmed.length === 0}
            onClick={() => {
              setTouched(true);
              if (trimmed.length === 0) return;
              onConfirm(trimmed);
            }}
          >
            {busy ? '提交中…' : '驳回'}
          </button>
        </div>
      </div>
    </div>
  );
}

export function SkillDrawer({ name, readOnly, onClose, onChanged }: SkillDrawerProps): ReactElement {
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [actionError, setActionError] = useState('');
  const [busy, setBusy] = useState(false);
  const [dialog, setDialog] = useState<DialogKind>(null);
  const [rollbackTarget, setRollbackTarget] = useState('');
  const [showBody, setShowBody] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  const loadDetail = useCallback(
    async (signal?: AbortSignal, silent = false): Promise<void> => {
      if (!silent) setLoading(true);
      try {
        const data = await api<SkillDetail>(`/admin/api/skills/${encodeURIComponent(name)}`, {
          signal,
        });
        setDetail(data);
        setLoadError('');
      } catch (cause) {
        if (signal?.aborted) return;
        setLoadError(describeSkillError(cause));
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [name],
  );

  useEffect(() => {
    const controller = new AbortController();
    setDetail(null);
    setLoadError('');
    setActionError('');
    setDialog(null);
    setShowBody(false);
    setRollbackTarget('');
    void loadDetail(controller.signal);
    return () => controller.abort();
  }, [loadDetail]);

  // 抽屉是模态的：锁住背后页面的滚动，打开时把焦点移进来。
  useEffect(() => {
    panelRef.current?.focus();
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previous;
    };
  }, []);

  // Esc 关抽屉；有确认框时让确认框自己处理，避免一次按键关两层。
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape' && dialog === null) onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [dialog, onClose]);

  // 回滚目标默认选最近的一个旧版本；列表变化后保持用户已选的那一项。
  useEffect(() => {
    if (!detail) return;
    const targets = detail.history.filter((entry) => entry.revision !== detail.revision);
    if (targets.length === 0) return;
    const values = targets.map((entry) => String(entry.revision));
    setRollbackTarget((previous) =>
      previous.length > 0 && values.includes(previous) ? previous : values[values.length - 1]!,
    );
  }, [detail]);

  const mutate = useCallback(
    async (run: () => Promise<string>): Promise<void> => {
      setBusy(true);
      setActionError('');
      try {
        const message = await run();
        setDialog(null);
        onChanged(message);
        await loadDetail(undefined, true);
      } catch (cause) {
        setDialog(null);
        setActionError(describeSkillError(cause));
        // 409/404 说明看到的与服务端不一致：立刻重取，别让人对着旧 diff 再点一次。
        if (cause instanceof ApiError && (cause.status === 409 || cause.status === 404)) {
          await loadDetail(undefined, true);
          onChanged('');
        }
      } finally {
        setBusy(false);
      }
    },
    [loadDetail, onChanged],
  );

  const pendingRevision = detail?.pending_revision ?? null;
  const hasPending = pendingRevision !== null;
  const stale = detail?.status === 'stale';
  const pendingOnly = detail?.status === 'pending';
  const missingTools = detail?.requires_tools ?? [];
  const rollbackTargets: SkillHistoryEntry[] = useMemo(
    () => (detail ? detail.history.filter((entry) => entry.revision !== detail.revision) : []),
    [detail],
  );
  const bodyLong = (detail?.body.length ?? 0) > 4000;
  const bodyText = detail && bodyLong && !showBody ? `${detail.body.slice(0, 4000)}\n…` : detail?.body ?? '';

  const approve = (): void => {
    if (pendingRevision === null) return;
    void mutate(async () => {
      const result = await api<SkillSummary>(
        `/admin/api/skills/${encodeURIComponent(name)}/approve`,
        { method: 'POST', body: { revision: pendingRevision } },
      );
      return `已通过 ${name}：rev ${result.revision} 已生效，下一轮对话起可用`;
    });
  };

  const reject = (reason: string): void => {
    if (pendingRevision === null) return;
    void mutate(async () => {
      await api<OkResponse>(`/admin/api/skills/${encodeURIComponent(name)}/reject`, {
        method: 'POST',
        body: { revision: pendingRevision, reason },
      });
      return `已驳回 ${name} 的提案 rev ${pendingRevision}：移到 .rejected/，7 天后清理`;
    });
  };

  const rollback = (): void => {
    const target = Number(rollbackTarget);
    if (!Number.isInteger(target)) return;
    void mutate(async () => {
      const result = await api<SkillSummary>(
        `/admin/api/skills/${encodeURIComponent(name)}/rollback`,
        { method: 'POST', body: { to_revision: target } },
      );
      return `已回滚 ${name} 到 rev ${target}：当前 rev ${result.revision}，历史未删除，下一轮对话起生效`;
    });
  };

  const status = statusMeta(detail?.status);

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
        aria-labelledby="skill-drawer-title"
        tabIndex={-1}
        ref={panelRef}
      >
        <header className="drawer-head">
          <div className="drawer-title">
            <h2 id="skill-drawer-title">{name}</h2>
            <div className="drawer-badges">
              {detail ? (
                <span className={status.className} title={status.hint}>
                  {status.label}
                </span>
              ) : null}
              {detail ? <span className="badge badge-muted">rev {detail.revision}</span> : null}
              {hasPending ? <span className="badge badge-info">提案 rev {pendingRevision}</span> : null}
            </div>
          </div>
          <button type="button" className="Button Button-ghost Button-sm" onClick={onClose}>
            关闭
          </button>
        </header>

        <div className="drawer-body">
          {loading && !detail ? <p className="muted">正在读取 {name}…</p> : null}

          {loadError ? (
            <div className="alert" role="alert">
              <span>读取详情失败：{loadError}</span>
              <button type="button" className="Button Button-sm" onClick={() => void loadDetail()}>
                重试
              </button>
            </div>
          ) : null}

          {detail ? (
            <>
              {readOnly ? (
                <div className="notice notice-readonly" role="status">
                  浏览器会话是只读的：这里只能查看 diff 与正文。通过 / 驳回 / 回滚要在 Vade App 里做。
                </div>
              ) : null}

              {stale ? (
                <div className="alert alert-warn" role="alert">
                  <div>
                    <strong>依赖的工具已不存在</strong>
                    <p>
                      core 连续判定该 skill 声明的工具不可用，已置为 <code>stale</code>。
                      {missingTools.length > 0 ? (
                        <>
                          {' '}
                          声明的工具：{missingTools.map((tool) => <code key={tool}>{tool}</code>)}
                        </>
                      ) : null}
                    </p>
                    <p>
                      stale 状态下<strong>不能通过</strong>新提案（新版本很可能依赖同一批工具）；
                      只能驳回提案，或回滚到依赖还可用的旧版本。
                    </p>
                  </div>
                </div>
              ) : null}

              {!stale && detail.available === false ? (
                <div className="notice" role="status">
                  core 标记为不可用
                  {detail.unavailable_reason ? (
                    <>
                      ：<code>{detail.unavailable_reason}</code>
                    </>
                  ) : null}
                  。通过新提案前先确认它不是同一个原因。
                </div>
              ) : null}

              {actionError ? (
                <div className="alert" role="alert">
                  <span>操作失败：{actionError}</span>
                  <button
                    type="button"
                    className="Button Button-sm Button-ghost"
                    onClick={() => setActionError('')}
                  >
                    知道了
                  </button>
                </div>
              ) : null}

              <section className="skill-section" aria-labelledby="skill-meta-title">
                <h3 id="skill-meta-title">元数据</h3>
                <dl className="skill-meta skill-meta-detail">
                  <MetaRow label="当前 rev">
                    <code>{detail.revision}</code>
                  </MetaRow>
                  <MetaRow label="状态">
                    <span className={status.className}>{status.label}</span>
                  </MetaRow>
                  <MetaRow label="来源">{originLabel(detail.origin)}</MetaRow>
                  <MetaRow label="最近使用">
                    {detail.last_used_at ? (
                      <span title={absoluteTime(detail.last_used_at)}>
                        {relativeTime(detail.last_used_at)}
                      </span>
                    ) : (
                      '从未使用'
                    )}
                  </MetaRow>
                  <MetaRow label="失败计数">{detail.fail_count ?? 0}</MetaRow>
                  <MetaRow label="派生自">
                    {detail.derived_from ? <code>{detail.derived_from}</code> : '—'}
                  </MetaRow>
                  <MetaRow label="依赖工具">
                    {missingTools.length > 0 ? (
                      <span className="chip-row">
                        {missingTools.map((tool) => (
                          <code key={tool}>{tool}</code>
                        ))}
                      </span>
                    ) : (
                      '无声明'
                    )}
                  </MetaRow>
                  <MetaRow label="触发词">
                    {(detail.triggers ?? []).length > 0 ? (
                      <span className="chip-row">
                        {(detail.triggers ?? []).map((trigger) => (
                          <code key={trigger}>{trigger}</code>
                        ))}
                      </span>
                    ) : (
                      '—'
                    )}
                  </MetaRow>
                </dl>
                {detail.description ? <p className="skill-desc">{detail.description}</p> : null}
                {detail.when_to_use ? (
                  <p className="muted skill-when">何时使用：{detail.when_to_use}</p>
                ) : null}
              </section>

              <section className="skill-section" aria-labelledby="skill-pending-title">
                <div className="skill-section-head">
                  <h3 id="skill-pending-title">待审提案</h3>
                  {hasPending ? (
                    <span className="badge badge-info">rev {pendingRevision}</span>
                  ) : null}
                </div>

                {!hasPending ? (
                  <p className="muted">
                    当前没有待审提案。自动提案与安装都会先落到 <code>pending/</code>，在这里审。
                  </p>
                ) : (
                  <>
                    <p className="hint">
                      左侧是当前生效版本，右侧是提案；diff 由后端算，页面不解析 YAML。
                    </p>
                    {detail.pending_diff ? (
                      <DiffView text={detail.pending_diff} />
                    ) : (
                      <div className="notice">
                        提案与当前版本没有正文差异（可能只改了元数据或资源文件）。
                      </div>
                    )}

                    {readOnly ? null : (
                      <div className="skill-actions">
                        {stale ? null : (
                          <button
                            type="button"
                            className="Button Button-primary"
                            disabled={busy}
                            onClick={() => setDialog('approve')}
                          >
                            {pendingOnly ? '启用' : '通过'}
                          </button>
                        )}
                        <button
                          type="button"
                          className="Button Button-danger-ghost"
                          disabled={busy}
                          onClick={() => setDialog('reject')}
                        >
                          驳回…
                        </button>
                        {stale ? (
                          <span className="hint">
                            stale 状态下不提供「通过」；请驳回，或回滚到旧版本。
                          </span>
                        ) : null}
                      </div>
                    )}
                  </>
                )}
              </section>

              <section className="skill-section" aria-labelledby="skill-history-title">
                <div className="skill-section-head">
                  <h3 id="skill-history-title">历史</h3>
                  <span className="muted">{detail.history.length} 个 revision</span>
                </div>

                {detail.history.length === 0 ? (
                  <p className="muted">还没有历史版本。通过提案或回滚时才会写入 .history/。</p>
                ) : (
                  <ul className="history-list">
                    {detail.history.map((entry) => (
                      <li key={entry.revision}>
                        <code>rev {entry.revision}</code>
                        <span className="muted">{formatBytes(entry.bytes)}</span>
                        {entry.revision === detail.revision ? (
                          <span className="badge badge-ok">当前</span>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                )}

                {!readOnly && rollbackTargets.length > 0 ? (
                  <div className="rollback-row">
                    <label className="field-label" htmlFor="rollback-target">
                      回滚目标
                    </label>
                    <select
                      id="rollback-target"
                      className="input"
                      value={rollbackTarget}
                      disabled={busy}
                      onChange={(event) => setRollbackTarget(event.target.value)}
                    >
                      {rollbackTargets.map((entry) => (
                        <option key={entry.revision} value={entry.revision}>
                          rev {entry.revision} · {formatBytes(entry.bytes)}
                        </option>
                      ))}
                    </select>
                    <button
                      type="button"
                      className="Button Button-sm"
                      disabled={busy || rollbackTarget.length === 0}
                      onClick={() => setDialog('rollback')}
                    >
                      回滚…
                    </button>
                  </div>
                ) : null}
              </section>

              <section className="skill-section" aria-labelledby="skill-body-title">
                <div className="skill-section-head">
                  <h3 id="skill-body-title">正文</h3>
                  <span className="muted">{detail.body.length} 字符</span>
                </div>
                <pre className="skill-body">{bodyText.length > 0 ? bodyText : '（空）'}</pre>
                {bodyLong ? (
                  <button
                    type="button"
                    className="link-button"
                    onClick={() => setShowBody((previous) => !previous)}
                  >
                    {showBody ? '收起正文' : '展开全部正文'}
                  </button>
                ) : null}
              </section>
            </>
          ) : null}
        </div>

        {detail && dialog === 'approve' ? (
          <ConfirmDialog
            title={`${pendingOnly ? '启用' : '通过'} ${name} 的提案？`}
            confirmLabel={pendingOnly ? '启用' : '通过'}
            busy={busy}
            onCancel={() => setDialog(null)}
            onConfirm={approve}
          >
            <p>
              {pendingOnly ? (
                <>
                  这个 skill 目前只在 <code>pending/</code> 里，<strong>尚未生效</strong>。
                </>
              ) : null}
              通过后提案 rev {pendingRevision} 转正为 rev {detail.revision + 1}，旧版本进{' '}
              <code>.history/</code>，可回滚。
            </p>
            <p>
              <strong>下一轮对话起生效，当前会话不受影响</strong>：正在进行的那一轮看到的仍是旧版本。
            </p>
          </ConfirmDialog>
        ) : null}

        {detail && dialog === 'reject' && pendingRevision !== null ? (
          <RejectDialog
            name={name}
            revision={pendingRevision}
            busy={busy}
            onCancel={() => setDialog(null)}
            onConfirm={reject}
          />
        ) : null}

        {detail && dialog === 'rollback' ? (
          <ConfirmDialog
            title={`回滚 ${name} 到 rev ${rollbackTarget}？`}
            confirmLabel="回滚"
            busy={busy}
            onCancel={() => setDialog(null)}
            onConfirm={rollback}
          >
            <p>
              会把 <code>.history/{name}/{rollbackTarget}.md</code> 复制成<strong>新的</strong>{' '}
              revision（rev {detail.revision + 1}），历史不删除、不覆盖。
            </p>
            <p>
              <strong>下一轮对话起生效，当前会话不受影响</strong>。回滚不会动{' '}
              <code>pending/</code> 里已有的提案。
            </p>
          </ConfirmDialog>
        ) : null}
      </aside>
    </div>
  );
}
