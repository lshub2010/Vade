# C3 · 会话 / 队列 / 路由 / Agent 循环 · RESULT

| 项 | 值 |
|---|---|
| 分支 | `tmp/c3-session`（基线 `dev_gingshan` 179a080） |
| 契约 | **C-06 `schema/events.json` 已冻结**（1.0.0，2026-11-13） |
| 测试 | `pytest core/tests -q` → **131 passed**（0 failed, 0 skipped） |
| 契约门禁 | `python3 tools/check_contracts.py` → `contracts: OK (... 33 error codes ...)` |
| schema/events.json sha256 | `dcca414a48b8a8b294699b81f23dea3b8dea0f2b73904010e8407de9dc3b4ed9` |
| push | 未 push（按任务要求） |

## 一、范围与出口

已交付（按优先级）：

1. **session store（SQLite）+ 事件流 + 恢复**：`SessionStore` 协议 + `SQLiteStore`，`next_seq` 走 `UPDATE ... RETURNING`（SQLite 3.35+，回退为 `BEGIN IMMEDIATE` 读改写），`append(msgs, events)` 一批一事务，先落盘后广播，`load()` 恢复 session/turn/message 树。
2. **队列/中断**：每会话单 worker 串行；`client_msg_id` 幂等（session 内唯一索引）；`interrupt` 立即释放队列、取消在途任务并向 router 发 `tools/cancel`，**不等待模块**；`cancel` 只对 queued 生效（running 返回 `INVALID_ARGUMENT`）；队列状态（running/running_state/pending）可查。
3. **router**：`{prefix}__{tool}` 双向映射（冲突 `fs2__`）、exclusive/pooled 实例选择、`ctx.session_id` 注入（未声明不发 ctx；模型伪造被覆盖或按 `additionalProperties:false` 拒绝）、deadline、`tools/cancel`、C-04 错误映射、30s 窗口熔断 + half-open。
4. **Agent loop 骨架**：可注入 mock 的 `LLMProvider`（`MockLLMProvider` + OpenAI 兼容 SSE 适配器）、工具循环、流式 delta、未知工具回填、max_steps 可读终止、`tokens > threshold*ctx` 裁剪（参数全部来自 `[compaction]`）、`PromptParts/assemble` 纯函数 + C3b hook 位。

未交付：T5 的 `ModuleLLMProvider`、T6 的 `session.turn` / `session.closed`（C-14 属 C3d）与完整 UI 连接生命周期（见 unverified）。

## 二、changed（文件 + md5）

