/**
 * 注册表快照 → React Flow 的 nodes / edges。
 *
 * 三条硬规则写在这里，别处不要再破：
 * 1. 一个模块一个节点（D35）：`tool_names` 只进面板，画布上只有 `tool_count`。
 * 2. 边只有 core → 节点一种；模块之间不连（FR-5.7）。出现两条模块间的线就是 bug。
 * 3. 状态的线型与颜色来自 `state` 字段，前端不推导（§7.3）。
 */

import type { Edge, Node } from '@xyflow/react';
import { MarkerType } from '@xyflow/react';
import type { NodeView } from '../app/types';
import { displayEndpoint, nodeTitle, truncateMiddle } from './format';
import type { NodeShape, Side, Size } from './shape';
import { SHAPE_SIZE, oppositeSide, sideFor } from './shape';

export const CORE_NODE_ID = '__core__';

export const NODE_TYPE = {
  core: 'topoCore',
  module: 'topoModule',
  invite: 'topoInvite',
  group: 'topoGroup',
} as const;

export type TopoKind = 'core' | 'module' | 'device' | 'pending';

/** 上层数据：`data` 必须是 Record，故用 type 别名（interface 没有隐式索引签名）。 */
export type TopoNodeData = {
  kind: TopoKind;
  shape: NodeShape;
  state: string;
  title: string;
  /** 已中间省略、可直接渲染的地址。 */
  subtitle: string | null;
  /** 完整地址，只进 title 属性（悬浮看全）。 */
  subtitleFull: string | null;
  moduleId: string;
  instanceId: string;
  toolCount: number;
  rttMs: number | null;
  version: string | null;
  authMode: string | null;
  /** 同 module_id 的实例数；> 1 时节点上标 `×N`。 */
  instanceCount: number;
  size: Size;
  /** core 卡片专用：注册表里的模块数 / 在线数。 */
  totals?: { nodes: number; online: number };
};

export type TopoFlowNode = Node<TopoNodeData, (typeof NODE_TYPE)[keyof typeof NODE_TYPE]>;

export type GroupNodeData = {
  title: string;
  count: number;
  width: number;
  height: number;
};

export type TopoGroupNode = Node<GroupNodeData, typeof NODE_TYPE.group>;

export type SessionEdgeData = {
  state: string;
  /** 只有 rejected 才有；来自 SSE 的 edge.state（本轮 /nodes 快照里没有这个字段）。 */
  reason?: string | null;
  /** 是否叠加流动虚线（T7：节点 > 50 时关掉）。 */
  flow: boolean;
};

export type TopoEdge = Edge<SessionEdgeData, 'session'>;

export interface Graph {
  nodes: TopoFlowNode[];
  edges: TopoEdge[];
  groups: TopoGroupNode[];
  /** 画布上非 core 的节点数（不含分组框）。 */
  count: number;
}

/** 形状判定优先级：桥 > 模型 > 记忆 > 工具 > 默认方框。 */
export function shapeFor(node: NodeView): NodeShape {
  if (node.state === 'pending') return 'invite';
  if (node.kind === 'device') return 'rounded';
  const caps = node.capabilities.map((c) => c.toLowerCase());
  const moduleId = node.module_id.toLowerCase();
  if (moduleId.includes('mcp') || caps.includes('bridge') || caps.includes('mcp')) return 'bridge';
  if (caps.includes('model')) return 'hexagon';
  if (caps.includes('memory')) return 'cylinder';
  return 'square';
}

function kindFor(node: NodeView): TopoKind {
  if (node.state === 'pending') return 'pending';
  return node.kind === 'device' ? 'device' : 'module';
}

const RING_RADIUS = [340, 660, 980, 1300];
const RING_CAPACITY = [12, 20, 28, 40];

interface Placement {
  x: number;
  y: number;
}

/**
 * 布局：core 在原点（虚拟坐标，fitView 会处理）。
 *
 * 后端给了 `x`/`y`（L1 布局表）就用它——那是用户拖过的位置，不能被前端重排覆盖；
 * 没给过的按环带均匀摆开，保证首次进入不是一坨。
 * 环带容量 12/20/28：第 13 个节点开始第二环，半径跳 320，避免与第一环的节点标签重叠。
 */
function place(items: NodeView[]): Map<string, Placement> {
  const placed = new Map<string, Placement>();
  let cursor = 0;
  let ring = 0;
  let indexInRing = 0;

  for (const item of items) {
    if (typeof item.x === 'number' && typeof item.y === 'number') {
      placed.set(item.id, { x: item.x, y: item.y });
      continue;
    }

    // 环带已满 → 下一环，角度从头开始但错开半个步长，视觉上不形成辐条。
    while (indexInRing >= (RING_CAPACITY[ring] ?? 40)) {
      ring += 1;
      indexInRing = 0;
    }
    const radius = RING_RADIUS[ring] ?? RING_RADIUS[RING_RADIUS.length - 1] ?? 340;
    const capacity = RING_CAPACITY[ring] ?? 40;
    const step = (Math.PI * 2) / capacity;
    const angle = -Math.PI / 2 + indexInRing * step + (ring > 0 ? step / 2 : 0);
    const size = SHAPE_SIZE[shapeFor(item)];

    placed.set(item.id, {
      x: Math.round(Math.cos(angle) * radius - size.width / 2),
      y: Math.round(Math.sin(angle) * radius - size.height / 2),
    });
    indexInRing += 1;
    cursor += 1;
  }

  return placed;
}

function compareNodes(a: NodeView, b: NodeView): number {
  if (a.module_id !== b.module_id) return a.module_id < b.module_id ? -1 : 1;
  if (a.instance_id !== b.instance_id) return a.instance_id < b.instance_id ? -1 : 1;
  return a.id < b.id ? -1 : 1;
}

