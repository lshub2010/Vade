/**
 * `/audit` 页的纯函数：查询串、时间范围、kind / status 展示、脱敏与摘要。
 *
 * 契约：`GET /admin/api/audit?from&to&module_id&kind&turn_id&limit&cursor`
 * （`schema/admin-api.yaml` · `AuditRow`）。**没有 `session_id` 参数** —— 页面上的
 * session_id 只在已加载的行里过滤，不假装服务端支持。
 *
 * 排序：reader 恒按 `ts DESC, id DESC` 返回；`turn_id` 回放时由页面反转成升序
 * （LLM 调用 → 工具 → 结果 → 下一轮）。
 */

import type { AuditRow } from './types';

/** 契约默认值，也是「加载更多」的步长。 */
export const PAGE_LIMIT = 100;
/** 轮次回放要整条链：reader 的 limit 上限与 schema maximum 都是 500。 */
export const TURN_LIMIT = 500;

export type RangePreset = '1h' | '24h' | '7d' | 'all';

export type AuditRange =
  | { kind: 'preset'; preset: RangePreset }
  | { kind: 'custom'; from: number | null; to: number | null };

export interface AuditFilters {
  range: AuditRange;
  moduleId: string;
  kind: string;
  turnId: string;
  /** 只在已加载的行里过滤；见文件头注释。 */
  sessionId: string;
}

export const PRESETS: ReadonlyArray<{ id: RangePreset; label: string; seconds: number | null }> = [
  { id: '1h', label: '近 1 小时', seconds: 3_600 },
  { id: '24h', label: '近 24 小时', seconds: 86_400 },
  { id: '7d', label: '近 7 天', seconds: 604_800 },
  { id: 'all', label: '全部', seconds: null },
];

/** 默认近 24 小时：审计库可能很大，首屏不该赌「全部」。 */
export function defaultFilters(): AuditFilters {
  return { range: { kind: 'preset', preset: '24h' }, moduleId: '', kind: '', turnId: '', sessionId: '' };
}

export function rangeBounds(range: AuditRange, nowMs: number = Date.now()): {
  from: number | null;
  to: number | null;
} {
  if (range.kind === 'custom') return { from: range.from, to: range.to };
  const preset = PRESETS.find((item) => item.id === range.preset);
  if (!preset || preset.seconds === null) return { from: null, to: null };
  return { from: Math.floor(nowMs / 1000) - preset.seconds, to: null };
}

export function rangeLabel(range: AuditRange): string {
  if (range.kind === 'custom') {
    const from = range.from === null ? '不限' : absoluteClock(range.from);
    const to = range.to === null ? '现在' : absoluteClock(range.to);
    return `${from} — ${to}`;
  }
  return PRESETS.find((item) => item.id === range.preset)?.label ?? '全部';
}

export function isFiltered(filters: AuditFilters): boolean {
  return (
    filters.range.kind === 'custom' ||
    filters.range.preset !== 'all' ||
    filters.moduleId.trim().length > 0 ||
    filters.kind.trim().length > 0 ||
    filters.turnId.trim().length > 0 ||
    filters.sessionId.trim().length > 0
  );
}

/** 拼 `/admin/api/audit` 的查询串。空字符串条件不发，避免 `kind=` 变成「等于空」。 */
export function buildAuditPath(
  filters: AuditFilters,
  cursor: string | null,
  limit: number,
  nowMs: number = Date.now(),
): string {
  const bounds = rangeBounds(filters.range, nowMs);
  const params = new URLSearchParams();
  if (bounds.from !== null) params.set('from', String(bounds.from));
  if (bounds.to !== null) params.set('to', String(bounds.to));
  const moduleId = filters.moduleId.trim();
  if (moduleId.length > 0) params.set('module_id', moduleId);
  const kind = filters.kind.trim();
  if (kind.length > 0) params.set('kind', kind);
  const turnId = filters.turnId.trim();
  if (turnId.length > 0) params.set('turn_id', turnId);
  params.set('limit', String(limit));
  if (cursor) params.set('cursor', cursor);
  return `/admin/api/audit?${params.toString()}`;
}

// ------------------------------------------------------------------ 展示元数据

