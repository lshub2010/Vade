/**
 * 拓扑数据：一次快照 + SSE 增量归并。
 *
 * 四条规则（改之前先读）：
 * 1. 只消费契约白名单字段（`NodeView`）。事件里多出来的字段一律丢弃——即使后端
 *    回归把 `psk` 塞进 payload，它也不会进 React state（AC-19 的前端侧）。
 * 2. 状态不推导。`state` 只来自 `GET /nodes` 或事件里后端给的 `state`。core 当前的
 *    `node.upsert` 发布的是 `{id, changes}`（`changes` 是 enabled/concurrency/risk
 *    这类**不在** NodeView 里的字段），前端无法据此更新状态，也不猜——改为
 *    `GET /nodes/{id}` 取后端真值。等 C5a 把完整 NodeView 放进事件后自动走全量分支。
 * 3. 按 seq 去重：`<= appliedSeq` 丢弃（浏览器自动重连时服务端会重放 buffer）。
 * 4. 断线不清空画布：items 留着，只切 `live` 状态并显示横幅（T4）。
 *
 * 重连策略：EventSource 自己会重连，但它是固定 ~3s 且我们拿不到退避控制；T4 要求
 * 1s→30s 指数退避，所以 onerror 时主动 close，自己定时重开。代价是新连接不会带
 * `Last-Event-ID`（EventSource 无法设 header），因此每次重开前先做一次全量快照——
 * 这比 replay 更强：没有缺口。seq 去重只用于同一条连接内的重复投递。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, api, describeError } from '../../app/api';
import type { NodeListResponse, NodeView } from '../../app/types';

export type LiveStatus = 'idle' | 'connecting' | 'live' | 'reconnecting' | 'polling';

export interface EdgeOverlay {
  /** 后端给的边状态，原样使用。 */
  state: string;
  /** 只有 rejected 才有（`reason` 不进 NodeView 白名单，只从事件来）。 */
  reason: string | null;
  since: number | null;
}

export interface TopologyApi {
  items: NodeView[];
  byId: Readonly<Record<string, NodeView>>;
  edgeStates: Readonly<Record<string, EdgeOverlay>>;
  status: 'loading' | 'ready' | 'error';
  error: string;
  live: LiveStatus;
  /** 下一次重连的倒计时（秒）；不在重连中为 null。 */
  retryIn: number | null;
  /** 启停状态不在 NodeView 白名单里，只能记我们自己操作成功的结果。 */
  disabledIds: ReadonlySet<string>;
  markEnabled: (id: string, enabled: boolean) => void;
  reload: () => void;
  refreshNode: (id: string) => Promise<void>;
}

const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;
const POLL_MS = 5_000;
/** 连续失败这么多次且从未连上过 → 判定 SSE 不可用，降级为轮询。 */
const POLL_FALLBACK_AFTER = 3;
/** 同一节点的增量事件合并成一次 GET。 */
const REFRESH_DEBOUNCE_MS = 150;

type Raw = Record<string, unknown>;

function isRecord(value: unknown): value is Raw {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function asString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((entry): entry is string => typeof entry === 'string');
}

/**
 * 白名单化：只复制契约里有的字段。
 * `id` 与 `state` 是「这是一个完整节点」的最低要求；缺任一个返回 null，
 * 调用方会走增量/刷新分支，而不是拿着半条数据覆盖画布。
 */
export function whitelistNode(raw: unknown): NodeView | null {
  if (!isRecord(raw)) return null;
  const id = asString(raw.id);
  const state = asString(raw.state);
  if (!id || !state) return null;

  return {
    id,
    kind: raw.kind === 'device' ? 'device' : 'module',
    module_id: asString(raw.module_id) ?? '',
    instance_id: asString(raw.instance_id) ?? '',
    label: asString(raw.label),
    capabilities: asStringArray(raw.capabilities),
    state,
    endpoint: asString(raw.endpoint),
    protocol: asString(raw.protocol) ?? 'vade/v1',
    version: asString(raw.version),
    auth_mode: asString(raw.auth_mode),
    rtt_ms: asNumber(raw.rtt_ms),
    last_seen: asNumber(raw.last_seen),
    x: asNumber(raw.x),
    y: asNumber(raw.y),
    revoked: raw.revoked === true,
    tool_count: asNumber(raw.tool_count) ?? 0,
    tool_names: asStringArray(raw.tool_names),
  };
}

/** 增量补丁：只接受 NodeView 里存在的字段，且类型要对得上。 */
function patchNode(node: NodeView, changes: Raw): NodeView {
  const next: NodeView = { ...node };
  if (typeof changes.label === 'string' || changes.label === null) next.label = changes.label as string | null;
  if (typeof changes.state === 'string') next.state = changes.state;
  if (typeof changes.endpoint === 'string' || changes.endpoint === null) {
    next.endpoint = changes.endpoint as string | null;
  }
  if (typeof changes.version === 'string' || changes.version === null) next.version = changes.version as string | null;
  if (typeof changes.auth_mode === 'string' || changes.auth_mode === null) {
    next.auth_mode = changes.auth_mode as string | null;
  }
  if (typeof changes.rtt_ms === 'number' || changes.rtt_ms === null) next.rtt_ms = changes.rtt_ms as number | null;
  if (typeof changes.last_seen === 'number' || changes.last_seen === null) {
    next.last_seen = changes.last_seen as number | null;
  }
  if (typeof changes.revoked === 'boolean') next.revoked = changes.revoked;
  if (typeof changes.tool_count === 'number') next.tool_count = changes.tool_count;
  if (Array.isArray(changes.tool_names)) next.tool_names = asStringArray(changes.tool_names);
  return next;
}

