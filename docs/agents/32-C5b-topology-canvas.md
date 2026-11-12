# C5b · 拓扑画布

| 项 | 值 |
|---|---|
| Agent 代号 | `c5b-canvas` |
| 波次 | W5（C5a 交出 `schema/admin-api.yaml` 后即可开工，可先用 mock） |
| 依赖 | **C-08 `schema/admin-api.yaml` 冻结**、C5a 的 SSE 事件格式 |
| 目录所有权 | `admin-ui/src/canvas/`（其余前端目录只读） |
| 产出 | 以 core 为中心的拓扑画布：状态、地址、治理、邀请位 |
| 出口 | AC-17～AC-20 全过；200 节点可交互 |

## 一、一句话任务

把注册表画成一张**能读、能管、不能骗人**的图：谁连上来了、健不健康、走没走端到端加密，一眼看清；拖一下位置能存住；点一下能停用；放一个空位能等新模块接进来。

## 二、三条不可谈判的约束

1. **一个模块一个节点（D35）**。工具不进画布，只在右侧属性面板列出。`mcp-bridge` 有 40 个工具也只显示 `40 tools`。
2. **状态必须颜色 + 线型双通道**。色弱用户与灰度截图都要能读。
3. **任何情况下不显示密钥**。地址只到 `scheme://host:port`；锁标只表示加密强度，不显示指纹原文以外的任何凭据。

## 三、必读

- `docs/design.md` §7.2–§7.8（数据模型、状态、编辑三层、白名单、视觉编码）
- `docs/requirements.md` FR-9.5–9.15、A18/A19/A20、AC-17～AC-20、D35
- `schema/admin-api.yaml`（C5a）
- `docs/agents/31-C5a-governance-admin.md` §五 T4（SSE 格式）

## 四、任务分解

### T1 · 画布骨架（1 天）

**第一天先交占位**：`admin-ui/src/canvas/index.tsx` 导出 `TopologyPage`（先渲染「拓扑加载中」），让 C5a 的 app 壳能 `import('../canvas')` 而不破构建；之后在原地替换成真实实现。

```
admin-ui/src/canvas/
├── TopologyPage.tsx
├── FlowCanvas.tsx
├── nodes/ModuleNode.tsx  CoreNode.tsx  InviteNode.tsx
├── edges/SessionEdge.tsx
├── panel/NodePanel.tsx  GovernancePanel.tsx  InviteDialog.tsx
├── state/useTopology.ts     # SSE + 本地归并
├── state/useLayout.ts       # 拖拽 + rev 乐观锁
└── styles/topology.css
```

- 路由 `/topology`，挂在 C5a 的 app 壳里（不改 `src/app/`，只注册路由，注册点由 C5a 预留）。
- 依赖：`reactflow`（或 `@xyflow/react`），版本锁死；**不要**再引第二个图形库。
- 数据只从 API + SSE 来，前端不推导健康状态（后端说 `degraded` 就是 `degraded`）。

### T2 · 节点（1.5 天）

节点字段按 `docs/design.md` §7.2，前端只消费白名单字段。

| 维度 | 编码 |
|---|---|
| `kind=core` | 深色实心，居中，固定不可拖出视野 |
| `kind=module` + `capabilities` 含 tools | 方形 |
| `capabilities` 含 model | 六边形 |
| `capabilities` 含 memory | 圆柱（顶部椭圆） |
| module_id 含 `mcp` 或声明 bridge | 双框 |
| `kind=device` | 圆角矩形 |
| `kind=pending` | 虚线框 + 低透明度 + 中心「+」 |
| 状态 | 见下表 |
| 加密 | 右上角锁标：实心 = 端到端；空心 = 仅 TLS |
| 地址 | 副标题，等宽，**中间省略**（`wss://10.0.1.7:91…01`） |

状态视觉（**必须同时有颜色与线型**）：

