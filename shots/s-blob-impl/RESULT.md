# s-blob-impl · RFC-0016 `ui/blob.token` 落地（契约 + core + 撤销接线 + 验收）

**结论**：RFC-0016 已从「设计提案」落成「可用的铸造通道」。C-05 顶部 **1.6.0 → 1.7.0**，`x-vade.appends` 11 → **12** 块（既有 11 块逐字未动），三处版本钉**只追加** 1.7.0；core 侧 `BlobTokenRPC` 按 modulepage 的双路发布进 `handler_providers`；用户拍板的四条（Q1 绑设备 / Q2 600 s / Q3 可重放 / **Q7 收紧读面**）逐条实现，Q4/Q5/Q6/Q8 按 RFC 默认值执行并在代码注释里逐条点名「这是默认值、不是已拍板」。四道门禁全绿：**pytest 1203 passed / contracts OK / ledger OK / docs 12 项 0 失败**。

- 分支：`tmp/s-blob-impl`（基于 `origin/dev_gingshan` @ `7941b93`）
- 工作树：`D:/HermesWorkspace/Vade-wt/s-blob-impl`
- 交付 commit：**`b182fe0`**（实现 + 本报告同一提交；其后仅一个 docs 提交回填两处 hash，无代码改动）
- 未 push、未合并、未 `--amend`/`rebase`/`reset`
- `schema/events.json`、`schema/errors.json`、`schema/manifest.schema.json`、`schema/config.schema.json`、`sdk/**`：`git diff` 为空，**逐字节未动**（机器证据见 §7）

## 1. 改动清单（12 改 + 4 新，+300 / −42 行，另有 3 个新测试文件）

