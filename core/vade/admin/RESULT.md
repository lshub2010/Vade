# C5a 结果：管理平面 + 审计 + 审批（结果 / 偏离 / 未验证）

分支 `tmp/c5a-admin`。本文件记录这一批**实际做到了什么**，以及**没做到什么**——不含"计划"。

## 1. 实现范围

### 管理平面 `core/vade/admin/`（约 3.8k 行，含 policy/audit）

| 文件 | 职责 |
|---|---|
| `app.py` | 唯一 ASGI 应用：`/admin`、`/admin/setup`、`/admin/api/*`。26 条业务路由 + 4 条 setup/bind 路由，全部走同一套 `guard()`（Host / Origin / Sec-Fetch-Site / 会话） |
| `auth.py` | 密码策略（≥12 字符、弱口令表、字符多样性）、口令哈希、`ChallengeStore`（一次性、绑定 IP、TTL）、`SessionStore`（access/refresh、`logout-all` 撤销）、`origin_allowed` / `is_loopback` |
| `devices.py` | `DeviceStore`：设备公钥登记、撤销；列表输出不返回公钥 |
| `identity.py` | 核心身份密钥、`fp8`（16 hex 字符），持久化在 `data/`，重复读取稳定 |
| `setup.py` / `setup_token.py` | 初始化流程、bind ticket、setup token（mask/rotate） |
| `server.py` | 独立监听器的 uvicorn 装配（`install(app)` 不开监听器） |
| `stream.py` | SSE `/admin/api/stream`（Last-Event-ID 续传） |
| `views.py` | `to_public_view`：节点数据的唯一出口（白名单字段；`endpoint` 去 query，防 `?token=` 泄漏） |
| `cli.py` | `python -m vade admin <cmd>` 11 个子命令：`print-fingerprint`、`print-setup-token`、`rotate-setup-token`、`reset-password`、`reset`、`proxy`、`print-tls-fingerprint`、`trust-key`、`revoke-device`、`issue-bind-ticket`、`init`。全部本地读写 `data/`，无远程调用；`proxy` 无签名客户端，显式非零退出而非假装可用 |

会话模型：`device`（bearer/签名）有全权；`browser`（`__Host-ac_admin` cookie）**只读**——非 GET 业务操作 403。浏览器没有原始签名能力，拿不到第二因子，因此不给写权限。

### 策略与审批 `core/vade/policy/`

- `engine.py`：风险判定。manifest 的声明只是起点；`shell|write|delete|forget|exec` 硬下限 `high`；覆盖只能升不能降（`RISK_DOWNGRADE_FORBIDDEN`）；未知风险字符串按 `high` 处理（fail closed）。
- `approval.py`：挂起/超时/会话授权。超时即拒绝（无"默认同意"、无 TTY 提示）；只有 `device_key` / `loopback_admin` 可批准，`browser_session` 一律 `APPROVER_INVALID`；会话授权按 `(session_id, tool_id, args_digest)` 键控（只按工具键控会让一次 `ls` 解锁 `rm -rf /`）；通用执行器（`shell`/`exec`/…）永远只能 `once`。
- `adapter.py`：`PolicyAdapter`，给 C3 loop 用。

### 审计 `core/vade/audit/`

`writer.py` 追加写、按天分表、`BEFORE UPDATE/DELETE` 触发器 `RAISE(ABORT)`（无 update/delete 方法）、入库前 `redact_args`；`reader.py` 分页查询；`redact.py` 参数摘要/脱敏；`retention.py` 保留策略。

### 其它

- `schema/admin-api.yaml`（管理 API 契约）、`schema/device-bind.json`。
- `core/vade/__main__.py`：`admin` 子命令转发到 C5a CLI（见 §5）。

### 测试

`core/tests/{admin,audit,policy}/` 共 39 条：admin 21、policy 11、audit 7。

## 2. 未实现（返回 501 `UNSUPPORTED` 的端点）

skills 与 memory 两组属于 C5b。它们在 `schema/admin-api.yaml` 里已经声明，所以本批**对契约中的每一种形状**都返回 501（而不是 404，404 会让 UI 无法区分"本版本没有"和"路径写错了"）：

```
GET|POST|PATCH|PUT|DELETE
  /admin/api/skills
  /admin/api/skills/install
  /admin/api/skills/install/preview
  /admin/api/skills/{name}
  /admin/api/skills/{name}/approve
  /admin/api/skills/{name}/reject
  /admin/api/skills/{name}/rollback
  /admin/api/memory
  /admin/api/memory/stats
  /admin/api/memory/{id}
  /admin/api/memory/{id}/promote
  /admin/api/memory/{id}/archive
  /admin/api/memory/{id}/restore
```

对应的能力（skill 安装/审批/回滚、memory 提升/归档/恢复）**没有实现**，只有路由占位。

## 3. 偏离：scrypt 代替 Argon2id

设计稿要求 `argon2id(normalize(password))`。本仓库依赖集里没有 `argon2-cffi`，也不 vendor wheel，所以 `core/vade/admin/auth.py` 用 `hashlib.scrypt`（标准库、内存硬）代替：

