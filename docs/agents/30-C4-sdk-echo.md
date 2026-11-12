# C4 · Python SDK v0.1 + echo 模块（G1）

| 项 | 值 |
|---|---|
| Agent 代号 | `c4-sdk` |
| 波次 | W4 |
| 依赖 | C3 的 turn 生命周期可用、C1 的 wheel、`schema/*` |
| 目录所有权 | `sdk/python/`、`modules/echo/` |
| 产出 | `vade-sdk==0.1.x` + echo 模块 + `sdk/PYTHON_SDK.md` |
| 出口 | **G1**：端到端跑通 + SDK 接口冻结（6 个模块代理都照它写） |

## 一、一句话任务

把「写一个模块」变成 30 行代码的事：连 core、握手、注册、收调用、跑函数、回结果；不连 core 也能 `--standalone` 跑同一批函数。

## 二、为什么这一批的接口必须一次定对

后面 6 个模块代理都照这份 SDK 写。SDK 改一次，6 个目录全要改。**宁可这一批多花一天，也不要 6 个模块返工。**

冻结的东西：`ModuleApp` / `ToolSpec` / `ToolError` 三个类的公开面、CLI 参数、manifest 生成规则、日志字段。冻结后只能加可选参数。

## 三、必读

- `docs/design.md` §15（模块统一目录、规格、五道门）
- `docs/requirements.md` FR-5 全节、A14/A15/A16、NFR-4
- `docs/agents/01-contracts.md` 全节
- `protocol/v1/frames.md`（C1）

## 四、任务分解

### T1 · SDK 公开面（1 天）

```
sdk/python/
├── pyproject.toml
├── README.md
└── vade_sdk/
    ├── __init__.py      # 只导出 ModuleApp / ToolSpec / ToolError / run
    ├── app.py           # 生命周期、重连、心跳、drain
    ├── spec.py          # ToolSpec + 签名 -> JSON Schema
    ├── transport.py     # WS + protocol-core 绑定
    ├── standalone.py    # CLI 直调
    └── logging.py       # JSON lines
```

公开 API（**就这三个，不要多**）：

```python
from vade_sdk import ModuleApp, ToolSpec, ToolError

app = ModuleApp(
    module_id="com.example.echo",
    version="1.0.0",
    concurrency="pooled",        # exclusive / pooled / mirrored
    display_name="回声模块",      # 可选
    priority=0,                  # 可选，model 模块选择用（M6）
    capabilities=["tools"],      # 能力类型声明，默认 ["tools"]
)

app.add_tool(ToolSpec(
    name="echo",
    fn=echo_text,                # 纯函数，同步或 async 均可
    description="原样返回输入文本",
    risk="low",
    timeout_ms=5000,
    idempotent=True,
))

app.replace_tools([...])         # 运行期全量替换能力集（M5 用），内部发 capabilities.update

if __name__ == "__main__":
    app.run()                    # 读 CLI/env，自动选 standalone 或连 core
```

约定：
- `fn` 的参数名与类型注解**就是** `input_schema` 的来源。支持 `str/int/float/bool/list/dict/Optional[T]` 与默认值；遇到不支持的类型**启动即报错**，不静默降级成 `{}`。
- 函数返回值：dict / list / str / int / bool / None。返回 `bytes` 一律拒绝（让模块自己 base64）。
- 抛 `ToolError(code, message, retriable)` → 结构化错误；抛其他异常 → 包装成 `INTERNAL`，**traceback 只进日志**。
- `app.replace_tools(specs)`：**全量替换**能力集，注册后可用；内部发 `module/capabilities.update`，core 拒绝时抛 `ToolError("MANIFEST_INVALID")` 并**保留旧表**（SDK 不得在本地先把旧表丢掉）。
- `app.run()` 只做一件事：解析 `--standalone` / `--core`，二选一。

### T2 · 连接与生命周期（2 天）

| 能力 | 要求 |
|---|---|
| 配对 | `--pair-code` 走 XX；成功后把 core 公钥 + 指纹存 `data/core_pin.json`（0600）。码是 **8 位中英混合**（`青松-A7K2M9`），解析前先 `normalize()`（NFKC → 去 `-`/空格 → 字母 upper），**必须与 core 的 HKDF 用同一份归一化实现**——各写一套就会出现「看着一样却握手失败」 |
| 重连 | IK；指数退避 0.5s → 30s，±20% 抖动；**永不放弃**，但日志降频 |
| 重注册 | 每次重连后重新 `module/register`（core 可能重启过） |
| 心跳 | 每 10s `module/heartbeat {load, inflight}`；连续 3 次发不出去 → 主动重连 |
| drain | 收到 `module/drain {grace_ms}` → 停止接新调用，等在途完成，回 `{accepted}`，退出码 0 |
| 取消 | 收到 `tools/cancel` → 置取消标记；可取消的工具抛 `CancelledError`，不可取消的返回结果但标记作废（core 侧已不等） |
| 密钥 | `MODULE_KEY_PATH` 默认 `data/module.key`，0600；**已存在就复用，绝不覆盖**（覆盖会导致指纹变化被白名单拒） |
| TLS | 校验 core 证书；自签时用配对时 pin 的指纹（TOFU），`CORE_FINGERPRINT` 可覆盖 |

