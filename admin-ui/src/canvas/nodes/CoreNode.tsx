/**
 * core 节点：深色实心、居中、不可拖（拖出视野就找不回来了）。
 *
 * 它是画布的锚点，不来自 `/nodes`：注册表里没有 core 自己这一行。
 * 上面只放注册表规模，不放任何身份信息 —— 指纹在页头，不在画布（少一处泄露面）。
 */

import type { ReactElement } from 'react';
import type { NodeProps } from '@xyflow/react';
import type { TopoFlowNode } from '../graph';
import { NodeShell } from './NodeShell';

export function CoreNode({ data }: NodeProps<TopoFlowNode>): ReactElement {
  const totals = data.totals ?? { nodes: 0, online: 0 };

  return (
    <NodeShell
      shape="core"
      state={data.state}
      size={data.size}
      handleType="source"
      ariaLabel={`core，注册表 ${totals.nodes} 个节点，其中 ${totals.online} 个在线`}
    >
      <div className="topo-core-body">
        <span className="topo-core-mark" aria-hidden="true">
          V
        </span>
        <span className="topo-core-name">core</span>
        <span className="topo-core-sub">
          {totals.online}/{totals.nodes} 在线
        </span>
        <span className="topo-core-proto">vade/v1</span>
      </div>
    </NodeShell>
  );
}