```
scrypt$<n>$<r>$<p>$<base64 salt>$<base64 digest>
n = 2**14 (16 MiB), r = 8, p = 1, dklen = 32, salt = 16 字节随机
```

- 参数与算法前缀**随哈希一起存储**，`verify_password` 按存储里的参数校验，因此后续换 Argon2id 不会让已有口令失效（换算法只需在 `hash_password` 加分支并重算）。
- `n=2**14` 而非 `2**15`：OpenSSL 的 scrypt 有默认内存上限，`2**15` 会被拒绝；`identity/pairing.py` 里的配对码哈希也用 `2**14`，两处一致。
- 与 Argon2id 的差距是**真实存在的**：scrypt 没有 Argon2id 的抗侧信道/抗 GPU 调参特性，`2**14` 也低于 OWASP 当前对 Argon2id 的建议档位。这是"依赖不可得"下的诚实降级，不是等价替换。

同样的偏离在 `core/vade/identity/pairing.py`（配对码哈希）里重复出现，理由相同。

## 4. 测试数字

- `python -m pytest core/tests -q`：连续 3 次 **全绿**（见 §6 的 3 次汇总；代码冻结后跑，非中途取样）。
- 审批套件压测：`test_approval.py` 单独连跑 20 次，20/20 通过（含单次耗时 4–5s 的机器停顿）。
- 新增两条回归测试：`test_a_timed_out_request_leaves_nothing_pending`（超时后不留悬挂请求）、`test_unimplemented_feature_endpoints_answer_501_not_404`（13 条未实现路径全部 501）。

## 5. 本轮修掉的问题

1. **审批超时竞态（跨用例"污染"的真身）**：`ApprovalManager.request` 用 `asyncio.wait_for(pending.future, …)`，超时会把 future **取消**；取消与实际出队之间有一个窗口，`decide()` 在这个窗口里看到 `future.done()` 却仍在 `_pending`，于是抛 `INVALID_ARGUMENT: approval request already resolved`。机器一停顿（单次跑 6s 的那种），`timeout_s=1.0` 的用例就会在 `create_task` 与 `decide` 之间被超时命中——所以每次失败的用例都不一样，单跑又是绿的。
   修法：`await asyncio.wait_for(asyncio.shield(pending.future), …)`（截止时间不再碰审批 future），超时分支在 `decide()` 用的**同一把锁**里收回请求；若批准与超时撞在同一拍，以真实决定为准。另补 `CancelledError` 分支，避免 turn 被取消后请求悬挂。
2. **测试依赖真实等待**：把 `await asyncio.sleep(0.01)` + `[pending] = manager.pending_requests()` 换成轮询 `_pending()`；决策驱动的用例把截止时间放到 30s（只有"断言超时"的那一步才缩到 50ms）。测试不再拿调度抖动当被测行为。
3. **`python -m vade admin --help` 退出码 2**：顶层 `admin` 子解析器 `add_help=False` + `REMAINDER`，argparse 在 REMAINDER 开始前拒绝 `--help`。改为显式接受 `-h/--help` 并转发给 C5a 自己的解析器。
4. **skills/memory 嵌套路径返回 404**：`{name}`/`{id}` 只匹配一层，`/skills/{name}/approve`、`/memory/{id}/promote` 等落到 404。补齐 13 条路径，统一 501。

## 6. 验证记录

```
$ python -m vade admin print-fingerprint --data-dir /tmp/x
12d4ae2c5fdbff07          # 同一 data-dir 重复执行，值不变
$ python -m vade admin --help           # exit 0，列出 11 个子命令
$ python -m vade admin print-fingerprint --help   # exit 0
```

三次全量（代码冻结后连续执行，非中途取样）：

```
$ python -m pytest core/tests -q
run 1: 297 passed, 8 warnings in 107.94s
run 2: 297 passed, 8 warnings in 117.71s
run 3: 297 passed, 8 warnings in  99.82s
```

## 7. unverified（没验证的，别当已验证）

- **真实 TLS / 真实设备**：所有 admin 测试走 `httpx.ASGITransport`，没有起真 uvicorn + 真证书；`/admin` 页面在真浏览器里没点过，`print-tls-fingerprint` 只用证书文件验证过。
- **浏览器只读语义的对抗面**：XSS/CSRF 只覆盖了 Origin / Sec-Fetch-Site / Host 的拒绝路径，没有真浏览器跨站复现。
- **多 UI 并发审批**：只在单进程单事件循环里验证"两端同时收到"。多进程/多副本下 `_pending` 是进程内状态，未验证。
- **SSE 断线重连**：`Last-Event-ID` 续传有实现，没在真代理（缓冲/超时）后面验证。
- **scrypt→Argon2id 迁移**：只验证了参数随哈希存储、旧哈希仍可校验；迁移脚本未写、未测。
- **`admin reset` / `init` 与运行中 core 的互斥**：只依赖 `data/core.lock` 的 flock 分支，未做真实并发启动测试。
- **压力/长跑**：没有 24h 长跑或大审计库（百万行）分页性能数据。