| 文件 | 改动 |
|---|---|
| `schema/rpc.json` | 顶部 `version` 1.6.0 → **1.7.0**；`directions.ui_to_core` 追加 **`ui/blob.token`**（形状逐键照 RFC §4.1：`params{ttl_s}` / `result{token,expires_at,max_bytes,endpoints{6}}` / 四个复用错误码 / notes）；`x-vade.appends` 追加第 **12** 块（`version:"1.7.0"`、`owner:"C2"`、`adds:["ui/blob.token"]`、`rewrites:"none"`、notes 三条：绑身份 / 只给原生 / 无新码无新事件无新表）；`http_endpoints.auth.device` 注记改为点名铸造方法（见 §6 偏离说明） |
| `core/vade/blob/rpc.py`（新） | `BlobTokenRPC`：`handlers() -> {"ui/blob.token": self.mint}`；`BLOB_TOKEN_ENDPOINTS` 六条路由字面量；`mint()` 校验顺序 = 参数严格 → 附件可用 → 显式只读 → cookie 会话 → 设备身份（顺序写在模块 docstring）；`ttl_s` **拒而不是夹**（bool/float/str/null/越界 → `INVALID_ARGUMENT data.field="ttl_s"`，零写行） |
| `core/vade/blob/__init__.py` | `install()` 拆成三段：数据面装配（原逻辑）+ 禁用时显式 `app.blob=None` + **`_install_token_rpc`**（setdefault 进已存在的 `session_handlers` **并且**总是 append 进 `handler_providers`——blob 按字母序先于 session 装配，provider 路径是真路径；幂等） |
| `core/vade/blob/service.py` | `issue_token` 成功后记一条 `blob.token` 审计（kind/status/device_id/expires_at，**永不写 token 或其哈希**，§5.8-21）；docstring 写明内部夹取只是直调方的兜底、RPC 层严格判定；**`stat()` 收紧（Q7 拍板）**：`kind=="device"` 的 principal 一律要求 `blob.device_id == principal.device_id`，**独立于 `read_scope`**；cookie principal 语义不变 |
| `core/vade/app.py` | ① `_revoke_connection(fingerprint, removed_ids=())`：解绑时对每个被删的 `module#instance` 调 `blob.revoke_device(device_id)`（§5.6-17 / Q4 默认）；② **`make_rpc_handler` 兜 `VadeError`**：provider 发布的 handler 抛 `VadeError` 原先会一路穿透到 `connection.py:170` 直接断 socket（e2e 实测发现），现转成 C-04 错误信封（与 `SessionRPC.dispatch` 同形，`data` 保留） |
| `core/vade/identity/whitelist.py` | `revoke()` 回调签名扩为 **`(fingerprint, removed_ids)`**（RFC §5.6-17 明示的实现选择；返回值不变，仍是 `module#instance` 元组）；docstring 写明契约 |
| `core/vade/identity/__init__.py` | `install()` 里的 `on_revoke` lambda 同步改为双参并**委托 `app._revoke_connection`**（原 lambda 只关连接；现在两条 Whitelist 构造路径都走同一份撤销语义，不再漂移） |
| `tools/check_contracts.py` | 头部说明加第 14 条；三处版本钉**只追加** 1.7.0（1.3.0/1.4.0/1.5.0/1.6.0 原样保留，每处带 `(RFC-0016)`，原文对照见 §3）；新增 **`check_blob_token()`**（形状 / `errors ⊆ C-04` / endpoints 与 `http_endpoints` 别名比对 / 代码侧 `BLOB_TOKEN_ENDPOINTS` 三方一致 / `TOKEN_TTL_*` 常量钉 600/60/900 / `BlobTokenRPC(None).handlers()` 注册 / `auth.device` 点名方法 / append 块版本与 adds / 顶部版本精确 == 1.7.0）；main() 注册；OK 文案加 "RFC-0016 blob token green" |
| `core/tests/session/test_meta.py` `test_rfc0013_partial_persistence.py` `test_rfc0014_search.py` | 三处「C-05 顶部版本」断言 1.6.0 → 1.7.0（只改版本号与相邻注释，断言仍是精确相等） |
| `docs/rfc/LEDGER.md` | §1（版本 1.7.0 / 36+7+6=49 / 12+8+3 块 / 测试数 1203 及出处）；§2 RFC-0016 行 → **已实现** + impl 路径 + 测试数；§3 facts 块同步；§4-10 新增（登记+实现同批、门禁咬合六变异实测记录） |
| `docs/status.md` | 附录新增 `s-blob-impl` 行 |
| `core/tests/blob/test_rpc_token.py`（新，32 例） | A1–A11 的 core 侧承重 + 拍板 Q1/Q3/Q7 专项（详见 §4） |
| `core/tests/identity/test_revoke_purges_tokens.py`（新，5 例） | A9 的「解绑即失效」腿：真 `App._revoke_connection` + 真 `Whitelist.revoke` + 真 `BlobService`，多实例指纹、旁路设备不受影响、`app.blob=None` 时不炸 |
| `core/tests/e2e/test_blob_token_ws.py`（新，1 例） | 真 uvicorn + 真 Noise + 真 HTTP：FakeModule 调 `ui/blob.token` → 用铸出的 bearer 跑完 begin→PUT→complete→GET → 同连接 `ui/attach`+`ui/submit`（file_id 过归属闸）→ **全新 cookie 连接** `ui/attach` 取回该 part + cookie 四道闸读 blob 200 + **cookie 调 `ui/blob.token` → `PERMISSION_DENIED cookie_session`（over the wire）** → blobs 行 `pinned=1`、`sha256` 与源一致、`device_id == module#instance`（A12 协议层复现） |

## 2. 用户拍板 → 代码落点（逐条）

