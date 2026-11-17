/**
 * 节点外壳：形状描边 + 四向 Handle + 状态类名。
 *
 * 描边走 SVG，状态走 CSS 变量（`--topo-node-stroke/dash/width`），
 * 这样「颜色 + 线型」两条通道由同一个 `.topo-state-*` 类驱动，不会某天只改了一半。
 */

import type { ReactElement, ReactNode } from 'react';
import { Handle, Position } from '@xyflow/react';
import type { NodeShape, Size } from '../shape';
import { SIDES, shapeGeometry } from '../shape';

const POSITION: Record<(typeof SIDES)[number], Position> = {
  top: Position.Top,
  right: Position.Right,
  bottom: Position.Bottom,
  left: Position.Left,
};

export function NodeShell({
  shape,
  state,
  size,
  handleType,
  ariaLabel,
  selected = false,
  children,
}: {
  shape: NodeShape;
  state: string;
  size: Size;
  handleType: 'source' | 'target';
  ariaLabel: string;
  selected?: boolean;
  children: ReactNode;
}): ReactElement {
  const geometry = shapeGeometry(shape, size);

  return (
    <div
      className={[
        'topo-node',
        `topo-shape-${shape}`,
        `topo-state-${state}`,
        selected ? 'topo-node-selected' : '',
      ]
        .filter(Boolean)
        .join(' ')}
      style={{ width: size.width, height: size.height }}
      aria-label={ariaLabel}
      role="group"
    >
      <svg
        className="topo-node-shape"
        width={size.width}
        height={size.height}
        viewBox={`0 0 ${size.width} ${size.height}`}
        aria-hidden="true"
        focusable="false"
      >
        <path className="topo-node-outline" d={geometry.outline} />
        {geometry.accent ? <path className="topo-node-outline topo-node-accent" d={geometry.accent} /> : null}
      </svg>

      <div className="topo-node-content">{children}</div>

      {SIDES.map((side) => (
        <Handle
          key={side}
          id={side}
          type={handleType}
          position={POSITION[side]}
          className="topo-handle"
          isConnectable={false}
        />
      ))}
    </div>
  );
}
