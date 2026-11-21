# f12 · core attach / replay 四个已知缺陷修复

基线 `0025c2b`（`dev_gingshan`），分支 `tmp/f12-attach`。修的是
`batch-results/f9-audit-core-attach.md` 点名的 P1-1 / P1-2 / P0-1 / P1-3。
只动 `core/vade/session/{replay,models,manager}.py` 与契约注记；未改 C-05 方法表、
未加新方法、未改任何既有字段名。

## 1. P1-1 · `last_seq > newest` 不再算「覆盖」

**改法**：`ReplayBuffer.covers()` 加**上界**：`oldest <= last_seq+1` **且**
`last_seq <= newest`；`newest` 取 `store.last_seq()`（`manager.attach` 传入
`durable_last_seq`），不是 `buf[-1]` —— replay 窗口不收 `coalesce` 行
（RFC-0007 §9.4），用 buffer 的 newest 会把「游标停在 `task.progress` 上」的正常重连
误判成 gap，每次都回快照。

越界一律走快照：客户端拿到 `snapshot` + `session.attached{snapshot:true}`，
`from_seq` = 快照点，不再是「`snapshot:null` + 零条补发 + 一个永远满足不了的 from_seq」。

## 2. P1-2 · 空 buffer 一律快照（重启 vs 全新会话）

`covers()` 对空 buffer 返回 `False`，**不分**「全新会话」和「重启后」：

- 重启后的 buffer 从空开始，replay-only 的 attach 拿不到「只落库、不落事件」的状态
  （实证是 `session/model.set`）；
- `store.last_seq == 0` 也救不了这一支：`create_session()` + `set_session_model()`
  就是一个「有状态、零事件」的会话；
- 全新会话走快照的代价近似为零（空消息树），而 replay 零条时 `snapshot:null` 是
  「已同步」的错误承诺。

顺带消掉审计里那条 DoS：`last_seq=0` 不再先 `read_since(sid, 0)` 把全部历史读进内存。

## 3. P0-1 · 快照有界：先去掉三份重复文本，再截断并如实上报

**去重**（`Message.to_snapshot` / `Turn.to_snapshot`，只作用于 `ui/attach` 的
`snapshot`，`to_dict()` 原样保留给进程内调用方）：

| 字段 | 改前 | 改后 |
|---|---|---|
| `messages[].content` | 总是有 | **总是有**（每个客户端都读它，不删） |
| `messages[].parts` | 总是有（`wire_parts()` 为老行合成 text part） | 只有含非 `text` part 时才带；纯文本的 `parts` 就是 `content` 的投影 |
| `turns[].user_text` | 总是有（用户消息的第 3 份） | 不带（`messages[]` 里那条 + `user_message_id` 可关联） |

**有界**（`SessionLog._bound_snapshot`，预算 = `MAX_REPLY_BYTES - SNAPSHOT_ENVELOPE_RESERVE`
= 65519 − 2048，按 `_encode_json` 同款编码实测整个 `{session_id,from_seq,snapshot}` 包装）：

1. 丢最旧的 `messages[]`（最新的那条永不丢，留给下一步截断）；
2. 仍超 → 丢最旧的 `turns[]`；
3. 仍超 → 对最大的文本字段折半截断（完成的 message 留头，`running_partial` 留尾，
   因为后续 delta 会接在尾部）；
4. 仍超 → 丢 `tasks` / `pending_inputs` / `pending_approvals`；
5. 兜底：清空可选块。**任何路径都不会返回 `PAYLOAD_TOO_LARGE`。**

每次截断都写：`snapshot.truncated`、`snapshot.omitted`（丢了多少条 message）、
`snapshot.omitted_turns`、`snapshot.truncated_before_seq`（现存最旧 message 的 seq；
全丢光时是 `from_seq+1`）、`snapshot.content_truncated`，行级 `truncated`/`omitted_chars`，
并写一条 `session.snapshot_truncated` 审计（`log.audit` 未接线时至少 `WARNING`）。

## 4. P1-3 · `snapshot.running_partial`

`snapshot()` 从 `queue.state()["running"]` 取正在跑的 turn，从进程内累加器
`partial_text()` 取已流出正文，输出 `{turn_id, text, chars}`（空闲时 `null`；
被截断时多 `truncated`/`omitted_chars`，`chars` = 实际下发字数）。
进行中 attach 的客户端现在能先画出前半段，再把后续 delta 接上去。

## 5. 契约注记

- `schema/rpc.json` `ui/attach.notes` 追加一段（C-05 1.7.0 不变；纯 note，无
  append 块、无方法/字段增删）：上界语义、空 buffer 语义、五个截断字段与审计、
  文本只出现一次、`running_partial`。
