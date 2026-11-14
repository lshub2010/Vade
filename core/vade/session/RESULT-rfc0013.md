# RESULT — RFC-0013 中断留痕落库（R9 / FR-8.35）

分支 `tmp/rfc0013-interrupt`（worktree `Vade-wt/rfc0013-interrupt`，基线 HEAD `e200772`）。提交见本文末尾。

## 0. 一句话结论

**FR-8.35 的验收句「中断后在另一端 attach，已生成的 N 字仍可见」现在可以标通过**：A1 用例以「同一库、全新 `SessionLog`、全新 attach（snapshot 路径，零 delta 重放）」复现了另一端视角，snapshot 里 partial 消息 `partial=true`、内容与被中断端逐字一致（含中文多段 delta）。需求册里「不得标通过」的前提——C-06 注记仍说「partial 只能从 turn.delta 重建」——已随本批删除。

## 1. 交付物

| 文件 | 变更 |
|---|---|
| `docs/rfc/RFC-0013-interrupt-persistence.md` | 新增，`status: proposed`，§1–§10 全（摘要+决定表 / 动机证据 / 目标非目标 / 方案 / 数据模型 / 契约变更 / 与 regenerate 关系 / 未决问题×4 / 验收 A1–A8 / 影响面） |
| `docs/rfc/README.md` | 索引追加 RFC-0013 行 |
| `schema/events.json` | `turn.interrupted.notes` 就地改写：删除「an interrupted assistant's partial text is reconstructable only from turn.delta events」，替换为落库真相句（RFC-0013/FR-8.35）；其余句子逐字保留（`source="none"` 等，已与 HEAD 逐字节比对）；`x-vade.appends` 追加 RFC-0013 C-06 块（version **1.2.0**，rewrites 仅注记）；顶层 version 保持 **2.0.0** |
| `schema/rpc.json` | `ui/attach.notes` 追加 `snapshot.messages[].partial` 声明句；`x-vade.appends` 追加 RFC-0013 C-05 块（version **1.2.0**，rewrites none）；顶层 version 保持 **1.3.0** |
| `core/tests/session/test_rfc0013_partial_persistence.py` | 新增 **13 条** |
| `core/vade/session/RESULT-rfc0013.md` | 本文件 |

## 2. 实现侧的复用裁定（重要）

任务背景里「partial 没落库到 snapshot」在当前 HEAD 不成立：grep `partial_message_id` 证实 RFC-0006 §4 的落库实现**已在** `core/vade/session/` 全链路存在——`manager.partial_text`/`_partial` 累积 → `queue._partial_message()`（文本非空才建 `Message(partial=True)`）→ `_finalize_interrupted()` 单事务写 turn 行+消息+事件 → `messages.partial` 列（`_APPENDED_COLUMNS` 可重入迁移）→ `_message_from_row` 回读 → `Message.to_dict()` 进 snapshot。按任务「已有类似逻辑就复用」的指令：

- `sqlite_store.py` / `models.py` / `queue.py` / `manager.py` / `rpc.py` **零代码改动**（RFC §5/§10 逐文件写明复用确认）；
- 任务提名的 `Turn.interrupted_partial_message_id` **不采纳**：现名 `partial_message_id` 已在事件键、DB 列、`x-vade.appends[0].fields` 三处冻结，改名是纯破坏面（RFC §4/§5 有裁定段）；
- 真正的缺口是**契约层**（C-06 假注记、C-05 未声明字段）与**验收测试**，本批补齐。

C-06 注记改写是对 RFC-0006 §9.3「不改 notes 字节」惯例的**明示偏离**（假注记会误导客户端丢弃落库消息），偏离理由与审计说明写在 RFC-0013 §2.2/§6.1 与新 append 块的 `overrides` 里；RFC-0006 的 `appends[0]` 原样未动（契约门仍验它的 `rewrites=="none"`/`version=="1.1.0"`）。

## 3. 测试（13 条，`test_rfc0013_partial_persistence.py`）

