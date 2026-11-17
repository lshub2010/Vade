# v1-canvas 批次结果（拓扑画布：边不渲染的排查与修复）

## 结论先说

画布上「一条边都没有」是两层问题叠出来的，两处都已修：

1. **边的状态类没挂到路径上** —— `SessionEdge` 只加了 `topo-edge topo-edge-${state}`，而颜色/线型来自 `.topo-state-*` 定义的 `--topo-edge-color` / `--topo-edge-dash`。CSS 变量只能从自身或祖先继承，图的 `<path>` 这两层都没有 → 全部落到 `var(--border-strong)` 兜底：**`rgb(52,66,92)`、`1.5px`、实线**。在 `#0b1017` 的画布底色上，9 条边全在 DOM 里但肉眼几乎看不见，读起来就是「没有边」。（图例那几行是 `topo-state-*` 挂在 `<li>` 上，所以图例一直是对的 —— 这就是为什么画布和图例对不上。）
2. **Handle 尺寸写成了 1px** —— `@xyflow/system` 的 `getHandleBounds()` 用 `offsetWidth/offsetHeight`（整数、布局值）取锚点尺寸，`1px` + `transform` 在缩放画布下会退化成 0×0 的锚点，是「节点都在、边全没」的经典成因。改成 React Flow 自己的默认可测量尺寸 `6×6`（透明背景 + `opacity: 0` 来隐藏），不再靠改尺寸隐藏。

另外补了一处**可观测性**：RF 默认的 `onError` 是 `createDevWarn(...)`，生产构建里被裁成空函数，边被静默丢弃（错误码 008 = source/target handle 没找到）时控制台是干净的。现在 `ReactFlow` 显式传了 `onError`，打到 `console.error('[topo] react-flow 008: ...')`。

### 关于「`.react-flow__edge` 数量 0」

这一条**在本机复现不出来**：改动前的树上（`topo-handle` 仍是 1px、`isConnectable={false}` 仍在），同样的 dev preview 在 6 种视口、0.8s/1.2s/2s/3.5s 四个采样点、各重复 2 次的冷加载里，`.react-flow__edge` 恒为 **9**。所以「0 条」要么来自更早的模块状态（`FlowCanvas.tsx` 的 mtime 是 07:08，vite 是 07:10 重启的，都晚于那次观察），要么是查询时机/选择器问题。
**能复现并且已经修掉的是那个可观测症状**：改之前 9 条边的计算样式全是 `rgb(52,66,92) | none`（见下表 before 列），即「DOM 有、眼睛没有」。这说明即便边数不是 0，用户看到的也确实是「没有边」。

## 改动

| 文件 | 内容 |
|---|---|
| `admin-ui/src/canvas/edges/SessionEdge.tsx` | 路径补 `topo-state-${state}`：`<path class="react-flow__edge-path topo-edge topo-edge-online topo-state-online">`。这是本轮的主修复。 |
| `admin-ui/src/canvas/styles/topology.css` | `.topo-handle` 从 `1px / min-0` 改成 `6×6 / min-6`，仍然透明 + `opacity: 0` + `pointer-events: none`（与 RF 默认 `pointer-events: none` 一致，不给画布加不可见的点击区）。注释里写清「隐藏靠透明，不要靠尺寸」。 |
| `admin-ui/src/canvas/FlowCanvas.tsx` | 新增模块级 `reportFlowError` 并传给 `ReactFlow onError`；其余结构未动（`NODE_TYPES` / `EDGE_TYPES` / `DEFAULT_EDGE_OPTIONS` 本来就是模块级常量，引用稳定，已核对）。 |
| `admin-ui/topo-preview.html`、`admin-ui/src/canvas/__preview.tsx` | 自检页用完删除（按要求不进仓库）。 |
| `admin-ui/dist/**` | `npm run build` 的真实产物（`tsc --noEmit` + `vite build`），已随提交带上。 |

## 验证（全部是浏览器/CDP 实测，不是「改完应该就好了」）

工具：headless Chromium 145 + 原生 CDP（`Page.navigate` → 等 3.2s → `Runtime.evaluate` 数 DOM + 读 `getComputedStyle`）。因为要读生产构建的 `#/topology` 路由，用一个本地静态服务器托管 `dist/` 并桩掉 `GET /admin/api/nodes`、`/admin/api/health`（浏览器模式下 200 即 `phase=authed`），没有改仓库里的任何代码来迁就验证。

**修复前（dist 构建，1440×900）** —— 9 条边全在，但：

| 状态 | stroke | dasharray |
|---|---|---|
| 全部 9 条 | `rgb(52, 66, 92)` | `none` |

**修复后（同一份 dist，1440×900）**：

| 状态 | 条数 | stroke | dasharray |
|---|---|---|---|
| online | 4 | `rgb(62, 207, 142)` | `none` |
| degraded | 2 | `rgb(230, 180, 90)` | `9px, 5px` |
| offline | 1 | `rgb(151, 163, 186)` | `5px, 5px` |
| rejected | 1 | `rgb(255, 123, 123)` | `1.5px, 4.5px` |
| pending | 1 | `rgb(100, 194, 238)` | `1.5px, 6px` |

其他实测数字：

- `.react-flow__edge` = **9**，`path.topo-edge` = **9**，`getTotalLength() < 2` 的退化路径 = **0**；
- `.react-flow__node` = 11（core + 9 个模块/设备/待接入 + 1 个分组框）；
- `path.topo-edge-flow` = 6（4 online + 2 degraded，与 `flow` 规则一致），两种流动色 `rgb(169,245,205)` / `rgb(247,221,166)`；
- 9 条边都带 `marker-end`，`<marker>` 定义 3 个（arrow closed 分色）；
- 视口 1440×900 / 1200×760 / 820×620 三个尺寸结果一致；dev preview 侧另测了 1400×900、1280×720、1024×600、800×600、600×480、420×380、320×300、2200×1400，边数都是 9、非退化路径都是 9（缩放从 0.32 到 1.06）；
- 冷加载重复采样（0.8/1.2/2/3.5s × 2 次）边数稳定为 9，没有「先 0 后出现」的竞态；
- 采集 `console.error` / `console.warn` / `window.onerror` / `unhandledrejection`：**0 条**（说明没有 008 之类的 handle 解析失败）；
- 截图：`#/topology` 在 1440×900 下 9 条边从 core 四向散出，绿实线 / 黄长虚线 / 灰短虚线 / 红点线 / 蓝点线可分辨，与图例一致。

`npm run build` 真跑通过（`tsc --noEmit` 无错，`vite build` ✓ built in 1.16s；只有 rolldown 对 `react-compiler-runtime` 的 `"use no memo"` 指令保留提示，属依赖自带的已知告警）。

## 没做 / 留给下一批

- 拖拽、SSE 增量、属性面板仍是 T3/T4/T5 的 TODO，本轮没碰（`nodesDraggable={false}`）。
- `isConnectable={false}` 保留：画布是只读的（`nodesConnectable={false}`），这个 prop 只影响连线交互，不影响边的锚点解析 —— 实测开关它边数都是 9，所以没有理由为了「看起来像修好了」去动语义正确的东西。
- 边状态色仍只由 `.topo-state-*` 一处定义（这是文件头写明的约束）；如果以后要新增状态，记得 `SessionEdge` 的 `topo-state-${state}` 会跟着 `data.state` 走，不需要改组件。