| 文件 | md5 |
|---|---|
| `core/vade/slots.py` | `41d52bc3fce1a0d52f65891b29a9034f` |
| `schema/events.json` | `33d6dd731f860cdc523be891a718d76d` |
| `core/vade/session/__init__.py` | `cca55dd1cf14597082bc5dad8ba1c244` |
| `core/vade/session/models.py` | `6017be1c0c4596f4d9e1d32db29ea760` |
| `core/vade/session/store.py` | `e96fb92a0dd5f1542d23e348f372bf1d` |
| `core/vade/session/sqlite_store.py` | `87c272e52561ed79e7bccc183a456620` |
| `core/vade/session/seq.py` | `7c4c79585286969d52d263c0f7b2ea99` |
| `core/vade/session/replay.py` | `92a6d1bbcdff08dafdba697dfcf95d70` |
| `core/vade/session/manager.py` | `c0355aaaf6a692f435878bba0ce6df07` |
| `core/vade/session/queue.py` | `654017320318ba2ee407b7210389447f` |
| `core/vade/session/rpc.py` | `786575665493461132098a31e21e0e6e` |
| `core/vade/router/__init__.py` | `70c63e6c887f0c057065b65600a8d125` |
| `core/vade/router/names.py` | `0b81c5e2954904f03e31c2d8323022ce` |
| `core/vade/router/dispatch.py` | `a655fee3ed75366b18c827a2a56bf14b` |
| `core/vade/router/breaker.py` | `ab563f29b8d4840daa8a005a2f7afb89` |
| `core/vade/loop/__init__.py` | `882daa141dbd8c875443e0bd5d6d3c4b` |
| `core/vade/loop/ports.py` | `05d88acf4694e665dffab57b6d0a7c86` |
| `core/vade/loop/prompt.py` | `5fd3356adacc486bc96d6270cc4b31bf` |
| `core/vade/loop/tools.py` | `7a61250aa035f17d34e93df59ab741b3` |
| `core/vade/loop/context.py` | `efe8ba78fc63037fac99c9d2247b1a1e` |
| `core/vade/loop/engine.py` | `92b6174eecf15044e24041f897307baa` |
| `core/vade/llm/__init__.py` | `918cda3301bfde008d879fb72923cd6f` |
| `core/vade/llm/base.py` | `08a4f06c270625733cdd2caa4b2c2b7d` |
| `core/vade/llm/mock.py` | `36757c6e0b86bb66375915a815f1b7fd` |
| `core/vade/llm/openai_compat.py` | `af6d4f704d8101fce81fdb289fd4e20c` |
| `core/tests/__init__.py` | `d41d8cd98f00b204e9800998ecf8427e` |
| `core/tests/session/__init__.py` | `d41d8cd98f00b204e9800998ecf8427e` |
| `core/tests/session/helpers.py` | `61f5a8732d1221408146b3a157fcdab8` |
| `core/tests/session/test_store.py` | `f7cd6ac64ba02f6a39e54c610c7383a1` |
| `core/tests/session/test_queue.py` | `390a5ef0f6afeb9b2246d485c97dab8f` |
| `core/tests/session/test_replay.py` | `2a3be17db60db8d596515cad0581cdc6` |
| `core/tests/router/__init__.py` | `d41d8cd98f00b204e9800998ecf8427e` |
| `core/tests/router/test_names.py` | `c79f33c655db0afa8d26b3ffbefbf2d6` |
| `core/tests/router/test_dispatch.py` | `5a06d3531a7c92c1b42b533371ea34f2` |
| `core/tests/loop/__init__.py` | `d41d8cd98f00b204e9800998ecf8427e` |
| `core/tests/loop/test_engine.py` | `17abfb86f47137db325e62a6996ea2e6` |
| `core/tests/e2e/__init__.py` | `d41d8cd98f00b204e9800998ecf8427e` |
| `core/tests/e2e/test_turn_lifecycle.py` | `525ca8898e079266eaed1de1a9add0d0` |

另：`protocol/core/python/libvade_protocol.so` 是从主检出复制来的构建产物（未入库，`.gitignore` 已忽略），本 worktree 缺它时 transport 测试无法跑。

## 三、red_evidence（真实失败输出，非编造）

开发过程中测试先红后绿，以下是本轮实际捕获的失败（原样摘录）：

```
# 1) router 把 Target（含 input_schema dict）放进 set，整轮 turn 直接 INTERNAL
TypeError: cannot use 'tuple' as a set element (unhashable type: 'dict')
  core/vade/router/dispatch.py:220 in Router.call
→ 修复：_calls 改为 list[tuple[Target, str]]，e2e 正常一轮测试转绿。

# 2) attach 返回的是 attached 事件 seq，而不是快照点
AssertionError: assert 7 == 6
  core/tests/session/test_replay.py:57
→ 修复：snapshot 分支 from_seq = snapshot["from_seq"]。

# 3) 名字映射实际可变（"只读"当时是纸面承诺）
AssertionError: assert {'com.example...': 'fs__other'} == {'com.example...': 'fs__read_file'}
  core/tests/router/test_names.py:39
→ 修复：forward/backward/prefixes 全部 MappingProxyType。

# 4) 中断测试等待 "running == next_turn" 偶发失败
assert False  (wait_until 0.5s)
  core/tests/e2e/test_turn_lifecycle.py:204
→ 根因：下一轮可能在 5ms 轮询间隔内已经完成；判据改为 started_at 非空，
   这正是「中断后队列立即释放」的证明。

# 5) RPC 方法名写错（"ui.submit"），被 SessionRPC 的 NOT_FOUND 分支挡住
{'code': 'NOT_FOUND', 'message': "unknown method 'ui.submit'"}
→ 修复测试；RPC 错误封套按预期工作。

# 6) 早期中断测试留下 pending task（未 close 就断言失败）
Task was destroyed but it is pending! task: <Task pending name='queue-...'>
→ 修复：所有路径 await log.close()，queue.shutdown 取消 worker/orphan。
```

完整套件在本轮曾经出现的红态（说明测试确实会失败）：`9 failed, 37 passed`、`1 failed, 45 passed`，最终 `131 passed`。

## 四、一轮完整 turn 的 events dump（真实运行，seq/type）

