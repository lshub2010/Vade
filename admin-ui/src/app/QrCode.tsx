/**
 * 二维码：一个 `<svg>` + 一条 path，不引 UI 框架、不写 `innerHTML`。
 *
 * 用 `qrcode-generator`（MIT，单文件，无依赖）拿模块矩阵，颜色自己定：
 * 白底深色模块是扫码器的硬要求 —— 深色主题里直接把二维码画成浅色是扫不出来的。
 * 静区（quiet zone）留在 viewBox 里，窄屏放大时不会贴边。
 */

import { useMemo } from 'react';
import type { ReactElement } from 'react';
import qrcode from 'qrcode-generator';

export interface QrCodeProps {
  /** 二维码内容（这里是 `bind_url`，含一次性绑定票）。 */
  value: string;
  /** 给读屏软件的说明；不渲染成可见文字。 */
  label: string;
  /** 静区宽度，单位是模块；规范要求 ≥4。 */
  margin?: number;
}

interface QrGeometry {
  /** 含静区的边长（模块数）。 */
  side: number;
  /** 每个深色模块一个小方块，拼成一条 path。 */
  path: string;
}

function geometry(value: string, margin: number): QrGeometry | null {
  if (value.length === 0) return null;
  try {
    // typeNumber 0 = 按内容自动选版本；M 级纠错在屏幕扫码与打印之间比较平衡。
    const code = qrcode(0, 'M');
    code.addData(value);
    code.make();
    const count = code.getModuleCount();
    const side = count + margin * 2;
    const parts: string[] = [];
    for (let row = 0; row < count; row += 1) {
      for (let col = 0; col < count; col += 1) {
        if (code.isDark(row, col)) parts.push(`M${col + margin} ${row + margin}h1v1h-1z`);
      }
    }
    return { side, path: parts.join('') };
  } catch {
    // 内容超长（40 版也放不下）时 qrcode-generator 直接 throw；页面降级成文字链接。
    return null;
  }
}

export function QrCode({ value, label, margin = 4 }: QrCodeProps): ReactElement {
  const shape = useMemo(() => geometry(value, margin), [value, margin]);

  if (!shape) {
    return (
      <p className="qr-fallback" role="status">
        二维码生成失败。请改用 App 直接打开下面的绑定链接。
      </p>
    );
  }

  return (
    <svg
      className="qr"
      viewBox={`0 0 ${shape.side} ${shape.side}`}
      role="img"
      aria-label={label}
      shapeRendering="crispEdges"
      focusable="false"
      xmlns="http://www.w3.org/2000/svg"
    >
      <rect width={shape.side} height={shape.side} fill="#ffffff" />
      <path d={shape.path} fill="#0b0e14" />
    </svg>
  );
}
