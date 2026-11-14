---
draft: blob-token
title: DRAFT：附件上传的设备凭据铸造（`ui/blob.token`）
status: proposed
date: 2026-11-14
numbering: 未分配——编号由父代统一分配；本文件不占用任何 RFC-NNNN
proposer: 侦察批次 `s-blobtoken`（复核客户端 `w-adapt` 的「blob 凭证须由 shell 外部供给」结论）
affects: C-05 `schema/rpc.json` —— `ui_to_core` 追加 1 个 `ui/blob.token`（顶部版本 1.5.0 → 1.6.0），`http_endpoints.auth.device` 的说明补上铸造方法名；core 侧 `core/vade/session/rpc.py`（注册 + 参数校验）、`core/vade/app.py`（blob 端口交给 RPC、撤销路径接 `BlobService.revoke_device`）、`core/vade/blob/service.py`（沿用 `issue_token`，签名不动）；无新表（`blob_tokens` 已存在）、无新事件（C-06 不动）、无新错误码（C-04 仍 34）、无新配置键（C-18 不动）
related: RFC-0004（§4.5 设计本方法、§7.1 #2 声明为 C-05 追加，但合并进 schema 时方法掉了）；RFC-0007 / RFC-0014（「方法不存在就降级」的客户端判定同法）；`core/vade/transport/RESULT-device-id.md`（原生连接拿到 `device_id` 的那批，§未做 1 已记录本缺口）；`docs/status.md` rfc0004a 行（把 `ui/blob.token` 列为未做）
type: C-05 追加；不改 C-04 错误码、不改 C-06、不改 C-03/C-18；不触碰任何现成路由或凭据模型
stage: 提案（未实现）——本文件只定契约，不改 `schema/**`、不改 `core/**`
---

# DRAFT · 附件上传的设备凭据铸造

> 本文件不带 RFC 编号：编号由父代统一分配。写入 `schema/rpc.json` 时 `doc` 字段指向父代分配后的正式文件名。

## 1. 摘要

`/ui/blob*` 六条路由（`core/vade/blob/http.py:44-51`）今天只认两种凭据：`blob_tokens` 表里的设备 Bearer，或浏览器 cookie 的四道闸。**原生（Noise 配对）客户端两种都拿不到**：Bearer 只能由 `BlobService.issue_token()` 铸造，而这个方法在整个生产代码里没有任何调用方（只有 `core/tests/blob/*` 直接调）；cookie 只能由 `POST /ui/session` 用 **browser** 用途的配对码换，且换出来的是一个新的 `ui-…` 身份，不是原生设备那一个。

于是原生端「已配对、能收发消息、能 `ui/submit` 文本」，在附件上传这一步却没有凭据。RFC-0004 §4.5 早就设计了解法 `ui/blob.token`、§7.1 #2 还把它列为 C-05 追加项，但合并时只落了 `ui/submit.content` 与 `http_endpoints` 两个 append 条目（`schema/rpc.json:20-25`），方法既没进 `ui_to_core`，也没进 `SessionRPC.handlers()`。本提案把它补成契约。

