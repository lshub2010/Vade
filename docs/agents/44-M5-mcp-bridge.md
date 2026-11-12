# M5 · `mcp-bridge` 模块

| 项 | 值 |
|---|---|
| Agent 代号 | `m5-mcp` |
| 波次 | W5 |
| 依赖 | C4 的 SDK v0.1（含 `app.replace_tools()`） |
| 目录所有权 | `modules/mcp_bridge/` |
| 出口 | 五道门 + 动态能力更新可用 + 单上游拆实例可用 + 上游未知 risk 一律按 high |

## 一、一句话任务

一个模块接进所有现成的 MCP server：启动时把上游工具全抓过来、改名、注册；上游工具变了就原子更新；**每个上游可拆成独立实例**，故障互不影响；未知风险的工具一律当高危。

> 知识库的推荐路径就是这条路（D37/D39）。`docs/design.md` §2.7.5 有完整说明：上游 MCP server **不是** Vade 模块，它不握手、不注册、不能被别的模块路由——只是 bridge 的外部依赖。所以这不违反 FR-5.7。

## 二、必读

- `docs/design.md` §15.2（mcp-bridge 规格）、§2.3（manifest 校验）、§2.5（RPC）
- `docs/requirements.md` A14、A16、A17（动态能力更新）、D27
- `sdk/PYTHON_SDK.md`、`docs/agents/01-contracts.md` §二/§四

## 三、目录

```
modules/mcp_bridge/
├── manifest.json                 # 启动时若无上游，可为零工具版本
├── config.example.toml
├── src/mcp_bridge/
│   ├── tools.py                  # 纯逻辑：命名、风险、结果转换（零 SDK import）
│   ├── upstream.py               # stdio / HTTP 两种传输的 MCP 客户端
│   ├── manager.py                # 发现、缓存、变更检测
│   └── server.py
├── tests/fake_mcp_server.py
├── Dockerfile
└── README.md
```

## 四、配置

两种启动模式，**默认拆实例**（D39）。注意：**不改 SDK 的 CLI 契约**（`app.run()` 只认 `--standalone` / `--core`），拆实例靠环境变量：

```bash
# 推荐：一个上游一个实例，故障隔离，拓扑上各自成节点
# instance_id 自动 = mcp-bridge-<upstream>
BRIDGE_UPSTREAM=kb  python -m mcp_bridge --core wss://core:8443
BRIDGE_UPSTREAM=gh  python -m mcp_bridge --core wss://core:8443
BRIDGE_UPSTREAM=all python -m mcp_bridge --core wss://core:8443   # 兼容模式：全部上游塞一个实例
```

```toml
[bridge]
split_instances = true            # false → 全部塞进一个实例（等价 BRIDGE_UPSTREAM=all）
instance_prefix = "mcp-bridge"    # 最终 instance_id = f"{prefix}-{upstream}"，且 upstream 里的 "_" 替换为 "-"
# 原因：上游名允许下划线（^[a-z][a-z0-9_]{0,15}$），instance_id 不允许（^[a-z][a-z0-9-]*$）
# my_kb → mcp-bridge-my-kb；转换后若重名（my_kb 与 my-kb 并存）→ 启动报错，不静默改名

[[servers]]
name = "kb"                       # 必填，唯一，^[a-z][a-z0-9_]{0,15}$
transport = "http"
url = "https://kb.internal/mcp"
headers_env = { Authorization = "KB_MCP_AUTH" }
risk_default = "high"             # 未知工具的风险
tool_risk = { recall = "low", remember = "medium", forget = "high", ingest = "high" }
```

- 凭据**只从环境变量读**，不写进 `manifest.json`、不进日志、不进 `extensions`。
- 上游连接失败：模块**仍要注册**（零工具 + warning），后台每 30s 重试一次。

## 五、工具命名与风险

上游工具名可能含 `-` `.` `/`，LLM 侧只允许 `[a-z0-9_]`：

```python
def mangle(server: str, tool: str) -> str:
    s = re.sub(r"[^a-z0-9_]", "_", tool.lower())
    name = f"s_{server}_{s}"[:64].rstrip("_")
    return name
```

- 冲突（截断或同名）→ 追加 `_2`、`_3`；仍冲突则跳过该工具并打 warning。
- 单模块 ≤ 64 个工具（manifest 校验的硬上限）；超出按「上游名排序」截断，README 写明，不静默。
- `risk`：`tool_risk` 显式声明 > 上游 annotation（若可信）> `risk_default`（默认 `high`）。
  **上游没声明风险就是 high**——宁可多弹一次审批，不可放过一次删除。
- `idempotent`：上游未声明时按 `false`（保守）。

## 六、调用代理

