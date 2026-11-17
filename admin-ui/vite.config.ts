import { defineConfig } from 'vite';
import type { Plugin } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * 生产构建注入 CSP（design.md §7.11 第 6 条）。
 * 只在 build 阶段注入：dev server 需要 inline 的 react-refresh 预导，注入会把它拦掉。
 * `connect-src https://*` 是因为用户的 core 地址运行时才知道（app:// 与 file:// 页面也要能连）。
 */
const CSP = [
  "default-src 'self'",
  "connect-src https://*",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "font-src 'self'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
  "frame-ancestors 'none'",
].join('; ');

function cspPlugin(): Plugin {
  return {
    name: 'vade-admin-csp',
    apply: 'build',
    transformIndexHtml(html) {
      const tag = `<meta http-equiv="Content-Security-Policy" content="${CSP}" />`;
      // 必须排在模块脚本之前，否则 CSP 管不到它；charset 之后即可（仍在前 1024 字节内）。
      if (html.includes('<meta charset="UTF-8" />')) {
        return html.replace('<meta charset="UTF-8" />', `<meta charset="UTF-8" />\n    ${tag}`);
      }
      return html.replace('</head>', `    ${tag}\n  </head>`);
    },
  };
}

export default defineConfig({
  // 相对路径：同一份 dist 既要被 core 托管在 /admin/，也要被 App 以 app://admin/ 加载。
  base: './',
  plugins: [react(), cspPlugin()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2022',
    sourcemap: false,
    assetsInlineLimit: 0,
  },
  server: {
    port: 5173,
    proxy: {
      // 本地开发：把 /admin/api 转到本机 core 的管理面（自签证书需 secure:false）
      '/admin/api': {
        target: 'https://127.0.0.1:8444',
        changeOrigin: true,
        secure: false,
      },
    },
  },
});
