# admin-ui

Vade 管理面前端。React + TypeScript + Vite，**不引 UI 框架**，只用原生 CSS 变量。
同一份 `dist/` 有两个去处：

- core 静态托管 `https://<host>:8444/admin` —— **系统浏览器，只读**；
- 打进 Vade App 包，WebView 以 `app://admin/` 加载（Android `file:///android_asset/admin/`）—— **全功能**。

## 命令

```bash
npm ci
npm run dev        # http://127.0.0.1:5173，/admin/api 代理到 https://127.0.0.1:8444
npm run build      # tsc --noEmit && vite build；产物在 dist/，提交进仓库
npm run typecheck
```

CI 会跑 `npm ci && npm run build`，然后校验 `git diff --exit-code -- admin-ui/dist`：
**改了源码就必须提交重新构建的 dist**，否则流水线红。

## 第一批范围（C5a / T5）

| 路由 | 状态 | 说明 |
|---|---|---|
| `/login` | 已实现 | 表单形态随运行形态变化；固定显示 core 身份指纹 `fp8` |
| `/nodes` | 已实现 | 模块/实例/状态/地址/版本/RTT/最后心跳/工具数；停用·启用·吊销·断开 |
| `/setup` | 占位 | 只写清闸门顺序，**不渲染二维码**（先过闸、再给二维码） |
| `/audit` | 占位 | 留给下一批 |
| `/settings` | 占位 | 留给下一批 |

`/skills`、`/memory`、`/topology` 尚未注册：`/topology` 要等 C5b 的 canvas 模块合并后再
`lazy(() => import('../canvas'))`，现在注册会让构建失败。

## 路由

用 hash 路由（`#/nodes`）。原因：同一份 `dist` 既要被 core 托管在 `/admin/`，也要被 App 以
自定义 scheme 打开，路径路由在 `app://` 下不可靠；hash 路由不要求服务端 rewrite。
入口 `src/app/router.ts`，只有几十行，没有引路由库。

## 运行形态（`webview` / `browser`）的判定

判定顺序（`src/app/host.ts`）：

1. 宿主注入的 `window.__AC_HOST__ = {mode, fp8, deviceLabel, session?}`（App 原生层
   `evaluateJavaScript` 注入，不受页面 CSP 限制）；
2. core 改写 `index.html` 时写入的 `<meta name="vade-mode">` / `<meta name="vade-fp8">`
   —— CSP 是 `script-src 'self'`，**不能**用 inline script 注入；
3. 有 `window.__AC_BRIDGE__` → `webview`；
4. 都没有 → `browser`（只读是安全侧默认；服务端仍会对写请求回 `403 READ_ONLY_SESSION`）。

**不要**从 URL 参数读 mode —— 链接可以被社工发送。

### 两种形态的差别（设计决定，不是能力缺陷）

| 形态 | 能做什么 | UI 怎么呈现 |
|---|---|---|
| `webview` | 全部 | 密码由**原生弹窗**输入；页面不画密码框，只通过桥拿 `access_token`（只存内存） |
| `browser` | 只读 | 密码框 + 常驻提示「当前为只读模式；要修改配置请用 Vade App 打开」；**写按钮一律不渲染** |

`refresh_token` 永远不进页面；`localStorage` / `sessionStorage` 一概不写。

原生桥的接口约定见 `src/app/nativeBridge.ts`（本批只留接口与注释，原生侧由 U1 实现）。

## 安全约束（改动时别破坏）

- 页面内**没有** `dangerouslySetInnerHTML`；所有节点数据按文本插值。
- 401 统一回登录页；登录失败文案统一「密码或设备不正确」，不区分密码错 / 设备未登记 / 签名错。
- 确认弹窗必须写影响面（几个工具、哪些工具、几个会话），不是「确定吗」。
- 破坏性操作（停用、吊销）在确认框里就是危险色。
- 窄屏（<768px）可用：操作按钮在卡片底部，不靠 hover；没有 `hidden md:block`。

## 用到的接口

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/admin/api/login/challenge` | 取 challenge（浏览器登录的前置） |
| POST | `/admin/api/login` | 密码登录；browser 形态只 Set-Cookie |
| POST | `/admin/api/logout` | 退出 |
| GET | `/admin/api/health` | 探活；若 core 在此下发 `fp8` 就用它 |
| GET | `/admin/api/nodes` | 节点列表（白名单字段） |
| PATCH | `/admin/api/nodes/{id}` | 停用 / 启用（body `{enabled}`） |
| POST | `/admin/api/nodes/{id}/disconnect` | 断开 |
| POST | `/admin/api/nodes/{id}/revoke` | 吊销（破坏性） |

契约：`schema/admin-api.yaml`（C-08）。节点 id 含 `#`，请求前必须 `encodeURIComponent`。

## 已知缺口（不在本批范围，已记录在 `batch-results/u1-adminui.md`）

1. **core 身份指纹没有专门的公开端点**：`/admin/setup` 只在未初始化时返回 `fp8`，
   `/admin/api/health` 契约里不含 `fp8`。本批的取法是「宿主注入 / meta / health 的 fp8」，
   都没有时登录页如实显示「未提供」。建议 core 在 `/admin/api/health` 增加 `fp8`（回环）
   或托管 `/admin` 时注入 `vade-fp8` meta。
2. **节点视图没有 `enabled` 字段**：`PATCH {enabled}` 能改，但 `GET /admin/api/nodes` 不返回
   当前启用状态。本页用「本次会话内已操作」的本地标记显示「已停用」，刷新后回到 core 的
   视图（在线/离线）。要准确显示，需要把 `enabled` 加进 §7.5 白名单。
3. `/setup`、`/audit`、`/settings` 是占位页；`/skills`、`/memory`、`/topology` 未注册。