并发：`max_concurrency` 默认 4；用 `asyncio.Semaphore` 限流，超出 core 会排队（`OVERLOADED` 由 core 判，不由模块拒）。

### T3 · CLI 与 standalone（1 天）

统一 CLI（6 个模块都继承，不许各自发明）：

```bash
python -m <module> --print-manifest
python -m <module> --check                       # 校验 manifest + 工具签名
python -m <module> --standalone <tool> --args '{"path":"/tmp/a.txt"}'
python -m <module> --core wss://host:8443 --pair-code '青松-A7K2M9'
python -m <module> --core wss://host:8443        # 已配对，走 IK
```

- `--standalone` **不 import 传输层**（延迟导入），保证 SDK 未连 core 也能跑；输出 JSON 到 stdout，错误走 stderr + 非零退出码。
- `--print-manifest` 输出**与注册时提交的完全一致**的 JSON（同一函数生成，禁止两份）。
- `--check` 校验：工具名正则、description 长度、risk 枚举、schema 合法、工具数 ≤ 64。失败退出码 2。

### T4 · echo 模块（0.5 天）

```
modules/echo/
├── manifest.json            # 由 --print-manifest 生成，提交
├── src/echo_module/
│   ├── tools.py             # 纯函数，不 import sdk
│   └── server.py            # ModuleApp + ToolSpec
├── tests/test_tools.py
├── Dockerfile
├── README.md
└── data/.gitkeep
```

工具：

| 工具 | 用途 | risk |
|---|---|---|
| `echo(text)` | 返回 `{text, len}` | low |
| `add(a, b)` | 数值相加，验证参数类型 | low |
| `sleep(ms)` | 用于 C3/F1 测超时与取消 | low |
| `boom(message)` | 抛 `ToolError("INTERNAL", message)`，测错误通道 | low |

`tools.py` 必须**零 SDK import**，`pytest` 不装 SDK 也能跑（用一个只依赖 stdlib 的测试）。

### T5 · SDK 文档与冻结（1 天）

`sdk/PYTHON_SDK.md` 必须包含：

1. 30 行最小示例（可直接复制运行）；
2. `ToolSpec` 每个字段的语义与默认值；
3. 类型 → JSON Schema 的映射表（含不支持的类型清单）；
4. 错误码使用规则（只能从 `schema/errors.json` 取）；
5. 重连/配对/密钥持久化/容器卷的注意事项（AMEND-01 的坑）；
6. `--standalone` 与连 core 两种模式的行为差异（哪些能力只有连 core 才有：审批、事件广播）；
7. **冻结声明**：`0.1.x` 期间只加可选参数，不改既有语义。

## 五、验收（G1）

```bash
# 1. SDK 单测
pytest sdk/python/tests -q

# 2. echo 独立跑（不连 core）
cd modules/echo && python -m echo_module --standalone echo --args '{"text":"hi"}'
# {"ok":true,"result":{"text":"hi","len":2}}

# 3. manifest 一致性
python -m echo_module --print-manifest | diff - manifest.json

# 4. 端到端
python -m vade --config /tmp/core.toml &
python -m echo_module --core wss://localhost:8443 --pair-code '青松-A7K2M9' &
python tools/send_message.py --text "用 echo 工具回显 hello"
# 期望：turn 完成，tool_result 里出现 hello，最终回复包含 hello

# 5. 断线重连
pkill -f "vade --config"; sleep 2; python -m vade --config /tmp/core.toml &
# echo 自动重连并重新注册，无需人工干预（5s 内，NFR-4）
```

## 六、禁止

- 禁止在 `tools.py` 里 import SDK（这是「模块自足」的硬判据）。
- 禁止 SDK 里出现任何具体模块名（`fs` / `shell` / …）。
- 禁止覆盖已存在的密钥文件。
- 禁止把 `--pair-code` 写进日志或 manifest。
- 禁止为 echo 引入第三方依赖（它要能在裸 Python 上跑）。
- 禁止在 `0.1` 冻结后再改 `ToolSpec` 的必填字段。

## 七、交付格式

按 `00-orchestration.md` §三，额外附：
- `--print-manifest` 与 `manifest.json` 的 diff（必须为空）
- 断线重连的时间戳日志（断开到重新注册 < 5s）
- `sdk/PYTHON_SDK.md` 的冻结版本号
- G1 的完整对话事件流（`events` 表 dump）