| state | 节点描边 | 连线 |
|---|---|---|
| `online` | 绿色实线 | 绿色实线 + 流动虚线动画 |
| `degraded` | 黄色实线 | 黄色虚线 + 流动 |
| `offline` | 灰色虚线 | 灰色虚线，静态 |
| `rejected` | 红色点线 | 红色点线 + 悬浮显示 `reason` |
| `pending` | 蓝色点线 | 蓝色点线，箭头指向 core（表示"待接入"） |

- 节点上显示 `tool_count` 徽标（如 `4 tools`）；**不显示工具名**。
- 同一 `module_id` 的多个实例（手机 + 电脑）用**虚线外框**归组，框标题是 `module_id`。
- 双击节点 → 右侧面板展开工具列表（分页，每页 20）；`mcp-bridge` 工具名过长时中间省略，悬浮显示全名。

### T3 · 布局与并发（1.5 天）

- 拖拽结束才 `PATCH /admin/api/layout`（不要拖动中每帧发请求）。
- 请求带 `rev`；`409` → 用响应里的最新布局覆盖本地，弹 toast「另一台设备已修改，已为你刷新」，**不静默合并**。
- 自动排列：调用后端预计算的力导向坐标（`POST /admin/api/layout/auto`，若 C5a 未实现则前端用 `d3-force` 算一次并 PATCH，需在交付说明里注明）。
- 支持：缩放（滚轮/按钮）、平移（空白拖）、框选、`Fit View`、折叠分组。
- L1（位置/别名/分组）**不记审计**，只存布局；别名也走 `PATCH /admin/api/layout`（节点对象里带 `label`），**不要**走 `/nodes/{id}`——那条会写审计，L1 每拖一次就多一条审计，表会被拖垮。

### T4 · 实时（1 天）

- `EventSource('/admin/api/stream')`，浏览器自动带 `Last-Event-ID`；手动重连时带上最后 `seq`。
- 事件应用：`node.upsert` 覆盖同 id；`edge.state` 只改状态与 `since`；`node.remove` 删除。
- **按 seq 去重**：记录 `appliedSeq`，`<=` 的丢弃（重连会重叠）。
- 断线：指数退避 1s→30s，页面顶部显示「实时连接已断开，正在重连」，**不清空画布**。
- 1s 内反映上下线（FR-9.9）：从 `kill -9` 到画布变灰，端到端计时写进验收。

### T5 · L2 治理（1.5 天）

节点面板操作：

| 操作 | API | 确认要求 |
|---|---|---|
| 停用/启用 | `PATCH /nodes/{id}` | **必须**列出将移除的工具名与数量 |
| 吊销 | `POST /nodes/{id}/revoke` | 必须说明「该设备需重新配对」+ 影响工具数 |
| 断开重连 | `POST /nodes/{id}/disconnect` | 轻确认 |
| 覆盖并发模型 | `PATCH /nodes/{id}` | 说明可选值与当前值 |
| 覆盖 risk | `PATCH /nodes/{id}` | **只能上调**。下调请求会被 core 以 `PERMISSION_DENIED` 拒绝（防止把 HITL 关掉）；UI 上优先显示「上调」而不是可编辑下拉 |
| 生成配对码 | `POST /invites` | 一次性展示，关闭后不可再查看 |

- 确认弹窗**不能只写「确定吗」**，必须由后端返回 `impact{tool_count, tool_names[], sessions_affected}`，前端原样展示。
- 操作结果 toast：成功/失败 + 审计 id（便于追）。
- 面板里显示 `auth_mode`：`module_key` / `device_key` → 「端到端加密（Noise IK）」实心锁；`browser_session` → 「仅 TLS」空心锁；bridge 节点额外标「上游明文」。**文案不能含糊**，也不要写「已加密」这种没有主语的词（FR-3.7 / FR-8.25）。

### T6 · L2.5 邀请位（1 天）