| 拍板 | 落点 | 测试 |
|---|---|---|
| Q1 绑设备：只有 whitelist 认证的原生连接能铸；cookie 一律 `PERMISSION_DENIED` | `rpc.py::mint` 以 `connection.browser_session` 判 cookie（§5.3-8，不用动态挂的 `ui_token`）→ `PERMISSION_DENIED reason="cookie_session"`；`connection.device_id is None` → `reason="device_unknown"`；**没有任何参数能指定绑定对象**（未知键直接 `INVALID_ARGUMENT`） | `test_q1_cookie_session_is_refused`、`test_a7_…`、e2e 步骤 5a |
| Q2 600 s，沿用 `TOKEN_TTL_*` | 缺省 600、窗口 60..900，值取自 `service.py` 既有常量；`check_blob_token()` 把三常量钉进门禁 | `test_a4_*`（缺省/60/900/59/901/1.5/true/"600"/null 逐个） |
| Q3 TTL 内可重放 | `get_token` 命中不删行（既有语义），铸一次反复用 | `test_q3_token_is_replayable_within_the_ttl`、e2e 重复请求 |
| **Q7 收紧读面：token 只能读自己设备的 blob** | `service.py::stat()`：`principal.kind=="device"` ⇒ 强制 `blob.device_id == principal.device_id`，**不看 `read_scope`**；越权 `PERMISSION_DENIED "file belongs to another device"`（fail-closed：只有两侧一致才放行）。cookie principal 与 `read_scope="device"` 运维开关语义不变 | `test_q7_token_reads_are_device_scoped_regardless_of_read_scope`、`test_q7_cookie_reads_keep_the_history_semantics` |
| Q4/Q5/Q6/Q8 按 RFC 默认 | Q4：解绑清 token（`whitelist.revoke` 回调扩为 `(fingerprint, removed_ids)`，`app._revoke_connection` 调 `revoke_device`；两处 Whitelist 构造同步改）。Q5：持有即可用（bearer 本义）。Q6：严格参数。Q8：不给只读连接发 `read_only=true` token（`blob_tokens.read_only` 列保持不启用，显式只读 → `READ_ONLY_SESSION`）。**四处均有代码注释点名「RFC 默认值、不是已拍板」** | `test_revoke_purges_tokens.py` 5 例、`test_a8_*`、`test_q1_explicitly_read_only_connection_is_refused` |

## 3. 三处版本钉：改后原文（只追加，未拆检查）

```
① check_subagent_events（原 :1096）
   check(rpc.get("version") in ("1.4.0", "1.5.0", "1.6.0", "1.7.0"),
         f"rpc.json: version must be 1.4.0 (RFC-0012), 1.5.0 (RFC-0014), 1.6.0 (RFC-0015) "
         f"or 1.7.0 (RFC-0016, the current head) (got {rpc.get('version')!r})")
② check_session_meta（原 :1356）
   check(rpc.get("version") in ("1.3.0", "1.4.0", "1.5.0", "1.6.0", "1.7.0"), …)
③ check_session_search（原 :1480）
   check(rpc.get("version") in ("1.5.0", "1.6.0", "1.7.0"), …)
```

新增的 `check_blob_token()` 里另有一处**精确**钉：`check(rpc.get("version") == "1.7.0", …)`（RFC-0016 拥有 C-05 head）。

**门禁咬合证明（§13.4 要求实测，全部真实执行）**——把 `schema/rpc.json` 临时改掉、跑门禁、还原：

| 变异 | 结果 |
|---|---|
| 顶部 version → `1.8.0`（未登记版本） | **FAIL**（四处版本钉同时红，含三处追加钉与新增的精确钉） |
| 删 `result.endpoints.fetch` | **FAIL**（六键集、别名比对、代码侧三方一致三条同时红） |
| `result.expires_at` 改名 `expired_at` | **FAIL** |
| `errors` 里 `UNSUPPORTED` 换 `UNAVAILABLE` | **FAIL** |
| `params` 加 `device_id` 键 | **FAIL** |
| 第 12 块 `version` 改回 1.6.0 | **FAIL** |

还原后 `contracts: OK`。**没有任何既有检查被拆或放宽**（`git diff tools/check_contracts.py` 可逐行核：版本钉只多了一个 `"1.7.0"` 成员）。

## 4. 验收对照（RFC §10 A1–A12）