| # | 决定 | 理由 |
|---|---|---|
| 1 | 新方法 `ui/blob.token`，方向 `ui_to_core`，C-05 1.5.0 → **1.6.0** | 形状 RFC-0004 §4.5 已冻结（`docs/rfc/RFC-0004-attachments-multimodal.md:131`），本提案不重新设计，只补登记与实现约定 |
| 2 | token **绑定 `connection.device_id`**，不接受任何 `device_id` 参数 | 设备身份只能来自传输层已证明的东西（Noise 静态键 → 白名单，`core/vade/transport/server.py:354-378`）；让客户端传 device_id 等于把提示当声明，正是 `parts.py:473-478` 反复警告的那类错误 |
| 3 | TTL 默认 600 s、范围 **60..900**，越界 `INVALID_ARGUMENT` | 与 `TOKEN_TTL_DEFAULT/MIN/MAX`（`core/vade/blob/service.py:55-58`）一致；`issue_token` 现有 `max/min` 夹取（`service.py:184`）保留为内部兜底，RPC 面必须**拒**而不是静默夹，否则客户端以为 5 s 拿到的就是 5 s |
| 4 | 返回 `{token, expires_at(ms), max_bytes, endpoints{…}}` | `expires_at` 用毫秒是本仓统一口径（`service.py:60-67` 的 `_ms()`）；`endpoints` 让客户端不必内嵌 6 条路径，减少一处漂移 |
| 5 | 错误码：`INVALID_ARGUMENT` / `PERMISSION_DENIED` / `READ_ONLY_SESSION` / `UNAVAILABLE` | **不新增 C-04 码**（仍 34，`schema/errors.json:14,17,28`）；比 RFC-0004 的「只有两个码」多两个，差异与理由见 §3.3 |
| 6 | 不新增配置键、不新增事件、不新增表 | `blob_tokens`（`core/vade/blob/store.py:76-82`）已经为这件事建好；C-06 无状态变更可广播；`[attach].enabled=false` 时用 `UNAVAILABLE` 而不是新开关 |
| 7 | 浏览器会话**也允许**调用（`device_id` 非空即可） | 方法对 cookie 会话是冗余（它本来就能上传），但拒绝它会让 shell 需要两套分支；允许只多一次无害的签发 |
| 8 | 不选「逐请求设备密钥签名」（RFC-0004 §11 备选 B） | nonce 表 + ±300 s 时钟窗 + 每片一次签名，成本高于一次 RPC；RFC-0004 Q6 已倾向前者，本提案只是把它落地 |

## 2. 侦察证据（逐条带文件:行）

### 2.1 六条路由的凭据入口是同一个函数

| 方法 | 路径 | 处理器（`core/vade/blob/http.py`） | 鉴权调用行 |
|---|---|---|---|
| POST | `/ui/blob`（begin） | `_begin` 205-211 | **207** |
| PUT | `/ui/blob/upload/{id}?offset=N` | `_put_part` 213-222 | **215** |
| HEAD | `/ui/blob/upload/{id}`（续传锚点） | `_head_upload` 224-227 | **225** |
| POST | `/ui/blob/upload/{id}/complete` | `_complete` 229-235 | **231** |
| DELETE | `/ui/blob/upload/{id}`（abort） | `_abort` 237-240 | **238** |
| GET\|HEAD | `/ui/blob/{file_id}[?v=thumb]` | `_get_blob` 242-247 | **243** |

六条全部走 `Authenticator.authenticate()`（`core/vade/blob/auth.py:159-181`），没有任何端点有自己的一套：

- Bearer 分支（`auth.py:160-171`）→ `self._tokens.get_token(sha256(token), now)`（`auth.py:162`）→ `BlobService.get_token`（`service.py:204-205`）→ `BlobStore.get_token`（`store.py:479-497`）→ **只看 `blob_tokens` 表**。
- cookie 分支（`auth.py:173-179`）→ `_cookie_gates`（`auth.py:115-156`）：Origin 白名单 + `Sec-Fetch-Site: same-origin` + `__Host-ac_ui` + `x-vade-csrf` 双提交与绑定校验。
- 都没有 → `AUTH_FAILED`（`auth.py:181`）。

### 2.2 两种凭据的唯一来源

| 凭据 | 唯一铸造入口 | 谁能拿到 | 原生配对设备为什么不行 |
|---|---|---|---|
| 设备 Bearer（600 s） | `BlobService.issue_token()`（`service.py:173-201`）→ `BlobStore.add_token()`（`store.py:471-477`） | **今天没有任何生产调用方**：`grep -rn "issue_token"` 只命中定义与 `core/tests/blob/{conftest.py:149,263, test_gc.py:46,90, test_mount.py:66, test_review_fixes.py:257,281}` | 设备通道没有铸造入口可调：`ui_to_core` 的 34 个方法（`schema/rpc.json:420-1163`）里没有一个 `ui/blob.*` |
| 浏览器 cookie | `POST /ui/session`（`core/vade/transport/server.py:168-172` → `_ui_session` 204-243） | 持有 **browser 用途**配对码的客户端：`pairing.consume(code, "browser")`（`server.py:216`）；用途只有 `module|browser`（`core/vade/identity/pairing.py:68-71`），用途不符按未知码拒（`pairing.py:229-235`） | 原生设备用 Noise 静态键配对（白名单），拿不到 browser 用途的码；即便硬拿一个，签发出来的是**新的** `ui-<16 hex>` 身份（`server.py:222`），与连接上的 `device_id` 不同源 |

