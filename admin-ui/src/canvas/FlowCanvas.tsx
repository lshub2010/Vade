/**
 * 画布容器：core 为中心的辐射图 + 拖拽 / 双击。
 *
 * 这一层只管交互接线：
 * - 拖动结束（`onNodeDragStop`）才回调一次，由 useLayout 去 PATCH；拖动过程中不写后端。
 * - 双击节点打开右侧属性面板；工具不进画布（D35）。
 * - 可视化编码、边的状态全部来自 graph.ts，不在这里二次判断。
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
import type { EdgeTypes, Node as FlowNode, NodeTypes } from '@xyflow/react';
import type { NodeView } from '../app/types';
import '@xyflow/react/dist/style.css';
import './styles/topology.css';
import { SessionEdge } from './edges/SessionEdge';
import { buildGraph, CORE_NODE_ID, NODE_TYPE } from './graph';
import type { BuildGraphOptions } from './graph';
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

export interface FlowCanvasProps {
  items: NodeView[];
  positions?: BuildGraphOptions['positions'];
  edgeStates?: BuildGraphOptions['edgeStates'];
  /** 布局未加载完时不允许拖：否则第一次拖动会把未知的整表布局覆盖掉。 */
  layoutReady: boolean;
  onMoveNode: (id: string, x: number, y: number) => void;
  onOpenNode: (id: string) => void;
}

function Canvas({
  items,
  positions,
  edgeStates,
  layoutReady,
  onMoveNode,
  onOpenNode,
}: FlowCanvasProps): ReactElement {
  const graph = useMemo(
    () => buildGraph(items, { positions, edgeStates }),
    [items, positions, edgeStates],
  );
  const nodes = useMemo(() => [...graph.groups, ...graph.nodes], [graph]);

  const isSelectableTarget = (id: string): boolean => id !== CORE_NODE_ID && !id.startsWith('group:');

  return (
    <ReactFlow
      nodes={nodes}
      edges={graph.edges}
      nodeTypes={NODE_TYPES}
      edgeTypes={EDGE_TYPES}
      defaultEdgeOptions={DEFAULT_EDGE_OPTIONS}
      nodesDraggable={layoutReady}
      nodesConnectable={false}
      edgesReconnectable={false}
      onError={reportFlowError}
      onNodeDragStop={(_event, node: FlowNode) => {
        if (!isSelectableTarget(node.id)) return;
        onMoveNode(node.id, Math.round(node.position.x), Math.round(node.position.y));
      }}
      onNodeDoubleClick={(_event, node: FlowNode) => {
        if (!isSelectableTarget(node.id)) return;
        onOpenNode(node.id);
      }}
      zoomOnScroll
      zoomOnPinch
      panOnDrag
      minZoom={0.25}
      maxZoom={1.75}
      fitView
      fitViewOptions={{ padding: 0.22, maxZoom: 1.1 }}
      proOptions={{ hideAttribution: false }}
      aria-label="拓扑画布"
    >
      <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="#232c3d" />
      <Controls showInteractive={false} position="bottom-right" />
    </ReactFlow>
  );
}

export function FlowCanvas(props: FlowCanvasProps): ReactElement {
  const count = props.items.length;
  return (
    <div className={count > 50 ? 'topo-canvas topo-no-flow' : 'topo-canvas'}>
      <ReactFlowProvider>
        <Canvas {...props} />
      </ReactFlowProvider>
    </div>
  );
}