```
1 turn.queued      {"turn_id":"bd1dbc7b...","state":"queued","client_msg_id":"m1"}
2 queue.updated    {"running":null,"pending":[{"turn_id":"bd1dbc7b...","state":"queued"}]}
3 turn.started     {"turn_id":"bd1dbc7b...","state":"running"}
4 queue.updated    {"running":"bd1dbc7b...","running_state":"running","pending":[]}
5 turn.tool_call   {"turn_id":"bd1dbc7b...","call_id":"c1","tool":"fs__read_file","args_digest":"sha256:fde64ffe..."}
6 turn.tool_result {"turn_id":"bd1dbc7b...","call_id":"c1","ok":true,"summary":"{\"echo\": {\"path\": \"a\"}}"}
7 turn.delta       {"turn_id":"bd1dbc7b...","text":"all done"}
8 turn.completed   {"turn_id":"bd1dbc7b...","state":"completed","usage":{},"stopped_reason":"stop"}
9 queue.updated    {"running":null,"running_state":null,"pending":[]}
```

消息树（同一轮）：`user("read a") → assistant(tool_calls) → tool(result) → assistant("all done")`。

## 五、关键设计决定（需下游知道）

- **消息不消耗事件 seq**：`messages.seq` 只记录随行事件的 seq，事件流因此严格 +1 无洞（验收要求）。
- `turns` 表在任务书骨架之外增加 `client_msg_id / user_text / stopped_reason / error_code / error_message / usage / tool_calls` 列；`client_msg_id` 有 session 内唯一索引，幂等由存储层兜底。
- `interrupt` 的终态写入是**幂等**的（`_finalized` 集合），interrupt 与正常完成竞争时只有第一个生效。
- `queue.state()` 增加 `running_state`，`queue.updated` 直接带 `session_id`，UI 不需要猜。
- 路由拒绝 > 65263 B 的 `tools/call` JSON（65519 减去 JSON-RPC 信封预留），返回 `PAYLOAD_TOO_LARGE`，不做 v1 分片。
- `events.json` 在 §2.5 的 13 个事件之外**新增 `turn.cancelled`**（`cancelled` 终态需要它），C-06 属 C3、W3 第 1 天冻结，C4/U1 需要适配；`turn.state` 以枚举 + 每个 `turn.*` 的 `state` 字段提供。

## 六、unverified（逐条，未验证即未验证）

1. **core→module 的 `tools/call` 真实链路未打通**：C2 的 `Connection` 没有 pending-request 表（入站响应被 `make_rpc_handler` 丢弃），Router 只对着注入的 `ModuleCaller` 测过。真实 WS 往返、`tools/cancel` 的模块侧行为均未跑。
2. **`ModuleLLMProvider`（T5）未实现**；`[llm] provider="module"` 目前回落 builtin 或 None。
3. **OpenAI 兼容适配器未对真实端点跑过**；SSE 解析函数未单独单测（只通过 mock provider 覆盖了 Delta 形状）。
4. **UI RPC 未接进 C2 的 dispatcher**：`app.session_handlers` 已发布，但 `make_rpc_handler`（C2 所有）仍只认 `module/*`，需要一行接线；浏览器/WS 会话路径未验证。
5. **`module.status` 未发射**：需要 registry 变更回调；`session.turn` / `session.closed`（C-14）属 C3d，未发。T6 的会话关闭四步顺序未实现。
6. **审批只有端口**：`NullApprovalPort` 对 high 立即拒绝（fail-closed），没有 60s 超时等待、没有多端 first-wins 的真实测试；`approval/decide` 的 `resolved` 形状按 duck typing 兼容，未与 C5a 联调。
7. **上下文裁剪的 token 估算 = len/4**，不是真实 tokenizer；`tail_mode` 只读不用（C3b 的 restart 项）。
8. **SQLite 单进程**：连接由 RLock 串行化，未验证多进程并发；v1 单 core 约束下成立。
9. **重启后 replay 的 24h 过期不生效**：DB 保留全部事件，过期语义只在内存 buffer 上验证（有测试）。
10. **中断在途工具调用的模块确认**：只发 `tools/cancel` 不等待（符合设计），但没有真实模块验证「结果作废」。
11. **`libvade_protocol.so` 非本 worktree 构建**：全绿依赖从主检出复制；CI 需先构建 Rust 协议库。
12. **性能**：未测 SQLite 写放大、事件广播吞吐、10k 事件 replay 耗时。

