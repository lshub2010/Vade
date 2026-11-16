# RFC-0003 · prompt 注入分层（catalog / 热工具 / stable+context）· RESULT

分支 `tmp/w1-rfc0003`，基于 `68dbcdd`。**未 push**（任务要求先 commit 不 push）。

## 实现点

| 区域 | 内容 |
|---|---|
| 配置 3 键（C-18 增量，`[prompt]`/`[tools]`） | `prompt.injection`（`"legacy"` 默认，仅接受 `legacy`/`layered`，`apply: restart`）；`tools.hot`（`[]` 默认，列表项不在本轮冻结快照内则忽略不报错）；`tools.hot_max`（int 1..64，默认 12，常驻上限，截断后 `+N more tools` 尾注）。根仍是 `additionalProperties:false` —— 拼错键名启动即 `UNKNOWN_CONFIG_KEY` |
| 两条路径 | **legacy**：pre-RFC 六段（persona/tool_block/skill_catalog/skill_bodies/session_memory/context）单条 system 消息，逐字节不动（golden 钉死）；目录/正文仍每轮进 system。**layered**：system 拆 `stable`/`context` 两段（C-11 `SECTION_ORDERS` 锁序，stable 段禁运行事实）；技能目录改为**持久 user 消息**（`<system-reminder>` + `digest:` 行，digest 变才整条重发，空目录发退休条），正文只经 `skill_load` 的 tool 结果进入；工具热常驻 + 冷工具 `tools_search`，命中后**下一步**进 schemas；`skill_load`/`tools_search` 是 core-native 常驻工具（单下划线命名，与模块工具 `{prefix}__{tool}` 双下划线结构隔离） |
| 接线（session 组件） | 读一次 `prompt.injection`（restart 项）；layered 时构建 `build_core_tools(_skill_loader)` + `LayeredCatalogPort(_skill_loader)` 传给 LoopEngine；legacy 时两者为空，loop 全部走 pre-RFC 代码路径 |
| 事件（C-06 冻结字节不动） | `events.json` 用 `x-vade.appends` 追加 `turn.skill_catalog`（`turn_id/digest/entries/empty`）与 `turn.tool_discovered`（query 摘要/命中数）；`rpc.json` 追加 `core_reserved_tools`；`skill.schema.json` 追加 `x-vade.injection`（C-11 机器可读序号表，schema_version 2） |
| 目录消息回放 | 目录消息是普通持久化消息（自带 seq）：从 seq 0 replay 照带；legacy 过滤器把 layered 期写下的目录消息从**模型请求**里滤掉（不删库），切回 legacy 不复活旧提醒 |
| 契约检查 | `tools/check_contracts.py` 增 `check_prompt_layers`（C-11 无重号/stable 全早于 context/通道覆盖、C-18 三键、C-05 保留名、C-06 appends） |

## 惰性接线 bug（上一棒发现并修复，本轮验证）

`install_components` 按**字母序**安装组件：`session` < `skill`，session `install()` 跑的时候 `app.skill_loader` **还不存在**。若在 wiring 期捕获 `app.skill_loader`，layered 的 core tools 与 catalog port 会永久拿到 `None`。修法：session 只传**零参 provider**（闭包 `lambda: getattr(app, "skill_loader", None)`）；`coretools._resolve_loader` 与 `LayeredCatalogPort._loader` **每次调用时**解析（实例 / provider / None 三态都接受，坏 provider 降级为空目录或 `UNAVAILABLE`，不炸 turn）。

承载：`core/tests/integration/test_layered_wiring.py` —— 走 `App.startup()` + `install_components()`（与 `_serve` 相同的两步），技能**在装配之后**才写入并 reload，证明目录消息仍到模型、`skill_load` 仍回答正文、`turn.skill_catalog` 同时落库与推给在线客户端。

## 本轮返工

1. **重写 `test_layered_wiring.py`**（磁盘上第一版草稿有 4 个 bug，全部修掉）：
   - `app.session._llm = llm` → **`app.loop.llm`**（loop 每次调用读 `self.llm.chat`；`app.llm` 只是接线把手，改它不进引擎）；
   - turn 结束后才 `attach` 走的是 snapshot 路径，**不会回放 turn 事件** → sink 改为 submit 前挂载（看在线事件），持久证据改读 `session_store.read_since()`（同步、确定性）；
   - 事件帧键是 `payload` 不是 `params`；
   - `app.session.close(session_id)` 签名不存在（`SessionLog.close()` 无参）→ 改 `await app.session.close()`；删掉 `if False else None` mutation 残留行与死代码。