| # | 状态 | 证据 |
|---|---|---|
| A1 | ✅ | `test_a1_contract_shape_matches_the_schema_and_the_code`（逐键比对 schema ↔ 代码 ↔ http_endpoints）+ `check_blob_token()`；§3 的六变异证明「改键名 / 删 endpoints / errors 换 UNAVAILABLE 全红」 |
| A2 | ✅ | `test_a2_mint_then_full_upload_lifecycle_over_the_real_asgi_app`（begin → PUT ≥2 片 → complete → HEAD 续传锚 → DELETE abort → GET 含 206 单段 Range，全部用铸出的 token）+ e2e 真 socket |
| A3 | ✅ | `test_a3_token_binds_the_calling_connection_and_the_submit_gate_agrees`（`BlobRecord.device_id == connection.device_id`；同设备 `resolve_attachments` 过、换设备 `PERMISSION_DENIED`；`core/tests/session/test_parts.py` 既有用例继续承重）+ e2e 同连接 `ui/submit` 建 turn |
| A4 | ✅ | `test_a4_*` 8 例：缺省=600；60/900 收；59/901/1.5/true/"600"/null 逐个 `INVALID_ARGUMENT data.field="ttl_s"` 且 **blob_tokens 零新增行** |
| A5 | ✅ | `test_a5_token_shape_and_expiry_window`（43 字符 base64url、毫秒、±2 s 窗口） |
| A6 | ✅ | `test_a6_expired_revoked_and_never_issued_are_indistinguishable`（三种响应 status=401 且**字节完全相同**） |
| A7 | ✅ | `test_a7_connection_without_identity_is_refused_and_writes_no_row` |
| A8 | ✅ | `test_a8_unknown_parameter_keys_are_refused_by_name`（device_id/read_only/session_id 逐个，`data.field=<键名>`，零写行） |
| A9 | ✅ | `test_a9_token_survives_a_core_restart`（同数据目录新 service，行在 sqlite）+ `test_a9_revoke_device_kills_the_token_immediately` + `test_revoke_purges_tokens.py` 5 例（解绑白名单设备立即失效） |
| A10 | ✅ | `test_a10_audit_records_the_mint_and_never_the_token`（审计含 kind/status/device_id/expires_at；token 字面量、sha256、**哈希前 16 位**全文搜索 0 命中；失败不写成功行） |
| A11 | ⚠️ core 侧交付，客户端侧在 vade-client | `test_a11_*` 三例：旧 core 模拟（handler 表无此方法）→ `RpcError("NOT_FOUND", "unknown method 'ui/blob.token'")`；§7.1 析取判定（`-32601` **或** `NOT_FOUND`+`unknown method`）写成测试内谓词并双向断言；发布后 dispatcher 真回 result；**本仓红线禁改 `sdk/**`，`BlobTokenDegradeTest.kt` 属客户端批次**（LEDGER 行已注明） |
| A12 | ⚠️ 协议层复现完成，真机拖拽属客户端批次 | e2e：submit 后**全新连接** `ui/attach` 取回 part；blobs 行 `pinned=1`、`sha256` 一致、`device_id == module#instance`。桌面截图需真客户端，不在本仓 |

## 5. 四道门禁（真实输出）

```
$ .venv/Scripts/python.exe -m pytest core/tests -q
1203 passed, 9 warnings in 526.67s          （基线 7941b93 = 1165；本棒 +38 = 32+5+1）

$ python tools/check_contracts.py
contracts: OK (… RFC-0014 session search green, RFC-0016 blob token green, …)

$ python tools/check_ledger.py
ledger: OK (16 RFC rows; rpc 1.7.0 49 methods in 3 directions, events 2.0.0 34 entries,
        34 error codes, appends 12+8+3; impl paths verified, statuses verified, …)

$ python docs/check_docs.py
合计 12 项，失败 0 项。
```

## 6. 两处需要父级知情的实现选择（均在红线允许的文件内）