### 2.3 原生连接的身份从哪来，为什么没有 HTTP 凭据

`_ws`（`server.py:298-352`）→ Noise IK 握手 → `_bind_device_identity`（`server.py:354-378`）用 `remote_static` 指纹查白名单（`core/vade/identity/whitelist.py:111-119`），把 `connection.device_id` 设成 `module_id#instance_id`。这条路径**不签发任何 HTTP 凭据**：`connection.ui_token` 对原生连接恒为 `None`（`server.py:350`）。

### 2.4 「凭证须由 shell 外部供给」这句话成不成立：**成立（原生模式），不是误判**

判定拆成两半，都带证据：

1. **原生（Noise）模式——工位上没有凭据可「供给」。** 客户端的统一装配点 `sessionSourceFrom()` 在**没有配对码**时构造 `VadeClientConfig(host, port)`（`vade-client` `composeApp/src/commonMain/kotlin/vade/ui/data/SourceConfig.kt:154-170`，`browserSession` 缺省为 null），即纯 Noise 连接：既没有 cookie（`server.py:350`），也没有 bearer（§2.2）。`BlobUploaderReal` 本身不看凭据、只接收注入的 `BlobHttpChannel`（`vade-client` `composeApp/src/commonMain/kotlin/vade/ui/data/BlobUploaderReal.kt:19-26`），所以「实现方必须拿到凭据」是对的，但**拿不到**——`shots/w-adapt/RESULT.md:66` 的原话与 core 自己的三处记录一致（`core/vade/blob/auth.py:5-8`、`core/vade/blob/RESULT.md:80-81`、`core/vade/transport/RESULT-device-id.md` §未做 1）。
2. **浏览器 cookie 模式——有会话 cookie，但也凑不齐四道闸。** 同一装配点在**配了配对码**时走 `BrowserPairing`（`SourceConfig.kt:156-164`），客户端的 `KtorTransportFactory.ensureCookie()` 会 `POST /ui/session`。但那只抓了 `__Host-ac_ui` 一个值：正则只匹配 `__Host-ac_ui=([^;]+)`，`__Host-ac_csrf` 被直接丢弃（`vade-client` `composeApp/src/commonMain/kotlin/vade/ui/protocol/VadeTransport.kt:112-119`），而 ws 升级只需要会话 cookie、不需要 CSRF，所以这个丢弃一直没暴露。`/ui/blob` 的 cookie 分支要求 `x-vade-csrf` 与 `__Host-ac_csrf` 双提交**且**绑定该会话（`core/vade/blob/auth.py:140-155`），缺一即 `403 ORIGIN_MISMATCH`；此外还要 `Origin` 落在 `server.ui_origins` 精确白名单里——默认是空数组（`schema/config.schema.json` → `server.ui_origins` `default: []`，`core/vade/config.py:198-206` 还禁止 `*`），以及一个非浏览器客户端自己声明 `Sec-Fetch-Site: same-origin`（`auth.py:124-131` 的注释写明：非浏览器上下文本来就不该用这个被动凭据）。

所以准确的说法是：**「须由 shell 外部供给」成立**——原生模式下无凭据可给；浏览器模式下 shell 手里也只有会话 cookie 的一半（缺 CSRF、缺 Origin 白名单、缺 `Sec-Fetch-Site`），而这三样正是 §8.2 用来排除非浏览器上下文的。把「有 `/ui/session` 这条路」当成「没问题」，等于假设客户端会伪装成浏览器并补齐它从未保存的 CSRF 值。旁证：客户端全仓 `commonMain` 里搜不到 `Sec-Fetch-Site` 或 `Origin` 头的构造，`csrf` 只出现在三处注释（`BlobUploaderReal.kt:26`、`VadeAdapters.kt:78,163`），没有一处真的发这个头。

### 2.5 cookie 不能顶替 Bearer：submit 面会把归属对不上

`ui/submit` 把 `connection.device_id` 传给 `resolve_attachments`（`core/vade/session/rpc.py:153` → `core/vade/session/manager.py:882`）。而 `resolve_attachments`：