- `docs/agents/22-C3-session-loop.md` §T1 规则补 4 条，§五 验收表补 3 行。
- `tools/check_contracts.py` / `docs/check_docs.py` / `tools/check_ledger.py` 全绿
  （append 计数未变，故 LEDGER 不动）。

## 6. 测试

新增 `core/tests/session/test_attach_bounds.py`（12 例）：

| 例 | 断言 |
|---|---|
| `test_last_seq_beyond_the_newest_event_is_answered_with_a_snapshot` | `last_seq=999`（只有 1..3）→ `snapshot` 非空、`replay==()`、`from_seq==3` |
| `test_last_seq_equal_to_the_newest_event_is_still_covered` | 正常「已追平」不被误伤 |
| `test_a_coalescible_tail_does_not_force_a_snapshot` | 尾行是 `task.progress` 时仍走 replay |
| `test_restart_empty_buffer_with_last_seq_zero_answers_with_a_snapshot` | 重启 + `last_seq=0` → 快照里能看见 `session/model.set` 的 `openai/gpt-4o` |
| `test_a_brand_new_empty_session_also_answers_with_a_snapshot` | 全新空会话同样快照（零事件不是「没有状态」的证明） |
| `test_an_in_process_replay_at_zero_still_works` | 进程内非空 buffer 的 `last_seq=0` 仍补发 4 条 |
| `test_a_long_session_attaches_with_a_truncated_snapshot` | 120×1500 字符：`omitted + len(messages) == 120`、`truncated_before_seq == messages[0].seq`、最新一条完整、整包 ≤ 65519、审计 1 行 |
| `test_one_oversized_message_is_clipped_not_refused` | 单条 32 000 汉字：`content_truncated`、行级 `truncated`/`omitted_chars`、保留开头、整包 ≤ 65519 |
| `test_the_snapshot_carries_each_text_once` | `content` 在、纯文本 `parts` 不在、`turns[].user_text` 不在；`to_dict()` 仍是旧的完整形状 |
| `test_a_mid_turn_snapshot_carries_the_streamed_partial` | 3 个 delta 后 `running_partial.text == "这是已经生成的前半段"`，且该轮尚无 assistant 行 |
| `test_an_idle_snapshot_reports_no_running_partial` | 空闲为 `null` |
| `test_a_running_partial_bigger_than_the_frame_is_clipped` | 32 000 字 partial 截尾、`chars` 与实际一致、整包 ≤ 65519 |

改了两个既有用例（它们锁的是旧行为）：

- `core/tests/e2e/test_turn_lifecycle.py::test_replay_from_a_restarted_core_reconstructs_the_call_chain`
  → `test_a_restart_snapshot_reconstructs_the_call_chain`：重启后 `attach(0)` 断言
  `snapshot` 非空且消息链 `user/assistant/tool/assistant` 完整（原来断言 `snapshot is None`
  + 全量 replay，正是 P1-2 的缺陷本身）。
- `core/tests/session/test_parts.py::test_legacy_row_appears_in_the_snapshot_with_a_synthesised_part`
  → `..._as_plain_content`：老行在 `snapshot` 里以 `content` 呈现、不带合成的 `parts`；
  `wire_parts()` 的合成逻辑本身没变。

## 7. 验证

本机（ARM，`libvade_protocol.so` 未构建，按约定不跑 Rust 构建）：

- `python -m pytest core/tests/session -q` → **240 passed**
- `python -m pytest tests/session tests/task tests/input tests/events tests/model tests/e2e/test_turn_lifecycle.py -q` → 见下（全绿）
- `tools/check_contracts.py` / `docs/check_docs.py` / `tools/check_ledger.py` → 三个 exit 0

全量 `pytest core/tests` 里有 21 failed / 23 errors，**全部是环境项**，与本批无关：

- 23 errors + 17 failed：`libvade_protocol.so` 不存在（`test_live_server` /
  `test_core_wiring` / `test_c3d_registration_chain` / `test_shutdown_restart` /
  `test_layered_wiring` / `test_framing` / `test_handshake_wiring` /
  `test_device_identity` / `test_admin_listener` / `test_pair_banner` /
  `test_ui_modules_ws` / `test_blob_token_ws`），CI 在 pytest 之前 `cargo build` 这一层；
- 4 failed：`test_wal_checkpoint`（p95 21.2ms vs 10ms 上限，2 例）与
  `blob/test_review_fixes::test_the_periodic_sweeper...`（定时器/磁盘），都是本机
  ARM + 慢盘的计时用例，且不经过 attach/snapshot 路径。

## 8. 本批未做（审计里的其余项）

P1-4（重启时从 `events` 重建 partial 正文）、P1-5（超大单事件静默 drop sink）、
P1-6（attach emit 失败回滚）、P2-1..P2-6 均未触碰，留给后续批次。
