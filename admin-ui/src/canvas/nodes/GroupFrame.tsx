/**
 * 分组框：同一 `module_id` 的多实例（手机 + 电脑）套一个虚线外框。
 *
 * 它是纯装饰节点（不可选、不可拖、不参与连线）。用绝对坐标画包围盒，
 * 而不是 React Flow 的 parent 关系：parent 会把子节点坐标变成相对值，
 * 让「后端存的 x/y」多一层换算，拖动上线后更容易错位。
 * TODO(T3)：拖动落地时改成 parent 关系，让框跟着子节点走。
 */

import type { ReactElement } from 'react';
import type { NodeProps } from '@xyflow/react';
import type { TopoGroupNode } from '../graph';

export function GroupFrame({ data }: NodeProps<TopoGroupNode>): ReactElement {
  return (
    <div className="topo-group" style={{ width: data.width, height: data.height }}>
      <span className="topo-group-title" title={data.title}>
        {data.title}
        <span className="topo-group-count">×{data.count}</span>
      </span>
    </div>
  );
}