| 验收 | 用例 | 断言要点 |
|---|---|---|
| A1（FR-8.35 验收句） | `test_fr835_a_fresh_attach_on_a_second_log_sees_the_partial_text` | 另一端新 log + `attach(last_seq=None)`：恰 1 条 `partial=true` assistant 消息，`content == "你好，世界！还没写完。"`，turn 行指针一致 |
| A2 | `test_rpc_ui_attach_snapshot_carries_the_partial_flag` | 生产 `SessionRPC.dispatch("ui/attach")`：result 三键；**每条** message 带 `partial` 键；恰 1 条 true；user 行 false |
| A3 | `test_partial_chars_equals_the_persisted_content_length` | `partial_chars == len(content) == len(拼接 delta)`（含中文，4 段） |
| A4a | `test_a_completed_turn_never_produces_a_partial_message` | completed turn：无 partial 行、指针 null |
| A4b | `test_a_failed_turn_never_produces_a_partial_message` | 流过 delta 后 raise：`turn.failed`、无 partial、指针 null（只有中断路径写 partial） |
| A5 | `test_the_partial_message_survives_a_store_reopen` | 关库→重开同路径：消息仍在、`partial=True` 从列回读 |
| A6 | `test_the_turn_row_pointer_matches_the_partial_message` | 事件 payload、store turn 行、snapshot 三方同一 `msg:<hex>` |
| A7 | `test_regenerating_an_interrupted_turn_keeps_the_partial_visible` | §7：regenerate 后旧 turn `superseded_by` 指新 turn；partial 消息仍绑旧 turn、恰 1 条；新 turn 回答 `partial=false` |
| 护栏 | `test_a_partial_bubble_lands_only_on_the_interrupted_turn` | 双 turn（一完成一中断）：partial 恰 1 条、只落在中断 turn；turn1 指针 null |
| 护栏 | `test_ordinary_messages_default_partial_to_false` | 构造器默认 + `to_dict()` 输出 false |
| 护栏 | `test_attach_replay_path_still_carries_the_pointer` | replay 路径（`last_seq=0`）：`turn.interrupted` 恰 1 次、带指针+字数、不带 text |
| A8a | `test_events_json_no_longer_claims_delta_is_the_only_source` | notes 无旧句、含真相句与 FR-8.35；RFC-0013 C-06 块恰 1 个 version 1.2.0；顶层 2.0.0 与 RFC-0006 块原样 |
| A8b | `test_rpc_json_documents_the_partial_field` | `ui/attach.notes` 声明 `partial`；RFC-0013 C-05 块恰 1 个 version 1.2.0；顶层 1.3.0 原样 |

既有 `test_interrupt_partial.py`（RFC-0006 的 A1–A4/S2/S4 共 5 条）全部继续通过。

## 4. 验证（真实输出）

- **`pytest core/tests -q` → `979 passed, 6 warnings`**（基线 966 + 新 13，0 failed；≥966 达标）。解释器：`C:/Users/LENSHLD/AppData/Local/Programs/Python/Python314/python.exe -m pytest`（默认 `python` 是 Hermes venv、无 pytest）。
- **`tools/check_contracts.py` → `contracts: OK`**（C-03/04/05/06/18 frozen、34 错误码、RFC-0005/0010 append green、event gate green）。
- **`docs/check_docs.py` → `合计 12 项，失败 0 项`**（12/12）。

## 5. 变异自验（2 个，均已实测）

| 变异 | 注入点 | 结果 | 恢复 |
|---|---|---|---|
| **M1 不写 partial 消息** | `queue.py::_finalize_interrupted` → `messages=()` | **7 failed / 6 passed**——含 FR-8.35 attach 用例（A1）与 rpc 标记（A2） | `git checkout` 还原 |
| **M2 `partial` 恒为 false** | `queue.py::_partial_message` → `partial=False` | **7 failed / 6 passed**——所有 `partial is True` 断言变红 | `git checkout` 还原 |

两个变异打击面一致但故障模式不同（M1 下消息整体消失、指针悬空；M2 下消息在而标记错），新文件对两者均非幸存。还原后全量复跑仍 979 passed。

## 6. 硬约束核对

- `turn_states` 仍 7 值、错误码仍 34（契约门实测绿）；
- `core/vade/loop/` `blob/` `tasks/` `input/` `modulepage/` `prompt/` **未动**（`git status` 仅 docs/schema/session-tests 六个文件）；
- delta 流式机制未动（`queue.py` 仅两处变异演练后还原，最终 diff 无该文件）。

## 7. 提交

commit：见 git log（`git log -1 --format=%H`，分支 `tmp/rfc0013-interrupt`，未 push）。
