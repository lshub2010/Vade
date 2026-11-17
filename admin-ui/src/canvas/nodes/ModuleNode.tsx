/**
 * 模块 / 设备节点。
 *
 * 视觉编码（design.md §7.7）：
 * - 工具=方形、模型=六边形、记忆=圆柱、桥=双框、设备=圆角 —— 由 `shape` 决定，见 graph.ts
 * - 状态：颜色 + 线型（`.topo-state-*`），另有状态文字，三通道冗余
 * - 加密：右上角锁标，实心=端到端 / 空心=仅 TLS / 无=后端没登记
 * - 地址：副标题，等宽，中间省略；完整值只在 `title` 里
 * - 工具：只显示 `tool_count` 徽标，**不列工具名**（D35）
 */

import type { ReactElement } from 'react';
import type { NodeProps } from '@xyflow/react';
import { encryptionMark, stateLabel, toolBadge } from '../format';
import { KindGlyph, LockMark } from '../icons';
import type { TopoFlowNode } from '../graph';
import { contentPadding } from '../shape';
import { NodeShell } from './NodeShell';

export function ModuleNode({ data, selected }: NodeProps<TopoFlowNode>): ReactElement {
  const padding = contentPadding(data.shape);
  const lock = encryptionMark(data.authMode);
  const endpoint = data.subtitle;

  const aria = [
    data.title,
    stateLabel(data.state),
    lock.kind === 'none' ? '加密方式未登记' : lock.short,
    data.toolCount > 0 ? toolBadge(data.toolCount) : '无工具',
    endpoint ?? '地址未登记',
  ].join('，');

  return (
    <NodeShell
      shape={data.shape}
      state={data.state}
      size={data.size}
      handleType="target"
      ariaLabel={aria}
      selected={selected}
    >
      <div
        className="topo-node-body"
        style={{ padding: `${padding.y}px ${padding.x}px` }}
      >
        <div className="topo-node-head">
          <KindGlyph shape={data.shape} />
          <span className="topo-node-title" title={data.moduleId}>
            {data.title}
          </span>
          {data.instanceCount > 1 ? (
            <span
              className="topo-node-instances"
              title={`同一 module_id 有 ${data.instanceCount} 个实例`}
            >
              ×{data.instanceCount}
            </span>
          ) : null}
        </div>

        <div
          className={endpoint ? 'topo-node-addr' : 'topo-node-addr topo-node-addr-missing'}
          title={data.subtitleFull ?? undefined}
        >
          {endpoint ?? '地址未登记'}
        </div>

        <div className="topo-node-meta">
          <span className={`topo-node-state topo-node-state-${data.state}`}>{stateLabel(data.state)}</span>
          {data.toolCount > 0 ? <span className="topo-node-tools">{toolBadge(data.toolCount)}</span> : null}
          {typeof data.rttMs === 'number' ? <span className="topo-node-rtt">{Math.round(data.rttMs)} ms</span> : null}
          {data.kind === 'device' ? <span className="topo-node-kind">设备</span> : null}
        </div>
      </div>

      {lock.kind === 'none' ? null : <LockMark kind={lock.kind} title={lock.title} />}
    </NodeShell>
  );
}