2. **引擎 no-capability 门挪后**（草稿集成测试暴露的真 bug）：原 `run_turn` 在 `router is None or not snapshot` 就提前返回 "No capabilities"，core-native 工具在其**之后**才 graft —— 无模块的 layered core 从此拿不到目录、`skill_load` 永不可达，违反 §4.2.4（core 工具常驻）与 §4.2.2（目录必达）。修法：`layered` 判定 + `with_core()` graft 上移到门之前；legacy 永不 graft，门与 golden 字节不动（`test_engine.py::test_no_modules_…` 仍绿）。

## 测试

新增/更新：`loop/test_prompt_legacy.py`（golden）、`loop/test_prompt_layers.py`（A1/A2/A8）、`loop/test_tool_discovery.py`（A5/A6）、`test_skill/test_catalog_message.py`（A3/A4/A10）、`config/test_prompt_switch.py`（A11）、`session/test_replay_catalog.py`（A12）、`router/test_name_namespace.py`（A13）、`integration/test_layered_wiring.py`（真实装配）。

```
pytest core/tests -q      → 967 passed, 6 warnings（c19-fix 基线 934 + 33）
tools/check_contracts.py  → contracts: OK（… RFC-0003 layers green）
docs/check_docs.py        → 合计 12 项，失败 0 项
```

## Golden 兼容证据（A9 / G5）

`core/tests/loop/golden/prompt_legacy.json` 从 pre-RFC 代码捕获（prompt.py/tools.py/loader.py/render.py 在 a0a5062 与本 worktree 基点之间无改动，故 legacy 路径即 a0a5062）。三个用例：case_a/case_a_llm（六段全填的 `assemble()` 输出，`to_dict` + `to_llm` 双视图字节比对）、case_b（空段跳过）、case_c（完整 legacy `run_turn`：模型收到的每条消息 + 每个工具 schema，hooks 含）。另有同输入双跑 sha256 字节稳定测试（前缀缓存的基座）。四个测试全绿。

## 变异承重（两个，本轮实测）

| # | 变异 | 结果 |
|---|---|---|
| M1 | `_inject_catalog` 改为直接 `return history`（目录注入失效） | **5 failed / 962 passed**：`test_layered_wiring`、`test_prompt_layers::A1/A2/A3`、`test_replay_catalog` |
| M2 | legacy 改道 layered 机器（session 接线 `injection = "layered" if True else "legacy"`） | **1 failed / 966 passed**：`test_core_wiring::test_full_turn_goes_through_a_real_connection_not_a_stub`（真实装配的 provider 视图 golden） |

诚实备注：`test_prompt_legacy` 的 engine 级 golden **抓不到 M2** —— 它直接钉 `LoopConfig(injection="legacy")`，绕过 session 接线；M2 由装配级测试兜住。两个变异均已还原（与备份逐字节 diff 验证），无 mutation 残留（`grep mutated/if False` 全仓仅剩合法命中）。

## 与 RFC 的偏差 / 未做项（诚实记录）

1. **Q1（目录消息对用户可见性）**：C-06 侧已按 `x-vade.appends` 落了 `turn.skill_catalog` 事件，但客户端渲染形态（系统提示条 vs 隐藏）仍待 C-06/C3 点头，本轮未做 UI 面板。
2. **Q5（压缩 vs 目录消息）**：`loop/context.py` 的压缩路径没有对目录消息做 `protect_first_n` 保护——压缩丢它 → 下一轮必重发（一次性全价）。按 RFC 属未决问题，本轮未改压缩器。
3. **U1-0 验收未跑**：激活率对比（triggers 从自动注入退化为提示加载）需要真实用户数据，本轮只有单测证据。
4. **真实 provider 的前缀缓存验证**：A1/A2 的字节稳定是代理证据；OpenAI 自动缓存的实际命中未实测。
5. **`[tools] hot` 无运维默认名单**：键与语义落地（不在快照内的项静默忽略），但没有任何环境给出推荐值，Q4 仍是「配置/技能/会话」三源，未做模块自荐（RFC 明确拒绝）。