- 没有身份 → `PERMISSION_DENIED / data.reason="device_unknown"`（`core/vade/session/parts.py:494-500`），fail-closed；
- 附件归属与连接身份不一致 → `PERMISSION_DENIED`（`parts.py:519-524`），且这一条**不受** `read_scope` 影响（`service.py:810` 只在 `read_scope="device"` 时查归属，默认 `"core"`，`core/vade/blob/settings.py:39`）。

即「cookie 传、原生 ws 提交」必然在提交那一步被拒：cookie 上传的 blob 属于 `ui-…`，原生连接的身份是 `module_id#instance_id`。要两边一致，整条链（ws + 上传）都得走 cookie 会话——那就不是原生模式了。

### 2.6 契约层：那串字符串到底在不在

| 检查 | 命令 | 结果 |
|---|---|---|
| `schema/rpc.json` 有 `ui/blob.token` | `grep -c "ui/blob.token" schema/rpc.json` | **0** |
| `schema/rpc.json` 有 `ui/blob` 的任何方法名 | `grep -n "ui/blob" schema/rpc.json` | 只有 `http_endpoints` 的路径（`:1193,1200,1207,1214,1221,1228`）与 append 说明（`:29`），**无方法名** |
| core 有对应 handler | `grep -rn "blob.token\|blob_token" core/vade` | 只有两处 docstring（`blob/auth.py:6`、`blob/service.py:181`）与表名 `blob_tokens`；**无 handler、无路由注册** |
| 契约怎么描述凭证获取 | `schema/rpc.json:1169` | `"Authorization: Bearer *** minted over the already-authenticated device channel>"`——声称有一条「在已认证设备通道上铸造」的路，但没有方法 |
| RFC-0004 自己怎么说 | `docs/rfc/RFC-0004-attachments-multimodal.md:128,131`（§4.5）、`:290`（§7.1 #2「C-05 新方法 `ui/blob.token`」）、`:446`（实施清单要求改 `schema/rpc.json`） | 设计、参数形状、契约变更都写了 |
| 实际落进 schema 的 append | `schema/rpc.json:20-25`；`docs/rfc/LEDGER.md:28` | `adds` 只有 `["ui/submit.content typed part union", "http_endpoints"]`，LEDGER 因此记「无方法名」 |

结论：不是「契约没写清」，而是**契约写了、合并时掉了**（与 `ui/submit.content` 定型同批，后者落了、前者没落）。因此本缺口属「确实存在、需要补方法」。

## 3. 提案：`ui/blob.token`

### 3.1 形状（可直接粘进 `schema/rpc.json` 的 `directions.ui_to_core`）

```json
"ui/blob.token": {
  "params": {
    "ttl_s": "int 60..900, default 600 (absent -> 600; non-int / bool / out of range -> INVALID_ARGUMENT, data.field=\"ttl_s\")"
  },
  "result": {
    "token": "string (base64url of 32 random bytes; only sha256(token) is stored)",
    "expires_at": "int (unix ms)",
    "max_bytes": "int (per-file ceiling carried by this token; equals [attach].max_bytes at mint time)",
    "endpoints": {
      "begin": "POST /ui/blob",
      "part": "PUT /ui/blob/upload/{upload_id}?offset=N",
      "complete": "POST /ui/blob/upload/{upload_id}/complete",
      "abort": "DELETE /ui/blob/upload/{upload_id}",
      "resume": "HEAD /ui/blob/upload/{upload_id}",
      "fetch": "GET|HEAD /ui/blob/{file_id}[?v=thumb]"
    }
  },
  "errors": ["INVALID_ARGUMENT", "PERMISSION_DENIED", "READ_ONLY_SESSION", "UNAVAILABLE"],
  "notes": "设备 bearer 的唯一铸造面：token 绑定调用连接已证明的 device_id（Noise 静态键 -> 白名单，transport/server.py:354-378），没有 device_id 参数、也没有别的设备可选（无预言机）。TTL 60..900 s，默认 600；只存 sha256(token)，行在 blob_tokens 表，重启后仍有效，撤销设备时整批删除。expires_at 是 unix ms。过期后与从未签发、已撤销不可区分（401 AUTH_FAILED，无预言机）。显式只读会话/设备 -> READ_ONLY_SESSION；attachments 关闭 -> UNAVAILABLE（data.reason=\"attachments_disabled\"）；连接无设备身份 -> PERMISSION_DENIED（data.reason=\"device_unknown\"）。endpoints 只是同一 listener 上六条路由的别名，客户端仍按 http_endpoints 的 content-type 与 body 上限行事。"
}
```

