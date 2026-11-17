# e2-bindtxn 批次结果

修复审 P1-2（`sessions.issue` 在事务外）+ P2-1/P2-2（首启时 `admin.json` 已写、`devices.add` 失败只回错误）+ 复审点名的 admin 层请求体上限。

## 改了什么

| 文件 | 内容 |
|---|---|
| `core/vade/admin/bind.py` | `sessions.issue` 移进 `consume_ticket` 之后的同一个 `try`；新增 `_rollback()`，按「设备 discard → admin.json 回滚 → `release_ticket`」倒序撤销，票最后放且无条件放；模块 docstring 补一条「提交全或无」的排序规则。 |
| `core/vade/admin/setup.py` | 新增 `InitializeSnapshot` 与 `begin_initialize_bound()` / `rollback_initialize()`：写 `admin.json` 前快照 `admin.json` 字节 + `setup.token`，回滚时**先恢复 token、再删/还原 `admin.json`**。`_write_admin` 抽出 `_atomic_write(path, raw)`（同一个 tmp + `os.replace` 0600 写法），供还原复用。 |
| `core/vade/admin/devices.py` | 新增 `DeviceStore.discard(device_id)`：事务回滚专用，只删设备行，**不**写 revoked。 |
| `core/vade/admin/app.py` | `MAX_BODY_BYTES = 1 MiB` + `_declared_too_large()` / `_too_large()`。`GuardedApp` 在路由之前用 `Content-Length` 直接 413；`_json` 改成流式读取并在超限时中止（413）。 |
| `core/tests/admin/test_bind.py` | +7 例，见下。 |

## 关键行为（可直接对上验收）

1. **票不白丢对一切异常成立**。`complete()` 的提交段现在是：`consume_ticket` →（首启）快照 + 写 `admin.json` → `devices.add` → `sessions.issue`，整段包在 `except BaseException` 里。`issue` 抛任何异常都走回滚，客户端拿到 500 `INTERNAL`，而票回到未消费状态——同一张二维码重试即成功，不会出现「设备已登记但没人拿到 token」。
2. **设备回收用 discard，不是 revoke**。`revoke()` 会把设备键写进 `devices.json` 的 `revoked`（§7.12.3 ④d 永久拒绝），回滚用它等于自己把重试拦死。`discard()` 只删设备行，`_save()` 同时写 device 列表与 revoked 集合，外部观察不到中间态。
3. **`release_ticket` 最后且无条件**。前两步各自 best-effort（失败只记审计，不替换原始异常），因为票能否重试是二维码能否救活的唯一开关。回滚步骤失败的审计：`kind=device.bound`、`status=denied`、`code=INTERNAL`、`extra.steps=["device"|"admin"]`、`extra.reason="bind rollback incomplete"`。
4. **首启失败选「回滚」而不是「已初始化但绑定失败」**（P2-1/P2-2 的二选一，这里写清）。理由：`setup_required=true` 时 `admin.json` 本来不存在、`setup.token` 还在；回滚能把状态还原成「未初始化 + token 可用 + 票可用」，用户重扫同一张码即可，而「已初始化、零设备、token 已消费」只剩 `vade admin reset` 一条路（浏览器会话只读，无法自助加设备）。回滚顺序是先恢复 token 再删 `admin.json`：崩在两步之间只会留下「已初始化 + 多一个 token」（`admin.json` 存在时 token 被忽略、下次启动 `ensure_setup_token()` 会删掉），反向顺序会留下「没密码没 token」，正是 `initialize()` 注释里要避免的状态。
5. **step-up 路径不碰密码**。已经初始化时 `init_snapshot is None`，回滚只回收设备 + 放票，`admin.json` 字节级不变（测试直接比对文件 bytes）。
6. **1 MiB 上限两层**。`GuardedApp` 在 Host 检查之后、路由之前读 `Content-Length`，超过就 413 `PAYLOAD_TOO_LARGE`，一个字节都不读——`/admin/setup`、`/admin/setup/unlock`、`/admin/bind` 这些 preauth 路由没有会话可限流，所以不能等 handler。第二层在 `_json`：`request.stream()` 累计字节，chunked 或伪造 `Content-Length` 的 body 在读满 1 MiB 时中止。所有 admin 路由（含 `skills.py` 注入的）都走 `_json`，没有旁路。
7. 413 不写审计：preauth 可达，写审计等于给攻击者一个刷库入口（与现有 ORIGIN_MISMATCH preauth 路径一致）。

