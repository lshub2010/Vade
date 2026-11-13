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

### 2.1 前端 `admin-ui/`：整批未交付（B-4，独立批次）

任务书 T5 要求的管理面 UI（登录页、列表页、审计页、设置页、skill 审核页、`npm run build` 产物入库、窄屏布局、验收截图）**一行都没有交付**：`admin-ui/` 里只有一个占位 `README.md`，本 commit 对 `admin-ui/` 的改动为零。

因此：

- **C5a 的实际范围是「后端 + 契约」，不是「管理面 UI」。** 前端是**独立批次**，需要自己的任务书、验收表和截图要求，**不能算 C5a 已交付**。
- 下游 F1 / U 组不要按"管理面 UI 已就绪"排期；本批能提供的只有 `schema/admin-api.yaml` 契约与 `/admin/api/*` 实现。
- 这条不是"没做完的边角"，而是批次交付项缺失，所以单列在此，而不是塞进 §7 的 unverified。

## 3. 口令哈希：Argon2id（B-2 已修复）

设计稿要求 `Argon2id(m=64 MiB, t=3, p=1)`。初版用 `hashlib.scrypt(n=2**14)` 的理由（"依赖不可得"）不成立——镜像里 `argon2-cffi` 可取。本批已改正：

```
argon2-cffi==25.1.0        # requirements.txt 新增
Argon2id, m=65536 KiB (64 MiB), t=3, p=1, hash_len=32, salt=16 随机字节
存储串（PHC）：$argon2id$v=19$m=65536,t=3,p=1$<salt>$<hash>
```

- **依赖**：`requirements.txt` 增加 `argon2-cffi==25.1.0`，并在注释里标明这是 C5a 的跨目录依赖变更（不是 C2 侧悄悄改的）。
- **新口令**：`hash_password()` 只产出 Argon2id；`password_needs_rehash()` 覆盖"旧 scrypt 串"和"参数低于当前档位的 Argon2id"两种情况。
- **旧哈希兼容**：`verify_password()` 先走 Argon2id，再回退到 `scrypt$n$r$p$salt$digest` 校验。既有 `admin.json` 不会被孤立。
- **迁移是在线的，没有离线脚本**：`SetupService.rehash_password()` 只在密码校验通过后被调用，原子重写 `admin.json`（tmp + `os.replace`、0600），写入 `password_rehashed_at`，并落一条 `admin.password_rehashed` 审计。校验失败不动文件；校验与写入之间若 `/password` 改了哈希，`stored` 不等则放弃升级，不覆盖没验证过的凭据。
- **配对码哈希不动**：`core/vade/identity/pairing.py` 仍用 `scrypt(n=2**14)`。那是短生命周期、一次性的配对码，不是管理员凭据；改它属于 C3b 语义，不在本批范围。

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

### 5.1 审查阻塞项（B-1 / B-2 / B-3）的修复

5. **B-1 · 浏览器会话越权写**：`READONLY_EXEMPT` 移除了 `/admin/api/logout-all`；`change_password` 不再传 `readonly_check=False`。现在 browser 对两条端点都 403 `READ_ONLY_SESSION`，`logout-all` 不会踢掉已登记设备，`/password` 不会改密。另外把豁免面收敛成两个常量（`READONLY_EXEMPT` 精确路径 + `PREAUTH_PREFIXES` 的 setup/bind），由 `readonly_exempt()` 单点判定；`change_password` 对 browser 分支只回 cookie、**永不**把 `access_token`/`refresh_token` 放进响应体（即使 guard 将来被放宽，这条不变量仍在）。新增 4 条测试：打 `logout-all`、打 `/password`、枚举路由表里**所有**写路由 × browser → 403、以及豁免清单与设计稿逐项相等。
6. **B-2 · scrypt → Argon2id**：见 §3；旧 scrypt 哈希可验证并在成功登录时原地升级。
7. **B-3 · `decide()` 默认 fail-closed**：`ApprovalManager.decide(..., actor_kind="")`，不传即 `APPROVER_INVALID`；`PolicyAdapter` 同样不再提供放行侧默认值；C3 的 `session/rpc.py` 显式把 `auth_mode` 映射成 `device_key` / `loopback_admin`（与原有 `auth_mode` 白名单等价，不改变 C-03 语义）。新增 2 条测试：manager 默认拒绝、adapter 默认拒绝；被拒的请求仍保持 pending。

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
