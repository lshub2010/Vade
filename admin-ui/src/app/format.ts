/** 时间 / 数值 / 指纹的展示格式。 */

const RELATIVE_STEPS: Array<[limitSeconds: number, divisor: number, unit: string]> = [
  [60, 1, '秒'],
  [3_600, 60, '分钟'],
  [86_400, 3_600, '小时'],
  [2_592_000, 86_400, '天'],
];

/** epoch 秒 → 「12 秒前」。时钟偏差（未来时间）按「刚刚」。 */
export function relativeTime(epochSeconds: number | null, nowMs: number = Date.now()): string {
  if (epochSeconds === null || !Number.isFinite(epochSeconds)) return '—';
  const seconds = Math.max(0, Math.floor(nowMs / 1000 - epochSeconds));
  if (seconds < 5) return '刚刚';
  for (const [limit, divisor, unit] of RELATIVE_STEPS) {
    if (seconds < limit) return `${Math.floor(seconds / divisor)} ${unit}前`;
  }
  const days = Math.floor(seconds / 86_400);
  return `${days} 天前`;
}

export function absoluteTime(epochSeconds: number | null): string {
  if (epochSeconds === null || !Number.isFinite(epochSeconds)) return '无记录';
  return new Date(epochSeconds * 1000).toLocaleString('zh-CN', { hour12: false });
}

export function formatRtt(rttMs: number | null): string {
  if (rttMs === null || !Number.isFinite(rttMs)) return '—';
  if (rttMs < 1) return '<1 ms';
  return `${Math.round(rttMs)} ms`;
}

/** fp8 = 16 位小写 hex；分 4 组便于核对，不改变字符。 */
export function formatFingerprint(fp8: string | null): string {
  if (!fp8) return '未提供';
  const clean = fp8.trim();
  return clean.length === 16 ? clean.replace(/(.{4})(?=.)/g, '$1 ') : clean;
}

export function shortId(id: string): string {
  return id.length > 28 ? `${id.slice(0, 12)}…${id.slice(-8)}` : id;
}

/** 剩余秒数 → `9:58`。倒计时与绑定票共用一种写法。 */
export function formatCountdown(totalSeconds: number): string {
  const safe = Math.max(0, Math.floor(totalSeconds));
  const minutes = Math.floor(safe / 60);
  const seconds = safe % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}
