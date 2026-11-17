/**
 * 会话边：core → 节点，一条边一个连接。
 *
 * 状态是双通道的：底色（绿/黄/灰/红/蓝）+ 线型（实线/长虚线/短虚线/点线）。
 * `flow` 为真时叠一条流动虚线（`stroke-dashoffset` 动画），只是「正在跑」的提示——
 * 灰度截图里，即使动画被静态化，底线的 dasharray 仍能区分状态。
 *
 * `reason` 只在 rejected 上有值（来自 SSE 的 edge.state），这里用 `<title>`
 * 做悬浮提示：不额外画气泡，避免一条边把布局顶开。
 */

import type { ReactElement } from 'react';
import { BaseEdge, getBezierPath } from '@xyflow/react';
import type { EdgeProps } from '@xyflow/react';
import type { TopoEdge } from '../graph';

export function SessionEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  markerStart,
  markerEnd,
  data,
}: EdgeProps<TopoEdge>): ReactElement {
  const [path] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
    curvature: 0.32,
  });

  const state = data?.state ?? 'offline';
  const flow = data?.flow === true;
  // `topo-state-*` 必须挂在路径自己身上：它给出的 --topo-edge-color / --topo-edge-dash
  // 是 CSS 变量，只能从自身或祖先继承。图例那行是挂在 <li> 上的，这里没有这层祖先，
  // 少写一个类名的表现是「所有边都是同一条灰实线」——在深色画布上基本看不见。
  const stateClass = `topo-state-${state}`;

  return (
    <>
      {data?.reason ? <title>{data.reason}</title> : null}
      <BaseEdge
        id={id}
        path={path}
        className={`topo-edge topo-edge-${state} ${stateClass}`}
        markerStart={markerStart}
        markerEnd={markerEnd}
      />
      {flow ? (
        <path
          className={`topo-edge-flow topo-edge-flow-${state} ${stateClass}`}
          d={path}
          fill="none"
          pointerEvents="none"
          aria-hidden="true"
        />
      ) : null}
    </>
  );
}