- 「添加模块」按钮（**注意：不是「新建模块进程」**）→ 在画布中心投放 `pending` 节点 → 弹窗展示：
  - **8 位中英混合配对码**（`青松-A7K2M9`，2 汉字 + 6 位 base32），180s 倒计时。字号必须大到能隔着桌子念出来——这是它存在的意义
  - 二维码，payload：`vade://pair?h=<host>&p=<port>&c=<urlencoded-code>&fp=<core_pub_fp>`（**code 必须 urlencode**，汉字与 `-` 在部分扫码器里会被截断）
  - 一行可复制的 `python -m <module> --core wss://... --pair-code '青松-A7K2M9'`（**必须加引号**，否则 shell 会把中文当文件名补全）
  - 「换一个」按钮：重新生成（旧码立即作废，`revoke` + `create`，不是并存两个）
- 模块接入后：**同一个节点 id 原地转实心**（后端 `node.upsert` 带同一 `id`），不新增节点。
- 邀请过期（180s）→ 节点保持 `pending` 但标记「已过期」，可重新生成；弹窗里的码要**立即置灰并划掉**，不能只是变淡——变淡的码照样有人照着输。
- 界面上必须有文案：「这里只登记已接入的模块，不会帮你在远程机器上启动进程」。

### T7 · 性能与降级（1 天）

- 200 节点 / 500 边：拖拽时 P95 帧间隔 < 32ms（约 30fps），首屏渲染 < 1s。
- 超过 200 节点或 500 边：**自动切列表视图**（不渲染画布），顶部说明原因，提供「仍要渲染」的一次性开关（仅当前会话）。
- 窄屏 < 768px：只读列表（模块、状态、地址、工具数），无拖拽、无操作按钮。
- 节点数 > 50 时关闭流动动画（省 GPU），但保留颜色与线型差异。

### T8 · 验收脚本（0.5 天）

Playwright：

```bash
cd admin-ui && npx playwright test tests/topology.spec.ts
```

用例：
1. 拖节点 → 刷新 → 位置保持（AC-17）。
2. 两个浏览器上下文打开 → 一端拖动，另一端 409 后自动刷新（AC-17 后半）。
3. 停用模块 → 下一轮对话工具消失（AC-20，需配合 core e2e）。
4. `kill -9` 模块 → 1s 内连线变灰虚线，节点地址仍在（AC-18）。
5. 扫描页面 DOM 与网络响应：无密钥类字段（AC-19 的前端侧）。
6. 放邀请位 → 用假模块配对 → 同节点变实心（不新增节点）。
7. 201 个节点 → 自动列表视图。

## 五、验收

| 项 | 判据 |
|---|---|
| AC-17 | 布局持久化 + 多端一致 + 409 提示 |
| AC-18 | 断连 1s 内变灰，重启回绿 |
| AC-19 | 页面与 API 均 0 命中密钥模式 |
| AC-20 | 停用后下一轮工具表变化，无需重启 |
| 视觉 | 5 种状态截图各一张，灰度化后仍可区分（人工判定，附灰度图） |
| 性能 | 200 节点拖拽 P95 < 32ms（Performance 面板截图） |

## 六、禁止

- 禁止把工具画成节点、禁止画模块之间的边（有边就是 bug）。
- 禁止只靠颜色编码状态。
- 禁止在前端拼 `endpoint`（后端给什么显示什么，避免把 query 带出来）。
- 禁止提供「新建模块进程 / 启动容器」入口（L3 不做）。
- 禁止在拖动过程中每帧写后端。
- 禁止改 `admin-ui/src/app/` 与 `pages/`（C5a 所有），需要路由注册点就提 RFC。

## 七、交付格式

按 `00-orchestration.md` §三，额外附：
- 5 状态 + 灰度版截图
- 200 节点性能截图（Performance 面板）
- Playwright 原始输出
- 与 `schema/admin-api.yaml` 的字段对照表（证明没用到未定义字段）
