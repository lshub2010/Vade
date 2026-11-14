# RFC-0003 Q5 + U1 · 压缩保护目录消息与 stable 段字节稳定性 · RESULT

分支 `tmp/rfc0003-q5`，基于 `ba25628`（dev_gingshan）。代码 commit **`0eb43c0`**（先 commit 后写本报告），未 push、未合并。

## 1. Q5 选型：语义保护谓词（protect all catalog messages），不是「压缩后重发」

**改动（两处，共 +48 行含测试外注释）：**

| 文件 | 改动 |
|---|---|
| `core/vade/loop/context.py` | `fit()` / `_protected_indices()` 增可选参数 `protect: Callable[[Message], bool]`。命中的消息对三步攻击全部免疫：step 1 工具结果剪枝、step 2 assistant 截断、step 3 整条丢弃。谓词归调用方所有——「哪条消息语义上不可丢」是调用方的知识，压缩器保持通用；`protect=None` 时行为与改动前逐字节相同 |
| `core/vade/loop/engine.py` | layered 路径传 `protect=self._catalog_keeper`（`role == "user" and is_catalog_message(content)`）；legacy 路径恒传 `None`。**保护所有**目录消息，不只最后一条 |

**为什么选保护而不是「压缩丢了一条就补发一条」（RFC §10 Q5 的重发路线）：**

1. **正确性缺口不同**。重发路线治的是「模型看不到目录」，但重发判定（`_inject_catalog`）是对**原始日志**做的——日志里目录还在，digest 未变就不重发。压缩丢的是 **fitted 视图**里的消息，这个缺口重发路线根本看不见。保护路线直接让 fitted 视图与日志的判定基准一致：日志里最后一条目录消息 == fitted 视图里最后一条目录消息。
2. **零额外 token**。补发路线每次压缩付一次全价（0.4–1.5k）；保护只保留本就该在场的字节。
3. **不破 fitted 字节稳定**。压缩丢任意一条消息，其后的全部历史位移一次——provider 前缀缓存对位移之后的段全部 miss。保护所有目录消息后，目录位置永不漂移。
4. **legacy 自动零影响**。legacy 历史里不可能有目录消息（session 接线 `_history_of(layered=False)` 先滤掉），谓词恒不命中；且 engine 在 legacy 分支根本不传谓词。

**代价（如实）：**

- 目录消息（约 0.4–1.5k token/条）**永久免于压缩**。条数由 digest 变化频率决定（install / uninstall / enable / disable / 描述变化），不由会话长度决定——正常会话 1–2 条，极端高频改技能的会话会累积。压缩预算因此收紧一个相应量，其余消息承担全部压缩压力（测试 2 证明压缩仍真压缩）。
- 退休条（空目录）同样受保护——它是防旧技能名复活的唯一凭证，丢不得。
- 未来若要回收旧代目录，只能在「保留最后 N 代」上做文章，且每回收一条仍付一次前缀位移——本轮不做。

## 2. U1 可测化：压缩发生后 stable 段字节不变

新增两条：

- `test_stable_section_is_byte_identical_across_five_ordinary_turns` —— 连续 5 轮 stable 段（persona/policy/tool_block，排除 runtime facts，与 A1/A2 同口径）sha256 一致，tools 数组一致，目录每轮可见且只发一次。
- `test_stable_section_is_byte_identical_across_compaction` —— 每轮都触发压缩的小窗口配置下，stable 段 sha256 与 tools 数组仍逐轮一致，目录每轮仍达模型。

**诚实口径**：这是 provider 侧 prompt cache 命中的**必要条件**（前缀字节不动），**不是充分条件**——真实命中还取决于 provider 的分块策略、tools 数组与消息边界、以及 provider 是否真的开了缓存。**真实 provider 命中仍未实测**。验证方法（真机一次即可）：对同一会话连发 ≥3 轮（其中一轮触发压缩），读 `turn.completed.usage` 里 provider 回报的 `cached_tokens`/`prompt_tokens`（RFC-0003 §6 已要求归一化保留原始键），第 3 轮起 `cached_tokens / prompt_tokens` 应显著 > 0（OpenAI 自动缓存口径 ≥ 0.25 折算即命中；Anthropic 看 `cache_read_input_tokens`）。

## 3. 测试（13 条，`core/tests/loop/test_compaction_catalog.py`）