## 测试

`core/tests/admin/test_bind.py` 新增 7 例：

| 用例 | 断言 |
|---|---|
| `test_session_issue_failure_releases_the_ticket_and_leaves_no_device` | `issue` 抛 `RuntimeError` → 500 `INTERNAL`；设备数 0、`devices.json` 的 `devices`/`revoked` 都是空；票可 peek；同票重试 200。 |
| `test_devices_add_failure_on_first_run_rolls_back_to_setup_required` | `initialize=False`，`add` 抛 `OSError` → `initialized() is False`、`admin.json` 不存在、`setup.token` 与出错前逐字节相同、票可 peek、设备 0；恢复后同票重试 200，`admin.json` 出现、token 被消费、设备 1。 |
| `test_devices_add_failure_on_the_step_up_path_changes_nothing` | 已初始化，`add` 失败 → `admin.json` bytes 不变、票可 peek、设备 0，重试 200。 |
| `test_a_failing_rollback_step_still_releases_the_ticket` | `issue` 与 `discard` 同时失败 → 仍 500 `INTERNAL`、票可 peek；审计里恰好一行 `device.bound`/`INTERNAL`/`steps=["device"]`。 |
| `test_an_over_cap_body_is_413_before_the_envelope_is_touched` | 1 MiB+ 的合法 JSON → 413 `PAYLOAD_TOO_LARGE`，票未消费、设备 0；1 KiB body 走正常路径（401 `BIND_UNAUTHORIZED`），证明不是一刀切拒绝；未知 `/admin` 路径 + 超大 body 也是 413、同路径小 body 是 404，证明闸门在路由之前。 |
| `test_the_cap_covers_the_unauthenticated_setup_routes` | `/admin/setup/unlock` 超大 body → 413，`admin.json` 未被创建。 |
| `test_chunked_body_over_the_cap_is_refused_without_a_content_length` | 直接 ASGI 调用：`Content-Length: 64` 但流式发出 1 MiB+ → 413（第二层生效）。 |

## 验证

- `python -m pytest core/tests/admin -q` → **62 passed**（改前基线 55，新增 7）。
- `python -m compileall -q core/vade/admin` → 无输出。
- **反向验证**（`git stash` 掉 4 个源文件，给旧 `app.py` 临时补一个只有常量的 `MAX_BODY_BYTES`，否则测试模块 import 直接失败）：跑前 6 例 → **5 failed, 1 passed**。唯一通过的是 step-up 那例——旧代码在该路径本来就只 `release_ticket`，它是对「回滚别把已初始化状态搞坏」的回归护栏，不是复现用例。第 7 例（回滚步骤自身失败）是加在反向验证之后写的，未参与该轮。
- 未跑全量 pytest（按要求）。`core/tests/integration/test_admin_assembly.py` 本机跑不了（缺 `protocol/core/python/libvade_protocol.so`），与 `batch-results/c3-audit.md` 记录的环境限制一致。

## 需要注意 / 后续

- **`devices.py` 超出任务点名的文件清单**（只授权了 bind/app/setup + 测试）。没有 `discard()` 就只能 `revoke()`，而 revoke 会把回滚变成永久拉黑设备键、让「保住票重试」失去意义；从 `bind.py` 里直接动 `DeviceStore._devices`/`_save()` 更糟。新增 5 行、纯回滚语义，特此写明。
- 回滚是 best-effort：磁盘写不进去时设备行可能留下，票仍放回，重试会以 `INVALID_ARGUMENT`（already registered）失败。这是磁盘故障下的降级，不做二次补偿——补偿本身也要写同一块坏盘。
- `initialize_bound()` 保留原样（`begin_initialize_bound` 只是快照 + 调它），外部/测试若直接调它仍是「没有回滚」的语义；生产路径只有 `bind.py` 一处调用，已改。
- 1 MiB 对 `/admin` 全路径生效。将来若要传大 body（例如技能包上传），应按路径放宽上限，而不是把 `MAX_BODY_BYTES` 调大；`GuardedApp` 与 `_json` 两处共用这个常量。
- `_json` 现在用 `request.stream()` 逐块读取。目前所有 admin 路由都是「读完再解析」，没有流式转发的调用方；若以后要接流式 body，需要绕开 `_json` 并自行限长。
