/**
 * 节点几何：形状决定 path，path 决定描边怎么画。
 *
 * 用 SVG 而不是 CSS border-radius / clip-path，原因有两个：
 * 1. 六边形与圆柱的描边必须跟着形状走，clip-path 只能裁掉边框的一半；
 * 2. 状态的「颜色 + 线型」双通道要作用在描边上，`stroke-dasharray` 需要真描边。
 */

export type NodeShape = 'core' | 'square' | 'hexagon' | 'cylinder' | 'bridge' | 'rounded' | 'invite';

export interface Size {
  width: number;
  height: number;
}

/** 每种形状的固定尺寸：布局与命中区域都按这个算，避免量 DOM 抖动。 */
export const SHAPE_SIZE: Record<NodeShape, Size> = {
  core: { width: 148, height: 148 },
  square: { width: 180, height: 84 },
  hexagon: { width: 200, height: 86 },
  cylinder: { width: 184, height: 90 },
  bridge: { width: 190, height: 90 },
  rounded: { width: 180, height: 80 },
  invite: { width: 162, height: 74 },
};

export type Side = 'top' | 'right' | 'bottom' | 'left';

export const SIDES: readonly Side[] = ['top', 'right', 'bottom', 'left'] as const;

export function oppositeSide(side: Side): Side {
  switch (side) {
    case 'top':
      return 'bottom';
    case 'right':
      return 'left';
    case 'bottom':
      return 'top';
    case 'left':
      return 'right';
  }
}

/**
 * 从 core 中心指向节点中心，取最近的一侧。辐射布局下人眼期待线从朝向 core 的那条边进出。
 * 用 |dx| vs |dy| 比较即可：斜向时选主轴，视觉上仍连续。
 */
export function sideFor(dx: number, dy: number): Side {
  if (Math.abs(dx) >= Math.abs(dy)) return dx >= 0 ? 'right' : 'left';
  return dy >= 0 ? 'bottom' : 'top';
}

function n(value: number): string {
  return Number.isInteger(value) ? `${value}` : value.toFixed(2);
}

function roundedRect(w: number, h: number, r: number, inset = 1): string {
  const x0 = inset;
  const y0 = inset;
  const x1 = w - inset;
  const y1 = h - inset;
  const radius = Math.min(r, (x1 - x0) / 2, (y1 - y0) / 2);
  return [
    `M ${n(x0 + radius)} ${n(y0)}`,
    `H ${n(x1 - radius)}`,
    `A ${n(radius)} ${n(radius)} 0 0 1 ${n(x1)} ${n(y0 + radius)}`,
    `V ${n(y1 - radius)}`,
    `A ${n(radius)} ${n(radius)} 0 0 1 ${n(x1 - radius)} ${n(y1)}`,
    `H ${n(x0 + radius)}`,
    `A ${n(radius)} ${n(radius)} 0 0 1 ${n(x0)} ${n(y1 - radius)}`,
    `V ${n(y0 + radius)}`,
    `A ${n(radius)} ${n(radius)} 0 0 1 ${n(x0 + radius)} ${n(y0)}`,
    'Z',
  ].join(' ');
}

/** 平顶六边形：左右各切一个 17% 的斜角。 */
function hexagon(w: number, h: number, inset = 1): string {
  const cut = Math.round(w * 0.16);
  const x0 = inset;
  const x1 = w - inset;
  const y0 = inset;
  const y1 = h - inset;
  const midY = h / 2;
  return [
    `M ${n(x0 + cut)} ${n(y0)}`,
    `H ${n(x1 - cut)}`,
    `L ${n(x1)} ${n(midY)}`,
    `L ${n(x1 - cut)} ${n(y1)}`,
    `H ${n(x0 + cut)}`,
    `L ${n(x0)} ${n(midY)}`,
    'Z',
  ].join(' ');
}

export interface ShapeGeometry {
  /** 主轮廓（所有形状都有）。 */
  outline: string;
  /** 圆柱顶盖那条弧，或者双框的内框；其它形状没有。 */
  accent: string | null;
}

export function shapeGeometry(shape: NodeShape, size: Size): ShapeGeometry {
  const { width: w, height: h } = size;
  switch (shape) {
    case 'core':
      return { outline: roundedRect(w, h, 22), accent: null };
    case 'square':
      return { outline: roundedRect(w, h, 3), accent: null };
    case 'rounded':
      return { outline: roundedRect(w, h, 16), accent: null };
    case 'invite':
      return { outline: roundedRect(w, h, 12), accent: null };
    case 'hexagon':
      return { outline: hexagon(w, h), accent: null };
    case 'cylinder': {
      const ry = 9;
      const body = [
        `M 1 ${n(ry)}`,
        `A ${n(w / 2 - 1)} ${n(ry)} 0 0 1 ${n(w - 1)} ${n(ry)}`,
        `V ${n(h - ry)}`,
        `A ${n(w / 2 - 1)} ${n(ry)} 0 0 1 1 ${n(h - ry)}`,
        'Z',
      ].join(' ');
      // 顶盖的后半圈：向下凹，形成「圆柱开口」的错觉。
      const rim = `M 1 ${n(ry)} A ${n(w / 2 - 1)} ${n(ry)} 0 0 0 ${n(w - 1)} ${n(ry)}`;
      return { outline: body, accent: rim };
    }
    case 'bridge':
      // 双框 = 外框 + 内框（内缩 5px），表示「这是个转发到上游 MCP 的桥」。
      return { outline: roundedRect(w, h, 10, 1), accent: roundedRect(w, h, 8, 6) };
  }
}

/** 形状决定左右内边距：六边形与圆柱的可用文字宽度更窄。 */
export function contentPadding(shape: NodeShape): { x: number; y: number } {
  switch (shape) {
    case 'hexagon':
      return { x: 34, y: 12 };
    case 'cylinder':
      return { x: 16, y: 20 };
    case 'bridge':
      return { x: 16, y: 14 };
    case 'core':
      return { x: 16, y: 16 };
    default:
      return { x: 14, y: 11 };
  }
}
