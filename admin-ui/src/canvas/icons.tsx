/** 画布上的小图标。全部内联 SVG：WebView 里没有图标字体，也不允许外链资源。 */

import type { ReactElement } from 'react';
import type { NodeShape } from './shape';

export function KindGlyph({ shape }: { shape: NodeShape }): ReactElement {
  const common = {
    className: 'topo-kind-glyph',
    viewBox: '0 0 12 12',
    width: 12,
    height: 12,
    'aria-hidden': true as const,
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.2,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
  };

  switch (shape) {
    case 'hexagon':
      return (
        <svg {...common}>
          <path d="M6 1.6 10.4 6 6 10.4 1.6 6Z" />
        </svg>
      );
    case 'cylinder':
      return (
        <svg {...common}>
          <ellipse cx="6" cy="3.2" rx="3.6" ry="1.4" />
          <path d="M2.4 3.2v5.6c0 .8 1.6 1.4 3.6 1.4s3.6-.6 3.6-1.4V3.2" />
        </svg>
      );
    case 'bridge':
      return (
        <svg {...common}>
          <rect x="1.5" y="2.5" width="9" height="7" rx="1.2" />
          <path d="M6 2.5v7" />
        </svg>
      );
    case 'rounded':
      return (
        <svg {...common}>
          <rect x="1.5" y="2.5" width="9" height="6" rx="1.4" />
          <path d="M4.2 10.6h3.6" />
        </svg>
      );
    case 'invite':
      return (
        <svg {...common}>
          <path d="M6 2.5v7M2.5 6h7" />
        </svg>
      );
    case 'core':
      return (
        <svg {...common}>
          <circle cx="6" cy="6" r="3.4" />
          <path d="M6 2.6v6.8M2.6 6h6.8" />
        </svg>
      );
    case 'square':
    default:
      return (
        <svg {...common}>
          <rect x="2" y="2" width="8" height="8" rx="1" />
          <path d="M4.4 4.6h3.2M4.4 7.4h3.2" />
        </svg>
      );
  }
}

/**
 * 锁标：实心 = 端到端（Noise IK）；空心 = 仅 TLS。
 *
 * 不用「锁 = 安全」这种含糊画法：两种锁的填充分明，灰度下也能看出差别，
 * 且 title 里写明是哪一种加密（FR-3.7）。
 */
export function LockMark({
  kind,
  title,
}: {
  kind: 'solid' | 'hollow';
  title: string;
}): ReactElement {
  return (
    <span className={`topo-lock topo-lock-${kind}`} title={title} aria-label={title} role="img">
      <svg viewBox="0 0 14 16" width="12" height="14" aria-hidden="true">
        <path
          className="topo-lock-shackle"
          d="M3.6 7V5.2a3.4 3.4 0 0 1 6.8 0V7"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
        />
        {kind === 'solid' ? (
          <rect
            className="topo-lock-body"
            x="2.2"
            y="7"
            width="9.6"
            height="7.4"
            rx="1.6"
            fill="currentColor"
          />
        ) : (
          <rect
            className="topo-lock-body"
            x="2.2"
            y="7"
            width="9.6"
            height="7.4"
            rx="1.6"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
          />
        )}
      </svg>
    </span>
  );
}