function moduleNode(item: NodeView, placement: Placement, instanceCount: number): TopoFlowNode {
  const shape = shapeFor(item);
  const size = SHAPE_SIZE[shape];
  const endpoint = displayEndpoint(item.endpoint);

  return {
    id: item.id,
    type: item.state === 'pending' ? NODE_TYPE.invite : NODE_TYPE.module,
    position: placement,
    draggable: false,
    connectable: false,
    // 尺寸写进 style：好处是首帧不用等 DOM 测量，fitView 不会跳两次。
    style: { width: size.width, height: size.height },
    data: {
      kind: kindFor(item),
      shape,
      state: item.state,
      title: nodeTitle(item.label, item.module_id),
      subtitle: endpoint ? truncateMiddle(endpoint) : null,
      subtitleFull: endpoint,
      moduleId: item.module_id,
      instanceId: item.instance_id,
      toolCount: item.tool_count,
      rttMs: item.rtt_ms,
      version: item.version,
      authMode: item.auth_mode,
      instanceCount,
      size,
    },
  };
}

function coreNode(totals: { nodes: number; online: number }): TopoFlowNode {
  const size = SHAPE_SIZE.core;
  return {
    id: CORE_NODE_ID,
    type: NODE_TYPE.core,
    position: { x: -size.width / 2, y: -size.height / 2 },
    draggable: false,
    connectable: false,
    selectable: false,
    style: { width: size.width, height: size.height },
    data: {
      kind: 'core',
      shape: 'core',
      // core 自身永远是 online：画布画得出来就说明 core 活着。
      state: 'online',
      title: 'core',
      subtitle: 'vade/v1',
      subtitleFull: null,
      moduleId: '',
      instanceId: '',
      toolCount: 0,
      rttMs: null,
      version: null,
      authMode: null,
      instanceCount: 1,
      size,
      totals,
    },
  };
}

interface Frame {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** 同 module_id 的多实例（手机 + 电脑）套一个虚线外框，标题是 module_id。 */
function frameFor(members: TopoFlowNode[]): Frame {
  const padX = 18;
  const padTop = 26;
  const padBottom = 14;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;

  for (const member of members) {
    const w = member.data.size.width;
    const h = member.data.size.height;
    minX = Math.min(minX, member.position.x);
    minY = Math.min(minY, member.position.y);
    maxX = Math.max(maxX, member.position.x + w);
    maxY = Math.max(maxY, member.position.y + h);
  }

  return {
    x: minX - padX,
    y: minY - padTop,
    width: maxX - minX + padX * 2,
    height: maxY - minY + padTop + padBottom,
  };
}

function sideHandles(distance: { dx: number; dy: number }): { source: Side; target: Side } {
  const source = sideFor(distance.dx, distance.dy);
  return { source, target: oppositeSide(source) };
}

function center(node: { position: Placement; data: { size: Size } }): { x: number; y: number } {
  return {
    x: node.position.x + node.data.size.width / 2,
    y: node.position.y + node.data.size.height / 2,
  };
}

export function buildGraph(items: NodeView[]): Graph {
  const ordered = [...items].sort(compareNodes);
  const placements = place(ordered);

  const byModule = new Map<string, number>();
  for (const item of ordered) {
    byModule.set(item.module_id, (byModule.get(item.module_id) ?? 0) + 1);
  }

  const online = ordered.filter((item) => item.state === 'online').length;
  const core = coreNode({ nodes: ordered.length, online });
  const coreCenter = center(core);

  const moduleNodes = ordered.map((item) =>
    moduleNode(item, placements.get(item.id) ?? { x: 0, y: 0 }, byModule.get(item.module_id) ?? 1),
  );

  // 流动动画是纯装饰，超过 50 个节点关掉（T7），但颜色与线型照旧。
  const flowAllowed = ordered.length <= 50;

  const edges: TopoEdge[] = moduleNodes.map((node) => {
    const nodeCenter = center(node);
    const { source, target } = sideHandles({
      dx: nodeCenter.x - coreCenter.x,
      dy: nodeCenter.y - coreCenter.y,
    });
    const isPending = node.data.kind === 'pending';

    return {
      id: `edge:${node.id}`,
      type: 'session',
      source: CORE_NODE_ID,
      target: node.id,
      sourceHandle: source,
      targetHandle: target,
      selectable: false,
      focusable: false,
      data: {
        state: node.data.state,
        reason: null,
        // pending 的线是「等接入」的占位，不该有流动感——它还没通。
        flow: flowAllowed && (node.data.state === 'online' || node.data.state === 'degraded'),
      },
      // pending 的箭头指向 core（「待接入」而不是「已连上」），所以用 markerStart。
      markerStart: isPending
        ? { type: MarkerType.ArrowClosed, color: 'var(--info)', width: 14, height: 14 }
        : undefined,
    };
  });

  const groups: TopoGroupNode[] = [];
  for (const [moduleId, count] of byModule) {
    if (count < 2) continue;
    const members = moduleNodes.filter((node) => node.data.moduleId === moduleId);
    if (members.length < 2) continue;
    const frame = frameFor(members);
    groups.push({
      id: `group:${moduleId}`,
      type: NODE_TYPE.group,
      position: { x: frame.x, y: frame.y },
      data: { title: moduleId, count: members.length, width: frame.width, height: frame.height },
      style: { width: frame.width, height: frame.height },
      draggable: false,
      selectable: false,
      connectable: false,
      focusable: false,
      zIndex: -1,
    });
  }

  return { nodes: [core, ...moduleNodes], edges, groups, count: moduleNodes.length };
}