### 3.2 语义（逐条可验）

1. **绑定**：token 行的 `device_id` = 调用连接的 `connection.device_id`；不为 `None`、不可指定（`store.py:76-82` 的 `device_id NOT NULL`）。
2. **TTL**：`ttl_s` 缺省 600；`60 ≤ ttl_s ≤ 900`；越界或非整数（含 `true`）→ `INVALID_ARGUMENT`。RPC 层判，不靠 `issue_token` 的夹取。
3. **单文件上限**：`max_bytes` = 签发时的 `[attach].max_bytes`；`begin` 用它判 413（`service.py:217-222`）。配额仍由 `begin` 的 `_check_quota` 判（`service.py:376-392`），token 不携带配额。
4. **有效期与撤销**：行落 `blob_tokens`（`store.py:471-477`），重启有效；`prune_tokens` 清过期（`store.py:494-497`）；`revoke_device` 清整批（`service.py:207-208`）。
5. **无预言机**：过期、已撤销、从未签发一律 `AUTH_FAILED`（`auth.py:161-165` + `store.py:484-486` 的「过期即删」），与现有行为一致。
6. **只读**：显式只读的会话/设备 → `READ_ONLY_SESSION`（沿用 RFC-0004 §4.5；`store.py:80` 的 `read_only` 列**不在本提案启用**，见 §8 Q1）。
7. **关闭**：`attach.enabled=false` 时 `app.blob is None`（`core/vade/blob/__init__.py:56-58`）→ `UNAVAILABLE`，与 `ui/submit` 的 `attachments_disabled` 同义（`parts.py:486-493`）。
8. **审计**：签发成功记一条 `blob.token`（`kind/status/device_id/expires_at`，**不含 token 明文**），复用 `BlobService._record`（`service.py:161-170`）。
9. **并发**：同一设备可同时持多个 token（各一行、各自 TTL），不互斥；客户端过期重铸即可。
10. **客户端用法**：拿到 token 后放进 `Authorization: Bearer <token>`，六个端点通用；`401 AUTH_FAILED` 时重铸一次并重试当前分片（幂等：`(upload_id, offset)`，RFC-0004 §4.3）。

### 3.3 与 RFC-0004 §4.5 的差异（三处，评审时需点头）

| # | RFC-0004 §4.5 | 本提案 | 理由 |
|---|---|---|---|
| 1 | `errors` 只有 `READ_ONLY_SESSION` / `UNAVAILABLE` | 加 `INVALID_ARGUMENT`（ttl_s 越界）与 `PERMISSION_DENIED`（`device_id` 为 None） | 写 §4.5 时 `Connection.device_id` 还不存在（它是 `transport/RESULT-device-id.md` 那批才有的）。绑不到身份的 token 只能拒；用 `UNAVAILABLE` 描述「你没有身份」是撒谎，C-04 的 `PERMISSION_DENIED` 正是这个语义，且与 `parts.py:496-500` 同码同因 |
| 2 | `result.endpoints{begin,part,complete}` 三条 | 六条（补 `abort` / `resume` / `fetch`） | 失败清理必须 `DELETE`、断点恢复必须 `HEAD`、提交后回显必须 `GET`；只给三条等于让客户端自己拼路径。纯追加，无兼容面 |
| 3 | `max_bytes` 未说明来源 | 明确 = 签发时 `[attach].max_bytes` | `issue_token` 现有默认就是 `settings.max_bytes`（`service.py:194`），写实它，避免客户端猜 |

## 4. 为什么不破坏现有兼容