/** 常见的 kind；输入框的 datalist 用，不限制自由输入。 */
export const KNOWN_KINDS: readonly string[] = [
  'admin.login',
  'admin.logout',
  'admin.logout_all',
  'admin.password_changed',
  'admin.password_rehashed',
  'admin.rejected',
  'admin_lockout',
  'setup.attempt',
  'setup.token_rotated',
  'device.added',
  'device.bound',
  'device.revoked',
  'device.ticket',
  'invite.created',
  'invite.revoked',
  'node.updated',
  'node.revoked',
  'node.disconnected',
  'config.changed',
  'audit.pruned',
  'approval',
  'approval_timeout',
  'approval_scope_granted',
  'tool_call',
  'llm.call',
  'module_page_call',
];

const KIND_LABELS: Record<string, string> = {
  'admin.login': '管理面登录',
  'admin.logout': '退出登录',
  'admin.logout_all': '全端退出',
  'admin.password_changed': '修改密码',
  'admin.password_rehashed': '密码重哈希',
  'admin.rejected': '管理面拒绝',
  'admin.reset': '管理面重置',
  admin_lockout: '登录锁定',
  'setup.attempt': '首次配置尝试',
  'setup.token_rotated': '配置令牌轮换',
  'device.added': '设备新增',
  'device.bound': '设备绑定',
  'device.revoked': '设备吊销',
  'device.ticket': '绑定票签发',
  'invite.created': '邀请创建',
  'invite.revoked': '邀请撤销',
  'node.updated': '节点更新',
  'node.revoked': '节点吊销',
  'node.disconnected': '节点断开',
  'config.changed': '配置变更',
  'audit.pruned': '审计清理',
  approval: '审批',
  approval_timeout: '审批超时',
  approval_scope_granted: '会话放行',
  tool_call: '工具调用',
  'llm.call': '模型调用',
  module_page_call: '模块页面调用',
};

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

const STATUS_LABELS: Record<string, string> = {
  ok: '成功',
  allowed: '放行',
  denied: '拒绝',
  failed: '失败',
  error: '错误',
  timeout: '超时',
  approval_required: '待审批',
  pending: '等待中',
  cancelled: '已取消',
};

export function statusLabel(status: string | null | undefined): string {
  if (!status) return '—';
  return STATUS_LABELS[status] ?? status;
}

export function statusClass(status: string | null | undefined): string {
  switch (status) {
    case 'ok':
    case 'allowed':
      return 'badge badge-ok';
    case 'denied':
    case 'failed':
    case 'error':
      return 'badge badge-danger';
    case 'timeout':
    case 'approval_required':
      return 'badge badge-warn';
    case 'pending':
    case 'cancelled':
      return 'badge badge-info';
    default:
      return 'badge badge-muted';
  }
}

export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms)) return '—';
  if (ms < 1) return '<1 ms';
  if (ms < 1_000) return `${Math.round(ms)} ms`;
  return `${(ms / 1_000).toFixed(ms < 10_000 ? 2 : 1)} s`;
}

/** 回放里相邻两步的间隔：ts 是秒，这里收秒、输出人话。 */
export function formatGap(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '—';
  if (seconds < 1) return `${Math.round(seconds * 1_000)} ms`;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 2 : 1)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}

function pad(value: number): string {
  return String(value).padStart(2, '0');
}

