import { defineConfig } from 'vite';
import type { Plugin } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * 生产构建注入 CSP（design.md §7.11 第 6 条）。
 * 只在 build 阶段注入：dev server 需要 inline 的 react-refresh 预导，注入会把它拦掉。
 * `connect-src` 必须写 `https://*:*`：core 的管理面在 `:8444`，而 CSP 里不带端口的
 * `https://*` 只匹配 443 —— 那样连页面自己的同源请求都会被拦（浏览器里表现为
 * `TypeError: Failed to fetch`，看起来像 core 掉线）。`'self'` 留给 `app://admin/`
 * 这种自定义 scheme 下的同源资源。仍然只允许 https：core 没有明文管理面。
 */
const CSP = [
  "default-src 'self'",
  "connect-src 'self' https://*:*",
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
