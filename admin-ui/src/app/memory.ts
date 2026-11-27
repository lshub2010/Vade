/**
 * `/memory` 的筛选状态、URL 拼装与文案。
 *
 * 只做展示层的事：把契约里的枚举翻成中文、把筛选拼成查询串。
 * 记忆正文不做任何解析 —— 它来自模型，按纯文本渲染（React 转义）。
 */

import { ApiError, describeError } from './api';
import type { MemoryLayer, MemoryStatus } from './types';

/** 契约默认 50；下拉只给三档，避免手输 100000 把 core 拖住。 */
export const MEMORY_LIMITS = [20, 50, 100] as const;

export type MemoryLimit = (typeof MEMORY_LIMITS)[number];

export interface MemoryFilters {
  /** 空串 = 全部层。 */
  layer: MemoryLayer | '';
  /** 空串 = 全部状态。 */
  status: MemoryStatus | '';
  namespace: string;
  /** 关键字；服务端在正文上做匹配，页面不本地过滤。 */
  q: string;
  limit: MemoryLimit;
}

export function defaultMemoryFilters(): MemoryFilters {
  return { layer: '', status: '', namespace: '', q: '', limit: 50 };
}

/** 有没有收窄条件：决定空列表时文案是「还没有记忆」还是「没有符合条件」。 */
export function isFiltered(filters: MemoryFilters): boolean {
  return (
    filters.layer !== '' ||
    filters.status !== '' ||
    filters.namespace.trim().length > 0 ||
    filters.q.trim().length > 0
  );
}

/** `limit` 每次都带：服务端默认值可能与页面的缺省档位不同。 */
export function buildMemoryPath(filters: MemoryFilters, cursor: string | null): string {
  const params = new URLSearchParams();
  if (filters.layer !== '') params.set('layer', filters.layer);
  if (filters.status !== '') params.set('status', filters.status);
  const namespace = filters.namespace.trim();
  if (namespace.length > 0) params.set('namespace', namespace);
  const q = filters.q.trim();
  if (q.length > 0) params.set('q', q);
  params.set('limit', String(filters.limit));
  if (cursor !== null && cursor.length > 0) params.set('cursor', cursor);
  return `/admin/api/memory?${params.toString()}`;
}

export function memoryPath(id: string): string {
  return `/admin/api/memory/${encodeURIComponent(id)}`;
}

/** 三种治理动作；对应的 POST 路径就是条目路径 + 动作名。 */
export type MemoryAction = 'promote' | 'archive' | 'restore';

export function memoryActionLabel(action: MemoryAction): string {
  if (action === 'promote') return '晋升';
  if (action === 'archive') return '归档';
  return '恢复';
}

export function memoryActionPath(id: string, action: MemoryAction): string {
  return `${memoryPath(id)}/${action}`;
}

export function layerLabel(layer: string): string {
  if (layer === 'session') return '会话';
  if (layer === 'global') return '全局';
  return layer.length > 0 ? layer : '—';
}

export interface MemoryStatusMeta {
  label: string;
  className: string;
  hint: string;
}

export function memoryStatusMeta(status: string): MemoryStatusMeta {
  switch (status) {
    case 'staging':
      return {
        label: '待确认',
        className: 'badge badge-warn',
        hint: 'staging：还没进入全局召回，晋升后才会被注入上下文',
      };
    case 'active':
      return {
        label: '生效中',
        className: 'badge badge-ok',
        hint: 'active：参与召回；可归档（需理由）',
      };
    case 'archived':
      return {
        label: '已归档',
        className: 'badge badge-muted',
        hint: 'archived：已移出召回，内容仍在，可恢复',
      };
    case 'superseded':
      return {
        label: '已被替换',
        className: 'badge badge-info',
        hint: 'superseded：已有更新的条目取代它，不再召回',
      };
    default:
      return {
        label: status.length > 0 ? status : '未知',
        className: 'badge badge-muted',
        hint: '契约外的状态值：页面按原样显示，不猜含义',
      };
  }
}

/** 只有 staging 能晋升；其余状态按钮不渲染。 */
export function canPromote(status: string): boolean {
  return status === 'staging';
}

/** staging / active 可归档；已归档与已被替换的不再重复归档。 */
export function canArchive(status: string): boolean {
  return status === 'staging' || status === 'active';
}

export function canRestore(status: string): boolean {
  return status === 'archived';
}

/** 正文折叠阈值：列表只给一眼，全文在详情抽屉里。 */
export const MEMORY_PREVIEW_CHARS = 220;

export function previewText(text: string | undefined): { preview: string; truncated: boolean } {
  const value = (text ?? '').trim();
  if (value.length === 0) return { preview: '（无正文）', truncated: false };
  if (value.length <= MEMORY_PREVIEW_CHARS) return { preview: value, truncated: false };
  return { preview: `${value.slice(0, MEMORY_PREVIEW_CHARS)}…`, truncated: true };
}

/**
 * `/memory` 的错误文案。
 *
 * 与 `describeError` 的差别只在几种要说得更具体的状态：403 只读、404 条目已被别人处理、
 * 409 状态冲突、400 参数（归档理由）、501 构建不含记忆模块。其余交给通用翻译。
 */
export function describeMemoryError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403 && error.code === 'READ_ONLY_SESSION') {
      return '当前是只读会话：浏览器形态只能查看，晋升 / 归档 / 恢复要在 Vade App 里做。';
    }
    if (error.status === 404) {
      return '目标记忆已不存在：可能已在其它设备上被归档、替换或清理。已重新读取。';
    }
    if (error.status === 409) {
      return `状态冲突（${error.code}）：${error.message}。条目可能刚被其它设备改过，已重新读取。`;
    }
    if (error.status === 400) {
      return `请求被拒绝（${error.code}）：${error.message}`;
    }
    if (error.status === 501) {
      return '本构建不含记忆模块（501 UNSUPPORTED）。';
    }
  }
  return describeError(error);
}