| 面 | 现状（行号） | 加方法后 |
|---|---|---|
| 老客户端 | 不调用 `ui/blob.token`；浏览器走 `/ui/session` cookie | 行为零变化 |
| 缺方法的客户端 | 未注册方法 → `{"error":{"code":"NOT_FOUND","message":"unknown method '…'"}}`（`core/vade/session/rpc.py:55`、`core/vade/app.py:241`）——**注意不是 JSON-RPC `-32601`**（`rpc.py:82` 的注释与 RFC-0014 §2.2 的措辞都不准确） | 客户端按 `code=="NOT_FOUND"` + `message` 前缀判定并降级；落地批次顺手把 `rpc.py:82` 注释改成实话（不改行为） |
| 六条路由 | 鉴权只有 `Authenticator.authenticate`（`http.py:207/215/225/231/238/243`） | **一行不改**：Bearer 分支本来就查 `blob_tokens`（`auth.py:160-171`），本提案只是终于有人往那张表里写行 |
| cookie 路径 | 四道闸（`auth.py:115-156`） | 不动 |
| 错误码 | C-04 冻结 34（`schema/errors.json`） | 复用 4 个已有码，不加码 |
| 配置 | `[attach]` 17 键（C-18） | 不加键；关闭时 `UNAVAILABLE` |
| 事件 | C-06 | 不发事件（签发不是会话状态变更） |
| 版本 | C-05 顶部 1.5.0 | 1.6.0（append 级也记 1.6.0） |

## 5. 对 C-0x 的影响

| 契约 | 影响 |
|---|---|
| **C-05** `rpc.json` | `ui_to_core` +1 方法（34 → 35，三方向合计 47 → 48）；顶部 `version` 1.5.0 → **1.6.0**；`x-vade.appends` 新增一块（`doc` 指向编号分配后的正式文件名，`contract: "C-05"`，`version: "1.6.0"`，`adds: ["ui/blob.token"]`）；`http_endpoints.auth.device` 的字符串补「由 `ui/blob.token` 铸造」 |
| **C-04** `errors.json` | 无（仍 34） |
| **C-06** `events.json` | 无 |
| **C-18** `config.schema.json` | 无（`[attach]` 17 键不变） |
| **C-03** `manifest.schema.json` | 无 |
| **C-01/C-02**（传输/设备鉴权） | 无改动：身份仍由 Noise 握手 + 白名单给（`server.py:354-378`），本方法只读 `connection.device_id` |
| **C-09**（UI 组件） | 无新组件；`AttachmentUploader` 的实现方从「外部供给凭据」变成「先 `ui/blob.token` 再传」，UI 形状不变 |

## 6. 落地清单（下一批，不在本文件）

| 文件 | 要做什么 | 锚点 |
|---|---|---|
| `schema/rpc.json` | 加方法、升版本、加 append 块、补 `http_endpoints.auth.device` 说明 | `:3`（version）、`:20-25`（append 数组）、`:420`（`ui_to_core`）、`:1169`（auth.device） |
| `core/vade/session/rpc.py` | `SessionRPC.__init__` 增 `blob_port`（照 `input_port` 的懒解析写法）；`handlers()` 注册；handler 取 `getattr(connection, "device_id", None)` | `:43-50`、`:68-101`、`:126-155`（`ui_submit` 的取法） |
| `core/vade/blob/service.py` | 复用 `issue_token`；若要在 service 层也拒越界，需与 RPC 层口径一致，否则两处行为分叉 | `:173-201`、`:184` |
| `core/vade/app.py` | 把 blob 端口接到 `SessionRPC`（现成解析器在 `core/vade/session/manager.py:946-953`）；`_revoke_connection` 补「撤销设备 → `blob.service.revoke_device(module#instance)`」 | `:78`（`on_revoke` 来源）、`:144-146` |
| `core/vade/identity/whitelist.py` | `revoke()` 已返回被删的 `module#instance`（`:202-209`），但 `on_revoke` 回调只带指纹；撤销接线要拿到 id（改回调签名，或在调用侧用返回值） | `:202-209` |
| `tools/check_contracts.py` | 新 `check_blob_token()`：方法存在、result 键集合、errors ⊆ C-04、`SessionRPC.handlers()` 已注册；**并改版本白名单**——`check_session_meta()` 断言 `rpc.version ∈ {1.3.0,1.4.0,1.5.0}`（`:1361-1363`），升到 1.6.0 会让它变红 | 参照 `check_session_meta()`（`:1328-1363`） |
| `core/tests/` | 新用例覆盖 §7 的 A1–A12（真走 `CoreApp`/ASGI，不 mock） | 照 `core/tests/blob/` 的既有驱动方式 |
| `docs/status.md`、`docs/rfc/LEDGER.md` | 落地批次回填（本批**不动**台账） | — |

