/**
 * `/settings` 的配置元信息与纯计算。
 *
 * 页面不硬编码「10 项」的键表：`GET /admin/api/config` 返回什么就渲染什么，
 * 这里只负责给已知键配人话标签、分组与危险判定。core 还没下发的键不会画空控件。
 */

import type { AuditRow, ConfigItem, ConfigSource } from './types';

export type ConfigGroup = 'evolve' | 'memory' | 'other';

export interface ConfigMeta {
  label: string;
  hint?: string;
  unit?: string;
  group: ConfigGroup;
}

const META: Record<string, ConfigMeta> = {
  'evolve.enabled': {
    label: '自进化总开关',
    hint: '关闭后不再产生新记忆与提案；已有记忆与技能保留，已排队的复盘不再执行。',
    group: 'evolve',
  },
  'evolve.nudge_interval': {
    label: '复盘间隔',
    hint: '每 N 轮对话触发一次旁路复盘。区间下界 5 会把复盘频率拉到最高，费用近似线性上升。',
    unit: '轮',
    group: 'evolve',
  },
  'evolve.daily_token_budget': {
    label: '日 token 预算',
    hint: '全局令牌桶，复盘 / 会话收尾 / 合并 / 技能进化共享。0 = 不限量（不推荐）。',
    unit: 'token',
    group: 'evolve',
  },
  'evolve.review_interval': {
    label: '会话复盘间隔',
    hint: '会话收尾复盘（session_close）的轮数间隔。',
    unit: '轮',
    group: 'evolve',
  },
  'evolve.flush_min_turns': {
    label: '短会话门槛',
    hint: '低于这个轮数的会话不调 LLM 做收尾，只走规则抽取。',
    unit: '轮',
    group: 'evolve',
  },
  'evolve.session_char_limit': {
    label: '会话写入上限',
    hint: '写入侧预算：超顶返回 MEMORY_FULL，由 evolver 当轮整理。',
    unit: '字符',
    group: 'evolve',
  },
  'evolve.daily_budget_skills': {
    label: '每日提案上限',
    hint: '超了记 evolve_budget_exceeded，不排队。',
    unit: '个/天',
    group: 'evolve',
  },
  'evolve.session_budget_skills': {
    label: '单会话提案上限',
    unit: '个',
    group: 'evolve',
  },
  'memory.session_inject_char_limit': {
    label: '会话注入预算',
    hint: '下一轮起按新预算裁剪注入内容；已存记忆不删，只是不注入。',
    unit: '字符',
    group: 'memory',
  },
  'memory.recall_k_max': {
    label: '召回条数上限',
    hint: '单次 recall 最多返回几条。',
    unit: '条',
    group: 'memory',
  },
  'memory.recall_token_limit': {
    label: '召回 token 上限',
    hint: '单次 recall 的返回预算。',
    unit: 'token',
    group: 'memory',
  },
  'memory.global_limit': {
    label: '全局记忆上限',
    hint: '容量配额。调小要先跑淘汰到水位并迁移，因此需重启后生效。',
    unit: '条',
    group: 'memory',
  },
  'memory.staging_ttl_days': {
    label: 'staging 保留天数',
    hint: '清理任务按天调度，热改会让已按旧 TTL 标记的行状态漂移，因此需重启。',
    unit: '天',
    group: 'memory',
  },
  'skills.l0_token_budget': {
    label: '技能目录预算',
    hint: '下调立即按激活优先、hit_count 降序裁剪目录。',
    unit: 'token',
    group: 'memory',
  },
  'compaction.threshold': {
    label: '压缩触发阈值',
    hint: '改它等于改费用曲线。',
    group: 'memory',
  },
  'compaction.target_ratio': {
    label: '压缩目标比例',
    group: 'memory',
  },
  'compaction.protect_last_n': {
    label: '压缩保护末 N 轮',
    unit: '轮',
    group: 'memory',
  },
  'policy.approval_timeout_s': {
    label: '审批超时',
    hint: '高危工具无人决策时的等待上限，超时按拒绝处理并写审计 approval_timeout。',
    unit: '秒',
    group: 'other',
  },
  'audit.retention_days': {
    label: '审计保留天数',
    hint: '清理任务按天调度，改完需重启。',
    unit: '天',
    group: 'other',
  },
  'core.log_level': {
    label: '日志级别',
    group: 'other',
  },
  'e2e.enabled': {
    label: '端到端加密',
    hint: '数据面 Noise 通道开关，改完需重启。',
    group: 'other',
  },
};

export function groupOf(key: string): ConfigGroup {
  if (key.startsWith('evolve.')) return 'evolve';
  if (key.startsWith('memory.') || key.startsWith('compaction.') || key.startsWith('skills.')) {
    return 'memory';
  }
  return 'other';
}

export function metaOf(key: string): ConfigMeta {
  return META[key] ?? { label: key, group: groupOf(key) };
}

export const GROUP_TITLE: Record<ConfigGroup, string> = {
  evolve: '进化',
  memory: '记忆',
  other: '其它热生效项',
};