- `tools/call` → 映射回上游原名 → 转发，超时取 `min(manifest.timeout_ms, 上游 timeout)`。
- 结果转换：MCP 的 `content[]`（text/image/resource）→
  - text 累加为 `text`；
  - image/resource 只返回元数据（`type, mime, size`），**不内联 base64**（会撑爆上下文）；
  - 总大小上限 2MB，超限截断 + `truncated: true`。
- 上游错误 → 映射到 `schema/errors.json`；未知错误 → `INTERNAL`，`message` 带上游 code 但不带堆栈。
- 取消：上游若支持 `notifications/cancelled` 就发；不支持则标记结果作废。

## 七、多实例与上游可观测（D39）

拆实例后，每个上游是独立进程、独立 `instance_id`、独立 manifest。这带来三个必须处理的问题：

| 问题 | 做法 |
|---|---|
| 拓扑显示 | 每个实例是一个节点（`com.example.mcp-bridge/kb`）。画布上**不画上游**，上游是侧栏三层树：`bridge → 上游 server → 工具`（延续 D35：工具不进画布） |
| 健康度 | 单实例只服务一个上游，状态直接映射：上游连不上 → `degraded`（有旧工具表）或 `offline`（从未连上）。**不要**把多上游的失败率平均成一个数字 |
| 审计归因 | 工具名已带上游前缀（`s_kb_recall`），配合 `instance_id` 可追到具体上游。日志额外打 `upstream=<name>` |

- 拆实例时工具上限按**每实例 64** 计，不是全局 128——但全局上限仍生效，启动时若超了，core 拒绝后注册的那个实例并给出可读原因。
- 单实例模式下（`split_instances=false`），健康度按「任一 critical 上游挂 → `degraded`」，`critical` 由配置 `critical = true` 标注，默认 `false`。
- 禁止把上游 URL、凭据、上游工具原始 schema 写进 `module.status` 事件。

## 八、动态能力更新（D27）

```
启动 → 并发连所有上游 → tools/list → 合并去重 → app.replace_tools(specs)
                                                ↓
                                     SDK 发 module/capabilities.update
                                                ↓
                              core 全量校验 → 原子替换 / 拒绝保留旧表
```

- 触发时机：启动完成、上游发 `notifications/tools/list_changed`、每 5 分钟轮询一次（兜底）。
- **必须全量提交**，不做增量；失败时保留旧能力，模块继续用旧表服务。
- 更新后日志记 `cap_epoch` 与工具数变化（+3/-1），不记工具参数。
- 上游全部掉线时**不要**提交空表（会让模型突然失去全部工具）；保留旧表，返回 `UNAVAILABLE`。

## 九、验收（五道门）

```bash
cd modules/mcp_bridge

# 假上游（stdio，两个工具，其中一个名为 delete-all，无 risk 声明）
python tests/fake_mcp_server.py --stdio &
python -m mcp_bridge --standalone s_fake_echo --args '{"text":"hi"}'
python -m mcp_bridge --standalone s_fake_delete_all --args '{}'   # 应被 core 审批，standalone 下提示 high risk

pytest tests -q
```

必测：
1. 两个上游、工具名冲突 → 全部可调用且映射正确；
2. 上游工具名 `delete-all` → `s_fake_delete_all`，risk = high；
3. `tools/list_changed` → `capabilities.update` 被调用一次，工具数变化正确；
4. 提交非法 manifest（如工具数 65）→ core 拒绝，旧表仍可用；
5. 上游启动失败 → 模块仍注册（0 工具），30s 后重试成功；
6. 凭据不出现在 `manifest.json`、日志、`extensions`（grep 环境变量值，0 命中）；
7. 图片结果不内联 base64；
8. **拆实例**：`BRIDGE_UPSTREAM=kb python -m mcp_bridge --core ...` 与 `BRIDGE_UPSTREAM=gh python -m mcp_bridge --core ...` 各起一个进程（**不是 `--instance/--upstream`**，那会破坏 C4 冻结的 CLI），拓扑上是两个独立节点；`kill -9` kb 那个，gh 的调用不受影响，kb 节点变 `offline`；
9. **risk 映射**：`tool_risk = { recall = "low" }` 时 `s_kb_recall` 不触发审批，`forget` 触发审批；未映射的工具仍是 high。

## 十、禁止

- 禁止把上游凭据写进任何提交文件。
- 禁止「上游没声明 risk 就当 low」。
- 禁止增量更新能力（必须全量）。
- 禁止在上游掉线时提交空工具表。
- 禁止在 `tools.py` import SDK。
- 禁止把 MCP 的 `resources` 全文塞进工具结果。

## 十一、交付格式

按 §三，额外附：假上游的工具清单、一次 `capabilities.update` 的请求/响应原文（脱敏）、凭据 grep 结果、上游断线重连日志。
