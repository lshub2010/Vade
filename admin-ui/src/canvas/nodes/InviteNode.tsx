/**
 * 邀请位（`state === 'pending'` 的注册表行）。
 *
 * 编码：虚线框 + 低透明度 + 中心「+」。它不是一个已经连上的模块，
 * 所以既没有锁标也没有地址 —— 这两样一旦画上去就等于在骗人。
 *
 * 配对码弹窗（T6）与 `POST /invites` 属于下一批；这里只画位置。
 */

import type { ReactElement } from 'react';
import type { NodeProps } from '@xyflow/react';
import type { TopoFlowNode } from '../graph';
import { contentPadding } from '../shape';
import { NodeShell } from './NodeShell';

export function InviteNode({ data, selected }: NodeProps<TopoFlowNode>): ReactElement {
  const padding = contentPadding(data.shape);

  return (
    <NodeShell
      shape={data.shape}
      state={data.state}
      size={data.size}
      handleType="target"
      ariaLabel={`邀请位 ${data.title}，待接入`}
      selected={selected}
    >
      <div className="topo-invite-body" style={{ padding: `${padding.y}px ${padding.x}px` }}>
        <span className="topo-invite-plus" aria-hidden="true">
          +
        </span>
        <span className="topo-invite-caption" title={data.moduleId}>
          {data.title}
        </span>
        <span className="topo-invite-hint">待接入</span>
      </div>
    </NodeShell>
  );
}
