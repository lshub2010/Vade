/**
 * L1 布局：拖拽位置 + `rev` 乐观锁。
 *
 * 契约（`schema/admin-api.yaml` · `/admin/api/layout`）：
 * - `GET` 全量布局 → `{rev, nodes:[{id,x,y,label,group_id}]}`
 * - `PATCH` 保存，body 带 `rev`；rev 不匹配 → **409 Conflict**
 * - `POST /layout/auto` 服务端算力导向坐标（前端不兜底）
 *
 * 两条实现约束：
 * 1. `patch_layout` 是**整表替换**：只发一个节点会把别人的位置删掉。所以每次保存都
 *    把当前完整布局发回去（含 label / group_id，标签只能走这里，走 `/nodes/{id}`
 *    会写审计）。
 * 2. 冲突时不静默合并：丢弃本地待保存位置、拉最新布局覆盖、弹提示。这里也**不**
 *    自动重试——那等于把别人的修改盖掉。
 *
 * 兼容说明：core 当前把 rev 冲突返回成 `400 INVALID_ARGUMENT`
 * "layout revision conflict"（契约写的是 409）。`isLayoutConflict` 同时认这两种，
 * 等 core 修好后可以直接删掉后半段。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, api, describeError } from '../../app/api';

export interface LayoutNode {
  id: string;
  x: number;
  y: number;
  label: string | null;
  group_id: string | null;
}

export interface LayoutResponse {
  rev: number;
  nodes: LayoutNode[];
}

export type LayoutNoticeTone = 'ok' | 'warn' | 'error';

export interface LayoutNotice {
  tone: LayoutNoticeTone;
  text: string;
}

export interface UseLayoutOptions {
  onNotice?: (notice: LayoutNotice) => void;
}

export interface LayoutApi {
  /** 已落库的位置，按节点 id 索引；给 graph 用。 */
  positions: Readonly<Record<string, { x: number; y: number }>>;
  rev: number;
  status: 'loading' | 'ready' | 'error';
  error: string;
  saving: boolean;
  savePosition: (id: string, x: number, y: number) => void;
  autoArrange: () => Promise<void>;
  reload: () => void;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function readLayout(raw: unknown): LayoutResponse {
  const source = isRecord(raw) ? raw : {};
  const rev = typeof source.rev === 'number' && Number.isFinite(source.rev) ? source.rev : 0;
  const nodes: LayoutNode[] = [];
  for (const entry of Array.isArray(source.nodes) ? source.nodes : []) {
    if (!isRecord(entry)) continue;
    const id = typeof entry.id === 'string' ? entry.id : null;
    const x = typeof entry.x === 'number' && Number.isFinite(entry.x) ? entry.x : null;
    const y = typeof entry.y === 'number' && Number.isFinite(entry.y) ? entry.y : null;
    if (!id || x === null || y === null) continue;
    nodes.push({
      id,
      x,
      y,
      label: typeof entry.label === 'string' ? entry.label : null,
      group_id: typeof entry.group_id === 'string' ? entry.group_id : null,
    });
  }
  return { rev, nodes };
}

export function isLayoutConflict(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  if (error.status === 409) return true;
  // TODO(core): `patch_layout` 目前抛 400 INVALID_ARGUMENT；契约要求 409。见交付说明。
  return (
    error.status === 400 &&
    error.code === 'INVALID_ARGUMENT' &&
    /layout revision conflict/i.test(error.message)
  );
}

async function fetchLayout(): Promise<LayoutResponse | null> {
  try {
    return readLayout(await api<unknown>('/admin/api/layout'));
  } catch {
    return null;
  }
}