## 7. 验收条件

| # | 条件（可机械判定） |
|---|---|
| A1 | `schema/rpc.json` 的 `ui_to_core` 有 `ui/blob.token`，`params/result/errors/notes` 与 §3.1 一致；`tools/check_contracts.py` 的断言覆盖它，改动任一字段即红 |
| A2 | 一条真 Noise + 白名单的原生连接调用 → 200/`result`；用该 token 能跑完 `begin → PUT → complete`，并能 `HEAD` 续传、`DELETE` abort、`GET` 取回 |
| A3 | token 绑身份：设备 A 的 token 读不到 B 的 blob（`read_scope="device"` → `PERMISSION_DENIED`）；A 上传的 blob 也不能被 B 的 `ui/submit` 引用（`parts.py:519-524`） |
| A4 | `ttl_s` 边界：缺省 → 600；`60`/`900` 接受；`59`/`901`/`1.5`/`true`/`"600"` → `INVALID_ARGUMENT`、`data.field=="ttl_s"`、**不写行** |
| A5 | `expires_at` 是 unix 毫秒，落在 `[now+ttl_s-2, now+ttl_s+2]` 秒窗口内 |
| A6 | 过期后同 token → `401 AUTH_FAILED`，与「从未签发」「已撤销」的响应字节相同（无预言机） |
| A7 | `device_id is None` 的连接 → `PERMISSION_DENIED`、`data.reason=="device_unknown"`，`blob_tokens` 不新增行 |
| A8 | `attach.enabled=false` → `UNAVAILABLE`、`data.reason=="attachments_disabled"`，六条路由仍 404（`http.py:138-140`） |
| A9 | 重启 core 后未过期的 token 仍能用（行在 sqlite）；`revoke_device(device_id)` 后同 token 立刻 `401` |
| A10 | 每次成功签发写一条 `blob.token` 审计，`extra` 不含 token 明文；失败不写「成功」行 |
| A11 | 无该方法时客户端按 `code=="NOT_FOUND"` 判定缺失并回落到 cookie/降级路径；有该方法时不改变任何既有方法的响应 |
| A12 | 连发 N 次得 N 行；`prune_tokens` 清过期行、`revoke_device` 清整批（行数与 `usage` 不泄漏） |

## 8. 未决问题

| # | 问题 | 倾向 |
|---|---|---|
| Q1 | 显式只读会话该**拒发**（RFC-0004 §4.5）还是签发 `read_only=true` 的 token（`blob_tokens.read_only` 列已存在，`store.py:80`；`Principal.read_only` 已能生效，`auth.py:169`、`service.py:213`）？ | 本提案按 RFC-0004 拒发（最小面）。若原生只读设备需要看历史图片，另开提案签只读 token，别在同一方法里塞两种语义 |
| Q2 | `max_bytes` 该不该随设备剩余配额收缩？ | 不该：token 携带静态上限，配额是每次 `begin` 的实时判定（`service.py:376-392`）。把动态值写进 token 会让「同一 token 今天能传、明天不能」变成不可解释的 413 |
| Q3 | 是否需要一个 `ui/blob.token.revoke`？ | 暂不。600 s 窗口 + 设备撤销整批清（`service.py:207-208`）够用；给 600 s 的凭据做全套生命周期管理，收益不抵面 |

## 9. 非目标

- **不做**逐请求设备密钥签名（RFC-0004 §11 备选 B）；理由见 §1 决定 8。
- **不做**「原生 `module#instance` ↔ 浏览器 `ui-…`」的归属映射（`transport/RESULT-device-id.md` §未做 2 记录的另一处缺口）。本提案让原生设备在自己的身份下上传与提交，两端互通另开提案。
- **不改**任何现成路由的鉴权、body 上限、content-type 规则或错误码。
- **不改**客户端；`w-adapt` 的 `BlobHttpChannel` 只需把「等待外部凭据」换成「调一次 `ui/blob.token`」。