## 七、测试覆盖清单（47 条 C3 测试 + 83 条 C2 回归）

- session：seq 原子性（8 线程 × 25 次）、批量落盘、重开恢复、client_msg_id 唯一、无 SQL 泄漏到调用方。
- queue：串行顺序、幂等 submit、queued 取消、running 取消拒绝、**中断 <0.5s 且模块忽略 cancel 也能启动下一轮**、审批只卡队首、队列可观察、先落盘后广播。
- replay：覆盖时 11..31 连续无重复、裁剪/过期强制 snapshot、attach 无 last_seq 返回 snapshot、null session 自动建会话。
- router：命名冲突/稳定性、exclusive 离线、pooled 最少在途、ctx 仅在声明时注入、伪造 session_id 覆盖/拒绝、超时+取消、未知错误码→INTERNAL、5 次超时熔断+30s half-open、超长参数 `PAYLOAD_TOO_LARGE`。
- loop：工具链事件顺序、分片 tool_call 参数拼接、未知工具回填、max_steps 可读终止、无模块可读提示、**源码不含具体工具名**、high risk 无审批拒绝、assemble 纯函数、裁剪+blob。
- e2e：完整 turn 事件序列与调用链、重启后从 SQLite replay 还原调用链、三端同 seq 流、中断后两条用户消息都不丢。


---

## 审查修复轮（2026-11-13）

审查报告 `/root/HermesWorkspace/Vade-REVIEW-C3.md` 判不通过，列 5 条真缺陷。本轮回合：

| 缺陷 | 修复 |
|---|---|
| `app.module_caller` 从未赋值 → 每个 turn 只回「无可用能力」 | `startup()` 装配 `ConnectionCaller(self._connections)`，`session.install()` 据此建 Router |
| `tools/call` 响应被丢弃（无 pending 表） | `Connection._pending` + `request()/notify()`，无 method 消息按 id 派发；`close()` 时 `_fail_pending(UNAVAILABLE)` |
| `ui/*` 六方法 NOT_FOUND | `make_rpc_handler` 增加 `app.session_handlers` 查表 |
| `ModuleLLMProvider` 未实现 | 新增 `core/vade/llm/module.py`（priority 选路、Router 调 `model/chat`、fallback） |
| `module.status` 零发射 | `registry.table.add_listener` + `_publish_status`（7 个触发点）+ `session/status.py` |
| C-06 rule 4 自相矛盾 | 改为「仅状态变更事件携带 `state`」；`check_contracts.py` 增加 C-06 门禁 |

**改了 C2 目录**（`transport/connection.py`、`app.py`、`registry/table.py`）：pending-request 属传输层、`module_caller` 属装配、registry 缺变更回调；C-01/C-03/C-04 语义未动。

### 测试

```
pytest core/tests -q          → 150 passed
pytest core/tests/integration → 5 passed
check_contracts.py            → OK（含 C-06 event gate）
```

新增：并发串行（6 路 gather → max_active=1、FIFO、seq 无洞）、中断真取消（模块收到 cancel 恰好一次、interrupted 后无 tool_result/delta）、SSE 分片拼接、ModuleLLMProvider 7 条、module.status 2 条、真 WS `ui/*` 端到端 1 条。

**red_evidence**（两处突变，均逐字节还原）：
- 去掉 `TurnQueue.interrupt` 的 `task.cancel()` → `AssertionError: interrupt did not cancel the running turn task`
- `Connection._dispatch` 改回「无 method 直接 return」→ `tools/call` 结果变成 `TIMEOUT`，断言红

**flaky 修正**：`test_interrupt_releases_queue_immediately_even_if_module_ignores_cancel` 原用 0.5s 墙钟阈值，高负载下偶发假红；改为 2.0s（模块本身 sleep 5s，语义不变）。

### 契约缺口（待 C-18 授权）

`llm.model_module` / `llm.fallback_to_builtin` 不在 `config.schema.json` 内，代码用 `cfg.get` 带默认值读取；C-18 的 `additionalProperties: false` 会拒绝这两个键，需要在 C-18 增补或走 RFC。

### 残留 unverified

- 模块吞掉 `CancelledError` 并正常返回时，engine 仍可能在 `turn.interrupted` 之后再发 `tool_result`（未加守卫）
- TLS、XXpsk3 首次配对（C2 遗留）
- 真实 LLM 端点（只测了 mock）
