# c2-attach 批次结果

## 任务 #4：`ui/attach` 重入去重（不泄漏 sink、不双投递）

### 缺陷

`SessionRPC.ui_attach` 每次调用都走 `SessionLog.attach()`，后者无条件 `_register_sink()`
新建一个 `SinkWorker`；`SessionRPC._sinks` 只是把新的 sink id **覆盖**到
`(connection_id, session_id)` 上。于是：

| 后果 | 机制 |
|---|---|
| 双投递 | 旧 worker 仍在 `SessionLog._sinks[session_id]` 里，`_broadcast()`/`emit()` 把它算作一个在线 sink，每个事件往**同一个 socket** 写两遍。重入时它自己的 `session.attached` 就是最便宜的见证（实测帧序列 `[1, 2, 3, 3]`） |
| 连接级泄漏 | 旧 worker 的 id 已经不在 `SessionRPC._sinks` 里，`ui/detach` 只能停最新那个；旧 worker 带着 writer task 和 256 格队列活到连接结束。每重入一次多一个 |
| 计数失真 | `attached_count()` / `attached_sessions()` / `status.ModuleStatusPublisher` 的扇出都按 worker 数算，一个客户端被当成两个 |

### 改法（只动 `core/vade/session/rpc.py`）

1. `ui_attach` 在 `log.attach()` 之前先 `_retire_sink(connection_key, session_id)`：
   从 `SessionRPC._sinks` 摘下旧 id，再从 `SessionLog._sinks[session_id]` 取出对应
   worker 调 `drop()`（取消 writer task + 触发 manager 侧 `on_drop` 注销 + 丢弃积压）。
   **顺序是先退旧、再注册新**——反过来 `log.attach()` 内部发出的 `session.attached`
   会先广播给旧 worker，仍然双投递。
2. 用 `drop()` 而不是 `SessionLog.detach()`（= `SinkWorker.stop()`）：`stop()` 把停止
   哨兵**排在队列尾部**，writer 会先把积压帧全部发出去，而本次 attach 的
   replay/snapshot 正要重发那些 seq —— 客户端会看到同一 seq 两遍。`drop()` 取消 writer
   并丢队列，重放成为该 seq 的唯一来源。这一条有专门的测试守着（见 ②b）。
3. `_connection_key()` 收口 `_sinks` 的键（`ui_attach` / `ui_detach` 同一口径），
   行为不变，只是不再两处各写一遍 `str(getattr(...))`。
4. `session_id is None`（自动建会话）不需要退旧：新会话 id 不可能已有该连接的 sink。
   跨连接、跨会话的 sink 一律不碰——去重范围严格是 `(connection, session)` 这一对。

### 测试（新增 `core/tests/session/test_attach_reentry.py`，5 例）

| 例 | 断言 | 反证（改前 / 变更后） |
|---|---|---|
| ① `test_reattach_retires_the_previous_worker` | 二次 attach：`_register_sink` 每次 attach 只被调 1 次，存活 sink 仍为 1，旧 worker `dropped=True`、task 结束、id 不在 `log._sinks` | 改前 `attached_count == 2` |
| ② `test_reattach_delivers_each_seq_once` | 以客户端实际收到的 `last_seq` 重入，帧序列单调且无重复；`session.attached` 恰好 2 条（每次 attach 一条，不是每个 worker 一条） | 改前 `[1, 2, 3, 3]` |
| ②b `test_reattach_does_not_flush_the_stale_workers_backlog` | 卡住旧 writer 让一帧停在队列里，再以 `last_seq=1` 重入并放行旧 writer：帧必须是 `[1, 2, 3]`（replay 的 2 + attached 的 3，各一次） | 把 `drop()` 换成 `detach()`（stop）→ 红：`[1, 2, 2, 3]` |
| ③ `test_detach_zeroes_the_sink_count` | 重入后 `ui/detach` → `attached_count == 0`，之后该会话的事件不再到达 socket；第二次 detach 仍是 `NOT_FOUND`（C-05 既有语义不变） | 改前 detach 后仍剩 1 个 worker |
| ④ `test_reattach_only_touches_its_own_connection_and_session` | A 重入不影响 B 的 sink，也不影响 A 在**另一个会话**上的 sink；失败的 attach（未知会话 `NOT_FOUND`）不改动既有挂载 | 改前 `attached_count == 3` |

验证（未跑全量 pytest）：

- `python -m pytest core/tests/session/test_attach_reentry.py -q` → **5 passed**
- `python -m pytest core/tests/session -q` → **199 passed**
- `python -m pytest core/tests/session core/tests/transport core/tests/uipage core/tests/model -q` → **420 passed**
- `python -m pytest core/tests/integration core/tests/transport/test_live_server.py -q` → **26 passed**（含真 WS `ui/attach → submit → detach → 二次 detach=NOT_FOUND`）
- `python -m pytest core/tests/e2e -q` → **6 passed**
- 变异验证：把 `_retire_sink` 的 `drop()` 换成 `await log.detach(...)`（优雅 stop）→ ① 与 ②b 变红；把 `rpc.py` 退回改前版本（`git show HEAD~1:…`）→ **5 例全红**（含 `duplicate seq on the wire: [1, 2, 3, 3]`）。

### 环境备注

`core/tests/integration`、`test_live_server` 在本机原先因缺
`protocol/core/python/libvade_protocol.so` 报 `OSError`（3 failed + 14 errors，已用
`git stash` 证明与本次改动无关）。本棒按 `cargo build --offline --features uniffi`
重建并拷到 `protocol/core/python/`（`*.so` 在 .gitignore 内，未入库）后全部转绿。

### 遗留（不在本棒文件范围）

1. **连接断开时的 sink 清理**：`ui/detach` 之外的路径（socket 直接关闭）现在依赖
   `SinkWorker._writer` 下次写入失败时 `drop()` 自愈；在那之前 `SessionRPC._sinks`
   与 `SessionLog._sinks` 各留一条。真正的兜底要挂在 `Connection._on_close` /
   `app.on_disconnect` 上，属于 `session/__init__.py` 的接线改动，本棒未动。
2. `SessionRPC._sinks` 与 `SessionLog._sinks` 仍是两张表（rpc 只存 id）。若后续允许
   一个连接同时挂多个会话，建议由 manager 提供 `drop_sink(session_id, sink_id)` 公开
   方法，rpc 不再直接读 `log._sinks`。
