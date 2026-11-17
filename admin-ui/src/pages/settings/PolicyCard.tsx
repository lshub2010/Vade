import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ReactElement } from 'react';
import { ApiError, api, describeError } from '../../app/api';
import { ConfirmDialog } from '../../app/ConfirmDialog';
import {
  GROUP_TITLE,
  dangerOf,
  estimateNudge,
  formatCompact,
  formatConfigValue,
  formatNumber,
  groupOf,
  isEditable,
  kindOf,
  metaOf,
  sourceLabel,
  validateNumber,
} from '../../app/configMeta';
import type { ConfigGroup, DangerKind } from '../../app/configMeta';
import { formatFingerprint } from '../../app/format';
import type {
  AuditListResponse,
  AuditRow,
  ConfigItem,
  ConfigPutResponse,
  ConfigUsage,
} from '../../app/types';

const ESTIMATE_DAYS = 7;
const AUDIT_PAGE_CAP = 20;

export interface PolicyCardProps {
  items: ConfigItem[];
  readOnly: boolean;
  usage: ConfigUsage | null;
  usageError: string;
  onSaved: () => Promise<void>;
  onUsageRefresh: () => void;
  fp8: string | null;
}

/** 记忆与进化策略：热生效的键才给控件，需重启的项照实标注。 */
export function PolicyCard({
  items,
  readOnly,
  usage,
  usageError,
  onSaved,
  onUsageRefresh,
  fp8,
}: PolicyCardProps): ReactElement {
  const [auditRows, setAuditRows] = useState<AuditRow[] | null>(null);
  const [auditTruncated, setAuditTruncated] = useState(false);
  const [auditError, setAuditError] = useState('');

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const result = await loadLlmCalls(7);
        if (cancelled) return;
        setAuditRows(result.rows);
        setAuditTruncated(result.truncated);
        setAuditError('');
      } catch (cause) {
        if (cancelled) return;
        setAuditRows(null);
        setAuditError(describeError(cause));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const editable = items.filter((item) => isEditable(item) && item.mode === 'hot');
  const groups: ConfigGroup[] = ['evolve', 'memory', 'other'];
  const missing = MISSING_DESIGN_KEYS.filter((key) => !items.some((item) => item.key === key));
  const hotCount = items.filter((item) => item.mode === 'hot').length;

  return (
    <section className="card" aria-labelledby="settings-policy">
      <header className="card-head">
        <div>
          <h2 id="settings-policy">记忆与进化策略</h2>
          <p className="card-sub">
            这些键热生效：保存后下一轮对话或下一次触发器就用新值。每次写入都记审计{' '}
            <code>config.changed</code>，落 <code>data/config.overrides.json</code>，不回写{' '}
            <code>core.toml</code>。
          </p>
        </div>
      </header>

      {readOnly ? (
        <p className="notice notice-readonly">
          浏览器只读会话：下面是当前生效值，不渲染输入框。要改策略请用 Vade App 打开管理面。
        </p>
      ) : null}

      {editable.length === 0 ? (
        <p className="muted">core 没有下发可改的策略项。</p>
      ) : null}

      {groups.map((group) => {
        const rows = editable.filter((item) => groupOf(item.key) === group);
        if (rows.length === 0) return null;
        return (
          <div className="config-group" key={group}>
            <h3 className="config-group-title">{GROUP_TITLE[group]}</h3>
            <div className="config-rows">
              {rows.map((item) => (
                <ConfigRow
                  key={item.key}
                  item={item}
                  readOnly={readOnly}
                  usage={usage}
                  usageError={usageError}
                  onUsageRefresh={onUsageRefresh}
                  auditRows={auditRows}
                  auditTruncated={auditTruncated}
                  auditError={auditError}
                  onSaved={onSaved}
                />
              ))}
            </div>
          </div>
        );
      })}

      {auditError && editable.some((item) => item.key === 'evolve.nudge_interval') ? (
        <p className="hint">
          成本估算依赖审计里的 <code>llm.call</code> 行，当前读取失败（{auditError}），先不显示估算。
        </p>
      ) : null}

      {missing.length > 0 ? (
        <p className="hint">
          本版本 core 下发 {hotCount} 项热生效配置（其中 {editable.length} 项在本页可改）。设计文档里的
          「记忆与进化策略 10 项」中，其余键（
          {missing.slice(0, 3).map((key) => (
            <code key={key}>{key}</code>
          ))}{' '}
          …）core 尚未暴露 —— 页面不画空控件，core 提供后会自动出现。需重启的项在下方只读区。
        </p>
      ) : null}

      {fp8 ? (
        <p className="hint">
          设备指纹核对：core <code>{formatFingerprint(fp8)}</code>。
        </p>
      ) : null}
    </section>
  );
}