1. **`http_endpoints.auth.device` 文案**：RFC §9.1-4 给的目标字符串本身仍带 `***` 与悬空的 `>`（正是 §2.1 点名的截断残迹——实际文件里那句话原本是 `Authorization: Bearer <token minted over …>`，RFC 引文里的 `***` 是转引时的脱敏）。本棒改成 `"Authorization: Bearer <token minted by ui/blob.token over the already-authenticated device channel; bound to the calling connection's device_id, TTL 60..900 s>"`：保留 §9.1 规定的全部语义元素（点名方法 / 绑连接 device_id / TTL 60..900），同时把悬空 `>` 修掉。`check_blob_token()` 钉的是「含 `ui/blob.token`」。若父级坚持逐字用 §9.1 原文，改这一行字符串即可，无结构影响。
2. **`make_rpc_handler` 兜 `VadeError`**（`core/vade/app.py`）：e2e 实测发现 provider 发布的 handler（`ui/blob.token`，也包括既有 `ui/page.get`/`ui/tool.call` 等 modulepage 方法——同一缺口）抛 `VadeError` 会穿透 dispatcher 到 `connection.py:170` 直接 CLOSE，客户端看到的是「连接被断」而不是错误信封，cookie 会话被拒时客户端会当成网络故障无限重试。现与 `SessionRPC.dispatch` 同形转 `RpcError(code, message, data)`。这是行为修复而非新能力，覆盖所有 provider handler；如需单独拆账，改动只有这一个 try/except。

另：**任务书原文写「ttl_s 夹在 60..900」**——按 RFC §5.2-6/决定 4/A4（越界/非整数一律 `INVALID_ARGUMENT`，静默夹取对凭据有效期不可接受）实现为**拒而不是夹**；「夹」字按「约束在 60..900」理解。若父级真要夹取语义，A4 六个用例会红，需要显式推翻 RFC。

## 7. 红线自证

```
$ git diff --stat -- schema/events.json schema/errors.json schema/manifest.schema.json schema/config.schema.json
（空 —— 逐字节未变；C-04 仍 34 码：check_contracts 的 len(codes)==34 硬钉未动且仍绿）
$ git diff --stat -- sdk
（空）
$ git diff --stat -- 'core/vade/blob/http.py'
（空 —— 六条路由的鉴权/上限/content-type 一行未改；Q7 收紧只动了 service.stat）
```

未注册方法回包仍是字符串码 `NOT_FOUND` + `unknown method …`（本棒不改那五处 `-32601` 提示文案，只把析取判定写进 A11 测试）。

## 8. 环境备注

worktree 没有未跟踪的构建产物 `protocol/core/python/vade_protocol.dll`（gitignored），Noise 系测试（e2e/transport）会整体 skip；已从主仓 `D:/HermesWorkspace/Vade/Vade/protocol/core/python/` 复制该 dll 到 worktree 后全量实跑（非 skip 口径）。全量 pytest 约 9 分钟（其中 e2e 起 4 个真 uvicorn listener）。

## 9. 残留（下一棒 / 客户端仓）

1. vade-client：`BlobHttpChannel` 原生模式先调 `ui/blob.token`；§7.2 三态（CoreTooOld/Disabled/NoDeviceIdentity）+ `Authorization` 头不进日志 + A12 真机拖拽截图（`BlobTokenDegradeTest.kt`）。
2. Q7 的已知代价已在代码注释与 LEDGER 写明：同一会话跨设备的 token 读面隔离后，桌面（自身 token）看不到手机上传的历史图片；若产品要跨设备可见，正解是 session 授权面收紧（另开提案），不是放宽本闸。
3. `revoke_node`（admin）目前只关连接不触 whitelist；「解绑」的唯一权威入口仍是 `Whitelist.revoke`（token 清算已挂在其回调上）。若 admin 要提供解绑 UI，调用 `whitelist.revoke(fingerprint)` 即可复用同一链路。

---

> 提交记录：实现提交 **`b182fe0`**（feat(blob)…，含本报告）；其后一个 docs 提交仅回填本文件与 `docs/status.md` 里的 commit hash，无代码改动。