/**
 * design §7.9 的「只读镜像」：这几项在只读区显示 `value + source`，不给编辑框。
 * 它们仍然会出现在 `GET /admin/api/config` 里，页面按这个集合把它们从可改区摘掉。
 */
export const READ_ONLY_MIRROR: ReadonlySet<string> = new Set([
  'policy.approval_timeout_s',
  'audit.retention_days',
  'e2e.enabled',
]);

export function isEditable(item: ConfigItem): boolean {
  return !READ_ONLY_MIRROR.has(item.key);
}

export type ConfigKind = 'bool' | 'enum' | 'number' | 'text';

export function kindOf(item: ConfigItem): ConfigKind {
  if (typeof item.value === 'boolean') return 'bool';
  if (Array.isArray(item.values) && item.values.length > 0) return 'enum';
  if (typeof item.value === 'number') return 'number';
  return 'text';
}

export type DangerKind = 'evolve-off' | 'budget-zero' | 'nudge-low' | null;

/** 三项必须二次确认：关总开关、预算归零、复盘间隔落到 5 以下。 */
export function dangerOf(key: string, value: unknown): DangerKind {
  if (key === 'evolve.enabled' && value === false) return 'evolve-off';
  if (key === 'evolve.daily_token_budget' && value === 0) return 'budget-zero';
  if (key === 'evolve.nudge_interval' && typeof value === 'number' && value < 5) return 'nudge-low';
  return null;
}

export function sourceLabel(source: ConfigSource | string | undefined): string {
  switch (source) {
    case 'default':
      return '默认值';
    case 'core.toml':
      return 'core.toml';
    case 'overrides':
      return '本页覆盖（overrides.json）';
    case 'env':
      return '环境变量';
    case 'cli':
      return '命令行';
    case undefined:
    case '':
      return '未提供';
    default:
      return String(source);
  }
}

export function formatNumber(value: number): string {
  return value.toLocaleString('zh-CN');
}

/** 紧凑数字：估算里的 token 量级不需要千分位。 */
export function formatCompact(value: number): string {
  if (!Number.isFinite(value)) return '—';
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return `${Math.round(value)}`;
}

export function formatConfigValue(item: ConfigItem): string {
  const value = item.value;
  const meta = metaOf(item.key);
  if (typeof value === 'boolean') return value ? '开' : '关';
  if (typeof value === 'number') {
    return meta.unit ? `${formatNumber(value)} ${meta.unit}` : formatNumber(value);
  }
  if (Array.isArray(value)) return value.length === 0 ? '（空）' : value.join(', ');
  if (value === null || value === undefined) return '—';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

/** 表单里的数字校验；与后端 `_coerce` 保持同一区间。 */
export function validateNumber(item: ConfigItem, raw: string): { value?: number; error?: string } {
  const text = raw.trim();
  if (text.length === 0) return { error: '请输入数字' };
  if (!/^-?\d+$/.test(text)) return { error: '只接受整数' };
  const value = Number.parseInt(text, 10);
  if (!Number.isFinite(value)) return { error: '请输入数字' };
  if (item.min !== undefined && value < item.min) return { error: `不能小于 ${item.min}` };
  if (item.max !== undefined && value > item.max) return { error: `不能大于 ${item.max}` };
  return { value };
}

// ------------------------------------------------------------- 复盘成本估算

export interface NudgeEstimate {
  days: number;
  /** 近 7 天按 `llm.call` 的 `purpose=chat` 行估算的对话轮数。 */
  turnsPerDay: number;
  reviewsPerDay: number;
  /** 没有实测样本时为 null —— 不编一个 0 当成本。 */
  tokensPerDay: number | null;
  truncated: boolean;
}

function purposeOf(row: AuditRow): string {
  const extra = row.extra ?? {};
  const purpose = extra['purpose'];
  return typeof purpose === 'string' && purpose.length > 0 ? purpose : 'chat';
}

function tokensOf(row: AuditRow): number {
  const extra = row.extra ?? {};
  const raw = extra['total_tokens'] ?? extra['tokens'];
  return typeof raw === 'number' && Number.isFinite(raw) ? raw : 0;
}

/**
 * 「按近 7 天均值 ≈ N 次/天、≈ M token/天」。
 *
 * 轮数用 `llm.call` 里 `purpose=chat` 的行数估算（一轮至少一次主模型调用）；
 * 单次复盘成本用 `purpose!=chat` 的行做实测均值。没有样本就如实回 null。
 */
export function estimateNudge(rows: AuditRow[], interval: number, days = 7): NudgeEstimate {
  const chat = rows.filter((row) => purposeOf(row) === 'chat').length;
  const aux = rows.filter((row) => purposeOf(row) !== 'chat');
  const auxTokens = aux.reduce((sum, row) => sum + tokensOf(row), 0);
  const turnsPerDay = chat / days;
  const reviewsPerDay = interval > 0 ? turnsPerDay / interval : 0;
  const perReview = aux.length > 0 ? auxTokens / aux.length : null;
  return {
    days,
    turnsPerDay,
    reviewsPerDay,
    tokensPerDay: perReview === null ? null : Math.round(reviewsPerDay * perReview),
    truncated: false,
  };
}