export function useLayout(options: UseLayoutOptions = {}): LayoutApi {
  const [layout, setLayout] = useState<LayoutResponse>({ rev: 0, nodes: [] });
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  const layoutRef = useRef<LayoutResponse>({ rev: 0, nodes: [] });
  /** 最后一次服务端确认过的布局；保存失败时回到它，不假装成功。 */
  const persistedRef = useRef<LayoutResponse>({ rev: 0, nodes: [] });
  const pendingRef = useRef<Map<string, { x: number; y: number }>>(new Map());
  const savingRef = useRef(false);
  const mountedRef = useRef(true);
  const noticeRef = useRef<UseLayoutOptions['onNotice']>(options.onNotice);
  noticeRef.current = options.onNotice;

  const applyLayout = useCallback((next: LayoutResponse): void => {
    layoutRef.current = next;
    setLayout(next);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setStatus((current) => (current === 'ready' ? current : 'loading'));
    void (async () => {
      const next = await fetchLayout();
      if (cancelled) return;
      if (next === null) {
        setError('读不到布局（GET /admin/api/layout）');
        setStatus((current) => (current === 'ready' ? current : 'error'));
        return;
      }
      applyLayout(next);
      persistedRef.current = next;
      setError('');
      setStatus('ready');
    })();
    return () => {
      cancelled = true;
    };
  }, [reloadKey, applyLayout]);

  const flush = useCallback(async (): Promise<void> => {
    if (savingRef.current) return; // 已有循环在跑，它会继续清空 pending
    savingRef.current = true;
    setSaving(true);
    try {
      while (pendingRef.current.size > 0) {
        // 本轮要发的位置。先取快照再清空：清除后才到的拖动会进入下一轮。
        const updates = new Map(pendingRef.current);
        pendingRef.current.clear();
        const base = layoutRef.current;
        let nodes = base.nodes;
        for (const [id, position] of updates) {
          const existing = nodes.find((node) => node.id === id);
          nodes = existing
            ? nodes.map((node) => (node.id === id ? { ...node, x: position.x, y: position.y } : node))
            : [...nodes, { id, x: position.x, y: position.y, label: null, group_id: null }];
        }
        // 上一条响应可能覆盖了拖动期间的乐观位置，这里合回去再发。
        applyLayout({ rev: base.rev, nodes });
        // 整表替换：把完整布局发回去（含 label / group_id）。
        const body = {
          rev: base.rev,
          nodes: nodes.map((node) => ({
            id: node.id,
            x: node.x,
            y: node.y,
            label: node.label,
            group_id: node.group_id,
          })),
        };

        try {
          const data = await api<unknown>('/admin/api/layout', { method: 'PATCH', body });
          if (!mountedRef.current) return;
          const next = readLayout(data);
          applyLayout(next);
          persistedRef.current = next;
        } catch (caught) {
          if (!mountedRef.current) return;
          if (isLayoutConflict(caught)) {
            pendingRef.current.clear();
            const latest = await fetchLayout();
            if (latest) {
              applyLayout(latest);
              persistedRef.current = latest;
              noticeRef.current?.({
                tone: 'warn',
                text: '布局已被其他会话修改，已刷新为最新版本；你刚才拖动的位置没有保存。',
              });
            } else {
              applyLayout(persistedRef.current);
              noticeRef.current?.({
                tone: 'warn',
                text: '布局已被其他会话修改，但刷新最新布局失败；请刷新页面后再拖动。',
              });
            }
            return;
          }
          // 网络 / 服务端错误：回到最后一次确认过的位置，不静默当成功。
          applyLayout(persistedRef.current);
          noticeRef.current?.({
            tone: 'error',
            text: `布局保存失败：${describeError(caught)}（位置改动未保存）`,
          });
          return;
        }
      }
    } finally {
      savingRef.current = false;
      if (mountedRef.current) setSaving(false);
    }
  }, [applyLayout]);

  const savePosition = useCallback(
    (id: string, x: number, y: number): void => {
      if (status !== 'ready') return;
      const current = layoutRef.current;
      const existing = current.nodes.find((node) => node.id === id);
      const moved: LayoutNode = {
        id,
        x,
        y,
        label: existing?.label ?? null,
        group_id: existing?.group_id ?? null,
      };
      const nodes = existing
        ? current.nodes.map((node) => (node.id === id ? moved : node))
        : [...current.nodes, moved];
      // 乐观更新：拖完立刻停在落点，保存失败再回滚。
      applyLayout({ rev: current.rev, nodes });
      pendingRef.current.set(id, { x, y });
      void flush();
    },
    [status, applyLayout, flush],
  );

  const autoArrange = useCallback(async (): Promise<void> => {
    if (status !== 'ready' || savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    try {
      const data = await api<unknown>('/admin/api/layout/auto', { method: 'POST' });
      if (!mountedRef.current) return;
      const next = readLayout(data);
      applyLayout(next);
      persistedRef.current = next;
      noticeRef.current?.({ tone: 'ok', text: `已按服务端坐标重排布局（rev ${next.rev}）` });
    } catch (caught) {
      if (!mountedRef.current) return;
      noticeRef.current?.({ tone: 'error', text: `自动排列失败：${describeError(caught)}` });
    } finally {
      savingRef.current = false;
      if (mountedRef.current) setSaving(false);
    }
  }, [status, applyLayout]);

  const reload = useCallback(() => setReloadKey((key) => key + 1), []);

  const positions = useMemo(() => {
    const map: Record<string, { x: number; y: number }> = {};
    for (const node of layout.nodes) map[node.id] = { x: node.x, y: node.y };
    return map;
  }, [layout]);

  return {
    positions,
    rev: layout.rev,
    status,
    error,
    saving,
    savePosition,
    autoArrange,
    reload,
  };
}