/** 这些键只影响 core 的治理状态，不体现在 NodeView 上 → 需要回头拉一次真值。 */
const NON_VIEW_KEYS = ['enabled', 'concurrency', 'risk'];

export interface ParsedEvent {
  seq: number | null;
  type: string;
  data: Raw;
}

/** `data: {"type":"node.upsert",...}` + SSE `id:` 行。格式见 stream.py。 */
export function parseStreamEvent(raw: string, lastEventId: string): ParsedEvent | null {
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isRecord(payload)) return null;
  const type = asString(payload.type);
  if (!type) return null;
  const seq = Number.parseInt(lastEventId, 10);
  return { seq: Number.isFinite(seq) ? seq : null, type, data: payload };
}

export function useTopology(): TopologyApi {
  const [byId, setById] = useState<Record<string, NodeView>>({});
  const [edgeStates, setEdgeStates] = useState<Record<string, EdgeOverlay>>({});
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [error, setError] = useState('');
  const [live, setLive] = useState<LiveStatus>('idle');
  const [nextRetryAt, setNextRetryAt] = useState<number | null>(null);
  const [disabledIds, setDisabledIds] = useState<ReadonlySet<string>>(() => new Set<string>());
  const [reloadKey, setReloadKey] = useState(0);
  // 倒计时需要一次心跳；只读这个值来触发重渲染。
  const [tick, setTick] = useState(0);

  const refreshTimers = useRef<Map<string, number>>(new Map());

  const refreshNode = useCallback(async (id: string): Promise<void> => {
    try {
      const view = await api<NodeView>(`/admin/api/nodes/${encodeURIComponent(id)}`);
      const node = whitelistNode(view);
      if (!node) return;
      setById((previous) => ({ ...previous, [node.id]: node }));
      // 全量视图里的 state 是权威值：有了它，旧的 edge.state 叠加就该退场。
      setEdgeStates((previous) => {
        if (!(id in previous)) return previous;
        const next = { ...previous };
        delete next[id];
        return next;
      });
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 404) {
        setById((previous) => {
          if (!(id in previous)) return previous;
          const next = { ...previous };
          delete next[id];
          return next;
        });
      }
      // 其他错误保留旧视图：下一次事件或手动刷新会再试，不把画布清空。
    }
  }, []);

  const markEnabled = useCallback((id: string, enabled: boolean): void => {
    setDisabledIds((previous) => {
      const next = new Set(previous);
      if (enabled) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const scheduleRefresh = useCallback(
    (id: string): void => {
      if (refreshTimers.current.has(id)) return;
      const timer = window.setTimeout(() => {
        refreshTimers.current.delete(id);
        void refreshNode(id);
      }, REFRESH_DEBOUNCE_MS);
      refreshTimers.current.set(id, timer);
    },
    [refreshNode],
  );

  useEffect(() => {
    let cancelled = false;
    let source: EventSource | null = null;
    let reconnectTimer: number | null = null;
    let pollTimer: number | null = null;
    let attempts = 0;
    let openedOnce = false;
    let appliedSeq = 0;

    function clearReconnect(): void {
      if (reconnectTimer === null) return;
      window.clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }

    function clearPoll(): void {
      if (pollTimer === null) return;
      window.clearInterval(pollTimer);
      pollTimer = null;
    }

    function closeSource(): void {
      if (!source) return;
      source.onopen = null;
      source.onmessage = null;
      source.onerror = null;
      source.close();
      source = null;
    }

    /** 全量快照。成功返回 true；失败时保留已有画布（T4：断线不清空）。 */
    async function snapshot(): Promise<boolean> {
      try {
        const data = await api<NodeListResponse>('/admin/api/nodes');
        if (cancelled) return false;
        const map: Record<string, NodeView> = {};
        for (const item of Array.isArray(data.items) ? data.items : []) {
          const node = whitelistNode(item);
          if (node) map[node.id] = node;
        }
        setById(map);
        setEdgeStates({});
        appliedSeq = 0;
        setStatus('ready');
        setError('');
        return true;
      } catch (caught) {
        if (cancelled) return false;
        const message = describeError(caught);
        if (message.length === 0) return false;
        setError(message);
        setStatus((current) => (current === 'ready' ? current : 'error'));
        return false;
      }
    }

    function startPolling(): void {
      closeSource();
      clearReconnect();
      setNextRetryAt(null);
      setLive('polling');
      if (pollTimer !== null) return;
      const tickOnce = (): void => {
        if (cancelled) return;
        void snapshot();
      };
      tickOnce();
      pollTimer = window.setInterval(tickOnce, POLL_MS);
    }

    function scheduleReconnect(): void {
      attempts += 1;
      // 从没连上过：大概率是 SSE 在这种会话形态下根本不通（webview 用 Bearer，
      // EventSource 不能加 header），三次之后换轮询，至少画布还能更新。
      if (!openedOnce && attempts >= POLL_FALLBACK_AFTER) {
        startPolling();
        return;
      }
      const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** (attempts - 1));
      setLive('reconnecting');
      setNextRetryAt(Date.now() + delay);
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        setNextRetryAt(null);
        void (async () => {
          // 快照失败也继续开流：流可能先于注册表恢复。
          await snapshot();
          if (!cancelled) openStream();
        })();
      }, delay);
    }

    function handleEvent(raw: string, lastEventId: string): void {
      const event = parseStreamEvent(raw, lastEventId);
      if (!event) return;
      if (event.seq !== null) {
        if (event.seq <= appliedSeq) return; // 重连重放：丢弃
        appliedSeq = event.seq;
      }

      const data = event.data;
      const rawId = asString(data.id) ?? asString(data.node_id);
      const id = rawId ? rawId.replace(/^edge[:/]/, '') : '';

      if (event.type === 'resync') {
        // 环溢出：服务端不重放历史，只说明「你的 cursor 已经追不上了」。
        // 丢掉水位并整表重拉，否则 node.remove 会永久丢失（幽灵节点）。
        appliedSeq = 0;
        void snapshot();
        return;
      }

      if (event.type === 'node.remove') {
        if (!id) return;
        setById((previous) => {
          if (!(id in previous)) return previous;
          const next = { ...previous };
          delete next[id];
          return next;
        });
        setEdgeStates((previous) => {
          if (!(id in previous)) return previous;
          const next = { ...previous };
          delete next[id];
          return next;
        });
        return;
      }

      if (event.type === 'node.upsert') {
        if (!id) return;
        const full = whitelistNode(isRecord(data.node) ? data.node : data);
        if (full && full.id === id) {
          setById((previous) => ({ ...previous, [full.id]: full }));
          setEdgeStates((previous) => {
            if (!(id in previous)) return previous;
            const next = { ...previous };
            delete next[id];
            return next;
          });
          return;
        }
        const changes = isRecord(data.changes) ? data.changes : null;
        if (!changes) return;
        setById((previous) => {
          const current = previous[id];
          if (!current) return previous;
          return { ...previous, [id]: patchNode(current, changes) };
        });
        if (NON_VIEW_KEYS.some((key) => key in changes)) scheduleRefresh(id);
        return;
      }

      if (event.type === 'edge.state') {
        if (!id) return;
        const state = asString(data.state);
        if (!state) return;
        const overlay: EdgeOverlay = {
          state,
          reason: asString(data.reason),
          since: asNumber(data.since),
        };
        setEdgeStates((previous) => ({ ...previous, [id]: overlay }));
      }
    }

    function openStream(): void {
      if (cancelled) return;
      closeSource();
      setLive('connecting');
      const es = new EventSource('/admin/api/stream');
      source = es;
      es.onopen = () => {
        if (cancelled) return;
        attempts = 0;
        openedOnce = true;
        setLive('live');
        setNextRetryAt(null);
        // 快照与订阅之间有一个窗口：服务端对新订阅者从 head 起不回放，
        // 窗口内发生的 node.remove 会静默丢失。订阅建立后再拉一次幂等快照收口。
        void snapshot();
      };
      es.onmessage = (message: MessageEvent<string>) => {
        if (cancelled) return;
        handleEvent(message.data, message.lastEventId);
      };
      es.onerror = () => {
        if (cancelled) return;
        closeSource();
        scheduleReconnect();
      };
    }

    setStatus((current) => (current === 'ready' ? current : 'loading'));
    void (async () => {
      const ok = await snapshot();
      if (cancelled) return;
      if (ok) openStream();
      else scheduleReconnect();
    })();

    return () => {
      cancelled = true;
      closeSource();
      clearReconnect();
      clearPoll();
    };
  }, [reloadKey, scheduleRefresh]);

  // 重连倒计时的 500ms 心跳；不在重连中就不开定时器。
  useEffect(() => {
    if (live !== 'reconnecting') return;
    const timer = window.setInterval(() => setTick((value) => value + 1), 500);
    return () => window.clearInterval(timer);
  }, [live]);

  useEffect(
    () => () => {
      for (const timer of refreshTimers.current.values()) window.clearTimeout(timer);
      refreshTimers.current.clear();
    },
    [],
  );

  const reload = useCallback(() => setReloadKey((key) => key + 1), []);

  const items = useMemo(() => Object.values(byId), [byId]);

  const retryIn = useMemo(() => {
    if (nextRetryAt === null) return null;
    // tick 只用于按时间推进重算。
    void tick;
    return Math.max(0, Math.ceil((nextRetryAt - Date.now()) / 1000));
  }, [nextRetryAt, tick]);

  return {
    items,
    byId,
    edgeStates,
    status,
    error,
    live,
    retryIn,
    disabledIds,
    markEnabled,
    reload,
    refreshNode,
  };
}