/** `HH:mm:ss.SSS`：回放时看清同秒内的先后。 */
export function clockTime(epochSeconds: number): string {
  const date = new Date(epochSeconds * 1_000);
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${String(
    date.getMilliseconds(),
  ).padStart(3, '0')}`;
}

/** 完整时间，供 `title` 与自定义范围回显。 */
export function absoluteClock(epochSeconds: number): string {
  const date = new Date(epochSeconds * 1_000);
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

/** epoch 秒 → `<input type="datetime-local" step=1>` 的本地时间串。 */
export function toDateTimeLocal(epochSeconds: number | null): string {
  if (epochSeconds === null || !Number.isFinite(epochSeconds)) return '';
  const date = new Date(epochSeconds * 1_000);
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

/** 反向：`datetime-local` 按本地时区解析；空/非法返回 null。 */
export function fromDateTimeLocal(value: string): number | null {
  if (value.trim().length === 0) return null;
  const ms = new Date(value).getTime();
  return Number.isFinite(ms) ? Math.floor(ms / 1_000) : null;
}

// ------------------------------------------------------------------ 脱敏

/**
 * 与 `core/vade/audit/redact.py` 的 `SENSITIVE_KEY` 同规则（第二道闸）。
 *
 * 服务端已经脱敏过一次；这里再脱一次是纵深防御：`extra` 是自由字段，
 * 将来 core 换实现也别让页面把凭据显示出来。`[redacted]` 会被原样保留 ——
 * 页面要显示「有字段被丢弃」，而不是悄悄少一行。
 */
const SENSITIVE_KEY = /(pass(word|wd|phrase)?|token|secret|credential|psk|private|api[_-]?key|access[_-]?key|auth|session[_-]?id|cookie)/i;

/** 与 core 一致：token 的计数是数字指标，不是凭据。 */
const SAFE_METRIC_KEYS = new Set([
  'tokens',
  'total_tokens',
  'token_count',
  'tokens_used',
  'token_usage',
  'prompt_tokens',
  'completion_tokens',
  'cached_tokens',
]);

const REDACTED = '[redacted]';
const MAX_STRING = 2_000;
const MAX_ITEMS = 64;
const MAX_DEPTH = 6;

function sanitize(value: unknown, depth: number = 0): unknown {
  if (depth > MAX_DEPTH) return '[truncated]';
  if (Array.isArray(value)) {
    const items = value.slice(0, MAX_ITEMS).map((item) => sanitize(item, depth + 1));
    if (value.length > MAX_ITEMS) items.push(`… ${value.length - MAX_ITEMS} more`);
    return items;
  }
  if (value !== null && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>);
    const out: Record<string, unknown> = {};
    for (const [index, [key, item]] of entries.entries()) {
      if (index >= MAX_ITEMS) {
        out['…'] = `${entries.length - MAX_ITEMS} more`;
        break;
      }
      out[key] =
        SAFE_METRIC_KEYS.has(key) || !SENSITIVE_KEY.test(key)
          ? sanitize(item, depth + 1)
          : REDACTED;
    }
    return out;
  }
  if (typeof value === 'string' && value.length > MAX_STRING) {
    return `${value.slice(0, MAX_STRING)}…(+${value.length - MAX_STRING})`;
  }
  return value;
}

/**
 * 展开的「原始 JSON」。
 *
 * 顶层列（`session_id` / `turn_id` / `actor` …）原样保留 —— 它们是审计列，
 * 不是参数；只对 `args_redacted` 与 `extra` 这两个自由字段做二次脱敏。
 */
export function auditJson(row: AuditRow): string {
  const safe: Record<string, unknown> = { ...row };
  if (row.args_redacted) safe.args_redacted = sanitize(row.args_redacted);
  if (row.extra) safe.extra = sanitize(row.extra);
  return JSON.stringify(safe, null, 2);
}

// ------------------------------------------------------------------ 摘要

export interface AuditSummary {
  /** 这一行「做了什么」：工具名 / 方法名 / 配置键 / kind 的中文名。 */
  title: string;
  /** 结果与耗时：状态 · code · 耗时。 */
  detail: string;
}

/**
 * 列表里的摘要列。
 *
 * 只取结构性字段（`tool` / `method` / `extra.name` / `extra.key`）与状态、code、
 * 耗时 —— **不拼自由文本**（`reason` 之类可能被塞进凭据）。要看细节展开 JSON。
 */
export function summarizeRow(row: AuditRow): AuditSummary {
  const extra = (row.extra ?? {}) as Record<string, unknown>;
  const text = (value: unknown): string | null =>
    typeof value === 'string' && value.trim().length > 0 ? value : null;

  const title =
    text(row.tool) ??
    text(row.method) ??
    text(extra.name) ??
    text(extra.key) ??
    text(extra.skill) ??
    kindLabel(row.kind);

  const parts: string[] = [];
  if (row.status) parts.push(statusLabel(row.status));
  if (row.code) parts.push(row.code);
  if (typeof row.duration_ms === 'number') parts.push(formatDuration(row.duration_ms));
  if (Array.isArray(extra.tables)) parts.push(`${extra.tables.length} 张天表`);

  return { title, detail: parts.join(' · ') };
}
