/**
 * 画布容器：把注册表快照渲染成 core 为中心的辐射图。
 *
 * 这一批（T1/T2）只做「画对」：读一次 `GET /admin/api/nodes` 后静态渲染。
 * 拖拽 / SSE / 属性面板在 T3、T4、T5，见各处的 TODO —— 这里没有假实现。
 */

import { useMemo } from 'react';
import type { ReactElement } from 'react';
import {
  Background,
  BackgroundVariant,
  Controls,
  MarkerType,
  ReactFlow,
  ReactFlowProvider,
} from '@xyflow/react';
import type { EdgeTypes, NodeTypes } from '@xyflow/react';
import type { NodeView } from '../app/types';
import '@xyflow/react/dist/style.css';
import './styles/topology.css';
import { SessionEdge } from './edges/SessionEdge';
import { buildGraph, NODE_TYPE } from './graph';
import { CoreNode } from './nodes/CoreNode';
import { GroupFrame } from './nodes/GroupFrame';
import { InviteNode } from './nodes/InviteNode';
import { ModuleNode } from './nodes/ModuleNode';

/**
 * 必须定义在组件外：每次渲染新建对象会让 React Flow 重挂所有节点，
 * 表现是「每点一下画布闪一次」，而且 200 节点时直接掉帧。
 */
const NODE_TYPES: NodeTypes = {
  [NODE_TYPE.core]: CoreNode,
  [NODE_TYPE.module]: ModuleNode,
  [NODE_TYPE.invite]: InviteNode,
  [NODE_TYPE.group]: GroupFrame,
};

const EDGE_TYPES: EdgeTypes = {
  session: SessionEdge,
};

const DEFAULT_EDGE_OPTIONS = {
  type: 'session',
  markerEnd: { type: MarkerType.ArrowClosed, color: 'var(--border-strong)', width: 12, height: 12 },
};

/**
 * React Flow 默认的 onError 只在 dev 里 warn（`createDevWarn` 在生产构建里是空函数）。
 * 边被静默丢弃时的错误码是 008（source/target handle 没找到），症状是「节点都在、一条边都没有」
 * 而且控制台干净——这次就是这么被误导的。显式接住，至少留一条能查的日志。
 */
function reportFlowError(code: string, message: string): void {
  // 008 已经在 styles/topology.css 的 .topo-handle 注释里写了成因，别只报错不解释。
  console.error(`[topo] react-flow ${code}: ${message}`);
}

function Canvas({ items }: { items: NodeView[] }): ReactElement {
  const graph = useMemo(() => buildGraph(items), [items]);
  const nodes = useMemo(() => [...graph.groups, ...graph.nodes], [graph]);

  return (
    <ReactFlow
      nodes={nodes}
      edges={graph.edges}
      nodeTypes={NODE_TYPES}
      edgeTypes={EDGE_TYPES}
      defaultEdgeOptions={DEFAULT_EDGE_OPTIONS}
      // 这一批不接拖拽：位置只读，PATCH /layout 在 T3 上线。
      nodesDraggable={false}
      nodesConnectable={false}
      edgesReconnectable={false}
      onError={reportFlowError}
      // 缩放（滚轮/按钮）与平移（空白拖）是 T3 之前就该有的基础操作。
      zoomOnScroll
      zoomOnPinch
      panOnDrag
      minZoom={0.25}
      maxZoom={1.75}
      fitView
      fitViewOptions={{ padding: 0.22, maxZoom: 1.1 }}
      proOptions={{ hideAttribution: false }}
      // TODO(T5)：双击打开右侧属性面板（工具列表分页），不是子节点（D35）。
      // TODO(T4)：接入 EventSource('/admin/api/stream')，按 seq 去重合并。
      aria-label="拓扑画布"
    >
      <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="#232c3d" />
      <Controls showInteractive={false} position="bottom-right" />
    </ReactFlow>
  );
}

export function FlowCanvas({ items }: { items: NodeView[] }): ReactElement {
  const count = items.length;
  return (
    <div className={count > 50 ? 'topo-canvas topo-no-flow' : 'topo-canvas'}>
      <ReactFlowProvider>
        <Canvas items={items} />
      </ReactFlowProvider>
    </div>
  );
}