interface ConfigRowProps {
  item: ConfigItem;
  readOnly: boolean;
  usage: ConfigUsage | null;
  usageError: string;
  onUsageRefresh: () => void;
  auditRows: AuditRow[] | null;
  auditTruncated: boolean;
  auditError: string;
  onSaved: () => Promise<void>;
}

function ConfigRow({
  item,
  readOnly,
  usage,
  usageError,
  onUsageRefresh,
  auditRows,
  auditTruncated,
  auditError,
  onSaved,
}: ConfigRowProps): ReactElement {
  const meta = metaOf(item.key);
  const kind = kindOf(item);
  const [draft, setDraft] = useState<string | boolean>(() => draftOf(item));
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState('');
  const [phase, setPhase] = useState<'idle' | 'saving' | 'applied' | 'restart'>('idle');
  const [danger, setDanger] = useState<DangerKind>(null);
  const [pendingValue, setPendingValue] = useState<number | boolean | null>(null);

  useEffect(() => {
    setDraft(draftOf(item));
    setDirty(false);
  }, [item.key, item.value]);

  const save = useCallback(
    async (value: unknown, confirmed: boolean): Promise<void> => {
      setPhase('saving');
      setError('');
      try {
        const body: Record<string, unknown> = { key: item.key, value };
        if (confirmed) body.confirm = true;
        const result = await api<ConfigPutResponse>('/admin/api/config', { method: 'PUT', body });
        if (result && typeof result.error === 'string' && result.error.length > 0) {
          setError(result.error);
          setPhase('idle');
          return;
        }
        setPhase(result?.needs_restart ? 'restart' : 'applied');
        setDirty(false);
        await onSaved();
      } catch (cause) {
        setPhase('idle');
        setError(putError(cause));
      }
    },
    [item.key, onSaved],
  );

  const requestSave = (): void => {
    if (readOnly) return;
    setError('');
    if (kind === 'bool') {
      const value = draft === true;
      const rule = dangerOf(item.key, value);
      if (rule) {
        setPendingValue(value);
        setDanger(rule);
        return;
      }
      void save(value, false);
      return;
    }
    if (kind === 'number') {
      const parsed = validateNumber(item, String(draft));
      if (parsed.error) {
        setError(parsed.error);
        return;
      }
      const value = parsed.value ?? 0;
      const rule = dangerOf(item.key, value);
      if (rule) {
        setPendingValue(value);
        setDanger(rule);
        return;
      }
      void save(value, false);
      return;
    }
    void save(String(draft), false);
  };

  const estimate = useMemo(() => {
    if (item.key !== 'evolve.nudge_interval' || auditRows === null) return null;
    const interval =
      typeof draft === 'string' ? Number.parseInt(draft, 10) : Number.parseInt(String(item.value), 10);
    if (!Number.isFinite(interval) || interval <= 0) return null;
    return { ...estimateNudge(auditRows, interval, ESTIMATE_DAYS), truncated: auditTruncated };
  }, [item.key, draft, item.value, auditRows, auditTruncated]);

  const usageRatio =
    usage && usage.tokens_budget > 0 ? Math.min(1, usage.tokens_used / usage.tokens_budget) : 0;

  return (
    <div className="config-row">
      <div className="config-main">
        <div className="config-label">
          <span className="config-name">{meta.label}</span>
          <code className="config-key">{item.key}</code>
          {item.mode === 'restart' ? <span className="badge badge-muted">需重启</span> : null}
        </div>
        {meta.hint ? <p className="hint">{meta.hint}</p> : null}

        {readOnly ? (
          <p className="config-value-line">{formatConfigValue(item)}</p>
        ) : kind === 'bool' ? (
          <div className="switch-row">
            <button
              type="button"
              role="switch"
              aria-checked={draft === true}
              aria-label={meta.label}
              className="switch"
              onClick={() => {
                setDraft(draft !== true);
                setDirty(true);
                setPhase('idle');
              }}
            />
            <span className="muted">{draft === true ? '开' : '关'}</span>
          </div>
        ) : kind === 'enum' ? (
          <select
            className="input input-inline"
            value={String(draft)}
            onChange={(event) => {
              setDraft(event.target.value);
              setDirty(true);
              setPhase('idle');
            }}
          >
            {(item.values ?? []).map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        ) : (
          <div className="number-row">
            <input
              className="input input-number"
              type="number"
              inputMode="numeric"
              step={1}
              min={item.min}
              max={item.max}
              value={String(draft)}
              aria-invalid={error.length > 0}
              onChange={(event) => {
                setDraft(event.target.value);
                setDirty(true);
                setPhase('idle');
              }}
            />
            {meta.unit ? <span className="muted">{meta.unit}</span> : null}
            {item.min !== undefined && item.max !== undefined ? (
              <span className="hint">
                范围 {item.min}–{item.max}
              </span>
            ) : null}
          </div>
        )}

        {estimate ? (
          <p className="hint estimate-line">
            {auditError
              ? '审计不可用，暂时无法估算复盘成本。'
              : estimate.turnsPerDay < 0.05
                ? '近 7 天没有对话记录，无法估算复盘成本。'
                : `按近 7 天均值（约 ${estimate.turnsPerDay.toFixed(1)} 轮/天）：≈ ${estimate.reviewsPerDay.toFixed(1)} 次复盘/天、≈ ${
                    estimate.tokensPerDay === null
                      ? '无 token 样本'
                      : `${formatCompact(estimate.tokensPerDay)} token/天`
                  }`}
            {estimate.truncated ? '（样本已截断，实际更高）' : ''}
          </p>
        ) : null}

        {item.key === 'evolve.daily_token_budget' ? (
          <div className="usage-block">
            {usage ? (
              <>
                <div className="usage-head">
                  <span>
                    今日（{usage.day}）已用 <strong>{formatNumber(usage.tokens_used)}</strong>
                    {usage.tokens_budget > 0 ? (
                      <>
                        {' / '}
                        {formatNumber(usage.tokens_budget)} token
                      </>
                    ) : (
                      ' token（预算 0 = 不限）'
                    )}
                  </span>
                  <span className="muted">
                    {usage.tokens_budget > 0 ? `${(usageRatio * 100).toFixed(1)}%` : '不限'}
                  </span>
                </div>
                {usage.tokens_budget > 0 ? (
                  <div
                    className={usageRatio >= 1 ? 'progress progress-full' : 'progress'}
                    role="progressbar"
                    aria-valuemin={0}
                    aria-valuemax={usage.tokens_budget}
                    aria-valuenow={Math.min(usage.tokens_used, usage.tokens_budget)}
                    aria-label="今日 token 用量"
                  >
                    <span className="progress-fill" style={{ width: `${Math.max(usageRatio * 100, 1)}%` }} />
                  </div>
                ) : null}
              </>
            ) : (
              <p className="hint">
                用量读取失败{usageError ? `：${usageError}` : ''}。
                <button type="button" className="link-button" onClick={onUsageRefresh}>
                  重试
                </button>
              </p>
            )}
          </div>
        ) : null}

        {error ? (
          <p className="form-error" role="alert">
            {error}
          </p>
        ) : null}
      </div>

      <div className="config-side">
        <dl className="triple">
          <div>
            <dt>当前值</dt>
            <dd>{formatConfigValue(item)}</dd>
          </div>
          <div>
            <dt>默认值</dt>
            <dd>{formatDefault(item)}</dd>
          </div>
          <div>
            <dt>来源</dt>
            <dd>{sourceLabel(item.source)}</dd>
          </div>
        </dl>
        <div className="config-side-actions">
          {!readOnly && dirty ? (
            <button
              type="button"
              className="Button Button-sm Button-primary"
              onClick={requestSave}
              disabled={phase === 'saving'}
            >
              {phase === 'saving' ? '保存中…' : '保存'}
            </button>
          ) : null}
          {phase === 'applied' ? <span className="badge badge-ok">已生效</span> : null}
          {phase === 'restart' ? <span className="badge badge-warn">已保存，重启后生效</span> : null}
        </div>
      </div>

      {danger ? (
        <ConfirmDialog
          title={dangerTitle(danger)}
          confirmLabel={dangerConfirmLabel(danger, pendingValue)}
          danger
          busy={phase === 'saving'}
          onConfirm={() => {
            const value = pendingValue;
            setDanger(null);
            if (value !== null) void save(value, true);
          }}
          onCancel={() => setDanger(null)}
        >
          <p>{dangerBody(danger, pendingValue, usage)}</p>
        </ConfirmDialog>
      ) : null}
    </div>
  );
}

function draftOf(item: ConfigItem): string | boolean {
  if (typeof item.value === 'boolean') return item.value;
  if (item.value === null || item.value === undefined) return '';
  if (typeof item.value === 'object') return JSON.stringify(item.value);
  return String(item.value);
}

function formatDefault(item: ConfigItem): string {
  const value = item.default;
  const meta = metaOf(item.key);
  if (typeof value === 'boolean') return value ? '开' : '关';
  if (typeof value === 'number') {
    return meta.unit ? `${formatNumber(value)} ${meta.unit}` : formatNumber(value);
  }
  if (value === null || value === undefined) return '—';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function putError(cause: unknown): string {
  if (cause instanceof ApiError) {
    if (cause.code === 'CONFIRM_REQUIRED') return '这个改动需要二次确认，请重新点保存';
    if (cause.status === 400) return `保存被拒绝：${cause.message}`;
    if (cause.code === 'UNKNOWN_CONFIG_KEY') return 'core 不认识这个键（可能版本不一致）';
    return describeError(cause);
  }
  return describeError(cause);
}

function dangerTitle(kind: DangerKind): string {
  switch (kind) {
    case 'evolve-off':
      return '关闭自进化？';
    case 'budget-zero':
      return '把日预算设为不限？';
    case 'nudge-low':
      return '把复盘间隔调到 5 轮以下？';
    default:
      return '确认这次修改？';
  }
}

function dangerConfirmLabel(kind: DangerKind, value: number | boolean | null): string {
  switch (kind) {
    case 'evolve-off':
      return '关闭自进化';
    case 'budget-zero':
      return '设为不限';
    case 'nudge-low':
      return `按 ${String(value)} 轮保存`;
    default:
      return '确认';
  }
}

function dangerBody(
  kind: DangerKind,
  value: number | boolean | null,
  usage: ConfigUsage | null,
): string {
  switch (kind) {
    case 'evolve-off':
      return '关闭后不再产生新记忆与新技能提案；已有记忆与技能保留；已排队的复盘不再执行。要恢复需重新打开这个开关。';
    case 'budget-zero':
      return `0 = 不限量：当天不再有 token 总闸，复盘与技能进化会一直花钱到次日 00:00（UTC）重置。今日已用 ${
        usage ? formatNumber(usage.tokens_used) : '未知'
      } token。`;
    case 'nudge-low':
      return `复盘间隔 ${String(value)} 轮低于安全下界 5：复盘频率会拉高，费用近似线性上升。当前值每 ${String(
        value,
      )} 轮触发一次。`;
    default:
      return '这项改动会影响成本或行为，确认后立即写入 overrides.json。';
  }
}

/** 设计文档 §7.9 的 10 项策略键（用来提示 core 还缺哪几项，不画空控件）。 */
const MISSING_DESIGN_KEYS = [
  'memory.session_inject_char_limit',
  'memory.recall_k_max',
  'memory.recall_token_limit',
  'evolve.flush_min_turns',
  'evolve.session_char_limit',
  'evolve.daily_budget_skills',
  'evolve.session_budget_skills',
  'compaction.threshold',
  'compaction.target_ratio',
  'compaction.protect_last_n',
  'skills.l0_token_budget',
];

async function loadLlmCalls(days: number): Promise<{ rows: AuditRow[]; truncated: boolean }> {
  const from = Math.floor(Date.now() / 1000) - days * 86_400;
  const rows: AuditRow[] = [];
  let cursor: string | null = null;
  let pages = 0;
  let truncated = false;
  do {
    const params = new URLSearchParams({ kind: 'llm.call', from: String(from), limit: '500' });
    if (cursor) params.set('cursor', cursor);
    const page = await api<AuditListResponse>(`/admin/api/audit?${params.toString()}`);
    rows.push(...page.items);
    cursor = page.next_cursor;
    pages += 1;
    if (pages >= AUDIT_PAGE_CAP && cursor) {
      truncated = true;
      break;
    }
  } while (cursor);
  return { rows, truncated };
}
