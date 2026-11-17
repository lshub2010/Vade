/**
 * 画布展示用的纯函数：状态文案、地址中间省略、加密锁标语义。
 *
 * 这里只做「怎么显示」，不做「状态是什么」——状态永远来自 core 的 `state` 字段
 * （design.md §7.3：前端不推导健康度）。
 */

export type StateTone = 'ok' | 'warn' | 'muted' | 'danger' | 'info';

const STATE_LABEL: Record<string, string> = {
  online: '在线',
  degraded: '降级',
  offline: '离线',
  rejected: '已拒绝',
  pending: '待接入',
};

const STATE_TONE: Record<string, StateTone> = {
  online: 'ok',
  degraded: 'warn',
  offline: 'muted',
  rejected: 'danger',
  pending: 'info',
};

/** 未登记的状态不猜，原样回显。 */
export function stateLabel(state: string): string {
  return STATE_LABEL[state] ?? state;
}

export function stateTone(state: string): StateTone {
  return STATE_TONE[state] ?? 'muted';
}

/**
 * 中间省略：`wss://10.0.1.7:9101` 在窄节点里要保住协议头与端口尾。
 * CSS 的 `text-overflow: ellipsis` 只能截尾，端口的最后几位恰恰是最需要看的部分。
 */
export function truncateMiddle(value: string, max = 26): string {
  if (value.length <= max) return value;
  const budget = max - 1; // 一个字符留给省略号
  // 尾部至少留 5 个字符，否则 `…01` 这种情况连端口都读不出来。
  const tail = Math.max(5, Math.floor(budget * 0.38));
  const head = Math.max(1, budget - tail);
  return `${value.slice(0, head)}…${value.slice(value.length - tail)}`;
}

/**
 * 展示前再剥一次 userinfo / query / fragment。
 *
 * core 的 `to_public_view()` 已经保证 `endpoint` 只到 `scheme://host:port`（§7.5），
 * 这里不是「拼 endpoint」，而是**防御性收窄**：即使后端回归、把 `wss://u:p@h?token=…`
 * 漏出来，画布也不会把它画到屏幕上（AC-19）。
 * 前端不做任何补全（不补默认端口、不补 scheme），拿不到就显示占位。
 */
export function displayEndpoint(endpoint: string | null): string | null {
  if (!endpoint) return null;
  const trimmed = endpoint.trim();
  if (trimmed.length === 0) return null;

  const q = trimmed.search(/[?#]/);
  let value = q === -1 ? trimmed : trimmed.slice(0, q);

  // scheme://user:pass@host → scheme://host
  const schemeEnd = value.indexOf('://');
  if (schemeEnd !== -1) {
    const authorityStart = schemeEnd + 3;
    const at = value.lastIndexOf('@');
    if (at > authorityStart) value = `${value.slice(0, authorityStart)}${value.slice(at + 1)}`;
  }

  return value.length > 0 ? value : null;
}

export interface EncryptionMark {
  /** solid = 端到端（Noise IK）；hollow = 仅 TLS；none = 后端没登记，不做任何暗示。 */
  kind: 'solid' | 'hollow' | 'none';
  title: string;
  short: string;
}

/**
 * 锁标语义（§7.7 / FR-3.7 / FR-8.25）。
 * 文案必须写清「是什么加密」，不能出现没有主语的「已加密」。
 * `browser_session` 只有 TLS —— 管理面本身没有端到端，画布不能画成实心锁。
 * `auth_mode` 缺失时给 none：宁可不画，也不冒充加密。
 */
export function encryptionMark(authMode: string | null): EncryptionMark {
  switch (authMode) {
    case 'module_key':
      return { kind: 'solid', title: '端到端加密（Noise IK，module_key）', short: '端到端加密' };
    case 'device_key':
      return { kind: 'solid', title: '端到端加密（Noise IK，device_key）', short: '端到端加密' };
    case 'browser_session':
      return { kind: 'hollow', title: '仅 TLS（browser_session，无端到端）', short: '仅 TLS' };
    default:
      return { kind: 'none', title: '未登记加密方式', short: '未知' };
  }
}

/** 节点主标题：别名优先，其次 module_id（后端给什么显示什么）。 */
export function nodeTitle(label: string | null, moduleId: string): string {
  return label && label.length > 0 ? label : moduleId;
}

export function toolBadge(count: number): string {
  return `${count} ${count === 1 ? 'tool' : 'tools'}`;
}