| # | 测试 | 覆盖 |
|---|---|---|
| 1 | `test_compaction_keeps_the_catalog_reminder_visible` | 压缩后目录仍在，逐字节相等；压缩非 no-op |
| 2 | `test_compaction_still_compacts_ordinary_history` | 压缩仍真压缩普通历史（6 条种子全被收走，只剩目录） |
| 3 | `test_retirement_message_survives_compaction` | 空目录退休条受保护、逐字节相等 |
| 4 | `test_every_catalog_generation_survives_compaction` | digest 变化后两代目录都存活，最后一条带新 digest |
| 5 | `test_default_protect_window_alone_does_not_save_the_reminder` | **默认配置**（protect_last_n=20）下目录落在窗口外 23 条深——只有 Q5 谓词救它 |
| 6 | `test_stable_section_is_byte_identical_across_five_ordinary_turns` | U1：连续 5 轮 stable 段不变 |
| 7 | `test_stable_section_is_byte_identical_across_compaction` | U1：压缩发生后 stable 段不变 |
| 8 | `test_digest_change_still_resends_after_compaction` | 压缩后 digest 变化仍整条重发（回归保护） |
| 9 | `test_legacy_path_has_no_reminder_and_still_compacts` | legacy 路径：无提醒消息、目录走 system、压缩行为同前 |
| 10 | `test_fit_protect_predicate_spares_the_marked_message` | 单元：谓词命中者幸存、逐字节相等 |
| 11 | `test_fit_without_protect_drops_the_catalog_message` | 单元：无谓词时目录被丢（钉住缺陷本体，防保护测试变空转） |
| 12 | `test_fit_protect_keeps_order_and_later_messages_survive` | 单元：保护不打乱顺序，目录之后的较新消息仍正常幸存/丢弃 |
| 13 | `test_fit_protect_trims_neighbors_but_never_the_reminder` | 单元：邻居被截成 `[trimmed]`，目录字节完好 |

layered 引擎级 1–8、10–13，legacy 引擎级 9，ContextBuilder 单元 10–13。种子消息 600 字符交替 user/assistant，同时压到 step 2（截断）与 step 3（整条丢）两条攻击路径。

## 4. 变异自验（真跑，均已还原）

备份 `engine.py` → sha256 `b3e8cc1f…`，变异后跑 `pytest core/tests/loop -q`，再从备份还原并核对 sha256 相同、`git diff --stat` 只剩本次改动、全仓 grep `MUTATION/MUTATED/if False` 零残留。

| # | 变异 | 结果 |
|---|---|---|
| M1 | `protect=self._catalog_keeper if layered else None` → `protect=None`（去掉目录保护） | **7 failed / 77 passed**：测试 1/2/3/4/5/7/8 全红（保护类 + 压缩非空转断言），单元 10–13 与 legacy 9 保持绿（谓词与 legacy 不经过该行，符合预期） |
| M2 | `persona=self.config.persona` → `persona=f"{self.config.persona}\n- now: {time.time_ns()}"`（stable 段掺时间戳） | **6 failed / 78 passed**：本轮 6/7 两条字节不变测试红；连带 `test_prompt_layers` A1/A2 与 `test_prompt_legacy` 两条 golden 字节稳定测试红（既有 stable 钉死网同时报警，佐证测试口径一致） |

还原证据：`cp` 回备份后 `sha256sum` 与备份一致（`b3e8cc1fb4f8cc14cf6c87d8eb9354eda135dc9fb4093f025930bd9d29f1c33c`）。

## 5. 门禁（原始输出尾部）

```
$ .venv/Scripts/python.exe -m pytest core/tests -q        （基线 1041）
1054 passed, 6 warnings in 55.25s

$ tools/check_contracts.py
contracts: OK (C-03/C-04/C-05/C-06/C-18 frozen, 34 error codes, config metadata complete,
RFC-0005 append green, RFC-0010 session metadata green, RFC-0012 subagent family green,
RFC-0003 layers green, event gate green)

$ docs/check_docs.py
合计 12 项，失败 0 项。
```

1054 = 基线 1041 + 新增 13。诚实备注：本轮第一次全量跑出现过 1 个与本改动无关的红——`core/tests/blob/test_review_fixes.py::test_the_periodic_sweeper_collects_and_stop_leaves_no_task`（定时清扫器计时型测试），单独跑与重跑全量均绿（12 passed / 2.90s；复跑全量 1054 passed），判定为负载下偶发 flake，非本改动引入（本改动不触 blob/sweeper 任何路径）。

## 6. 未验证项（诚实清单）

1. **真实 provider 缓存命中未实测**（U1 本体）：本轮只有字节稳定 + 压缩后仍稳定的代理证据。真机验证方法见 §2，一次会话即可完成。
2. **多代目录累积的真实分布未测**：保护所有目录消息的 token 代价取决于真实会话里 digest 变化频率，本轮无真实数据；若某环境高频改技能，可考虑后续加「保留最后 N 代」策略（每回收一条付一次前缀位移）。
3. **R7（压缩 cache-safe forking）未做**：压缩本身仍会使其后历史位移、破一次缓存——这是 RFC §11 登记的后续项，本轮不动。
4. **超长会话（数千轮）下的 fit() 行为**未做规模压测，仅单测级验证。
