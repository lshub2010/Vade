# 设备身份接到 `Connection` · RESULT

分支 `tmp/device-id`，基于 `58e9e66`。**两条路径都完成**。

## 问题

`transport/server.py` 构造 `Connection` 时没传 `device_id`，而
`session/rpc.py:112` 早已在 `getattr(connection, "device_id", None)`。
真实连接上一律 `None` → 带 `file_id` 的 `ui/submit` 恒答
`PERMISSION_DENIED / data.reason="device_unknown"`（`parts.py:494`）。
fail-closed 是对的，功能等于没开通：设备提交**自己刚上传**的文件也被拒。

## 改动

| 文件 | 内容 |
|---|---|
| `transport/connection.py` | `Connection(..., device_id: str \| None = None)`，存 `self.device_id`；新增 `on_established` 钩子，在 `run()` 里**握手完成之后**（`browser_session` 则立即可用）调用一次。钩子抛异常只记 warning，不让已认证的 socket 崩掉——此时 `device_id` 保持 `None`，fail-closed 而非 fail-open。 |
| `transport/server.py` | `_ws`：cookie 命中且未 `revoked` 才从 `self._sessions[token]["device_id"]` 取值；吊销行按"查不到"同一条路径处理（`session = None`）。`CoreApp(whitelist=...)` 新增可选参数；`_bind_device_identity` 从 `crypto.remote_fingerprint` 反查 whitelist。 |
| `identity/whitelist.py` | `device_for_fingerprint(fp) -> str \| None`，返回 `module_id#instance_id`。 |
| `__main__.py` | `_serve` 把 `app.whitelist` 传给 `CoreApp`（不传则路径 2 静默失效）。 |
| `core/tests/transport/test_device_identity.py`（新） | 5 用例。 |
| `core/tests/transport/test_live_server.py` | +1 真 socket 用例；fixture 补 `whitelist=app.whitelist`。 |

### 路径 1 · 浏览器会话（cookie `__Host-ac_ui`）

值就是 `/ui/session`（`server.py:217`）发出、存在服务端会话表里的 `ui-<16hex>`。
cookie 只是**查表的钥匙**，不是身份本身：token 不在表里或 `revoked=True` →
`device_id is None`，且 `browser` 也是 False（该 socket 退回原生路径）。
**没有**从 query、header、JSON-RPC 字段读任何设备标识。

### 路径 2 · Noise 原生连接

`CryptoSession.read_handshake` 在**发 msg2 之前**就算好 `remote_static` 的
fp8 并过 whitelist（`crypto_gateway/session.py:155-165`），所以
`on_established` 被调用时该 key 已被信任；whitelist 再回答"这个 key 属于哪次配对"。

映射依据：现有 identity 层**只有 module 概念**（`Entry(module_id, instance_id,
fingerprint, public_key)`），没有独立的 UI 设备表，也没有"原生 UI 客户端"的配对
流程。因此用的是 core 里既有的规范名 `module_id#instance_id`——与
`Registration.session_id`、`Registry` 的节点 id 同一口径。同一 key 绑多个
instance 时取排序最小的一个，保证重启后答案稳定。

查不到 → 保持 `None`。注意这条在**网关层**就已经成立：未列入 whitelist 的 key
根本拿不到 msg2（`PERMISSION_DENIED`，连接关闭），所以"握手成功但查不到配对"
只有 whitelist 在握手后被改的竞态才可能出现，`_bind_device_identity` 仍按
`None` 处理，不做兜底猜测。

## 测试

`core/tests/transport/test_device_identity.py`

| 用例 | 断言 |
|---|---|
| `test_the_cookie_socket_is_bound_to_the_device_id_ui_session_returned` | 真 `POST /ui/session` → 真 `/ws` scope；`connection.device_id == 返回的 ui-…`；该设备**自己**的 `file_id` 能过 `ui/submit`（`queue_pos == 1`），`BlobService` 是真实例、blob 行直接入库 |
| `test_a_revoked_cookie_grants_no_identity_and_its_file_submit_is_denied` | `revoke_session` 后同一 cookie：`browser_session is False`、`device_id is None`；`ui/submit` → `PERMISSION_DENIED` + `{field: content[1].file_id, reason: device_unknown}`；库里无 turn |
| `test_a_noise_socket_is_bound_to_the_device_that_owns_the_verified_key` | **真** `vade_protocol` IK 握手（`CandidateKeySession` + 真 whitelist）；`device_id == "com.example.fs#fs-01"` |
| `test_an_unpaired_fingerprint_stays_anonymous_and_fail_closed` | 已认证但 fingerprint 无配对：`device_id is None`，`file_id` 提交仍 `device_unknown` |
| `test_one_key_paired_twice_resolves_to_one_canonical_name` | 同 key 两个 instance → 稳定取最小；未配对 fp → `None` |

`core/tests/transport/test_live_server.py::test_a_real_cookie_socket_carries_the_device_identity`
—— uvicorn 真 listener、真 TCP/WS，断言线上连接的 `device_id` 等于
`/ui/session` 的返回值。

**反向验证**：把 `device_id=device_id` 改回 `None`、`on_established` 置空后重跑
`core/tests/transport` → 3 failed（上面三条主用例），恢复后 32 passed。即这些
用例真的在测这次修复，不是跟着实现写的。

## 门禁（本机，`.venv/Scripts/python.exe`）

```
$ python -m pytest core/tests -q
555 passed, 6 warnings in 33.05s          # 基线 549 + 6

$ python tools/check_contracts.py
contracts: OK (C-03/C-04/C-05/C-06/C-18 frozen, 33 error codes, config metadata complete, event gate green)

$ python docs/check_docs.py
合计 12 项，失败 0 项。
```

## 未做 / 后续

1. **`ui/blob.token`（RFC-0004 §4.5）仍缺**。原生连接现在有了 `device_id`，
   但换 600 s 设备 token 的 RPC 还没实现，所以原生客户端仍不能走 `/ui/blob*`
   ——上传通道只认 cookie 会话（`blob/auth.py` 从 `_sessions` 取 `device_id`）。
   接上它之后，原生侧才算端到端可用。
2. **模块设备名与 UI 设备名不同源**。路径 2 拿到的是 `module_id#instance_id`，
   路径 1 是 `ui-…`，两者在 blob 归属表里是不同字符串。原生 UI 若要与浏览器
   设备共享附件归属，需要一个"原生设备 ↔ ui- 设备"的映射表；现有 identity
   层没有这个概念，本批不发明它。
3. **`Connection.device_id` 可被赋值**（普通属性，非 property）。没有做成
   read-only 是因为 `_bind_device_identity` 要在握手后写；若日后要防内部误写，
   可改为 `_device_id` + 只读 property + 内部 setter。
4. **`ui/blob.token` 之前的审计字段**：`ui/submit` 的审计仍不带 `file_ids[]`
   （RFC-0004 §11 审计条目），属独立批次。
