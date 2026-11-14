---
rfc: RFC-0016
number: RFC-0016
title: 附件上传的设备凭据铸造：`ui/blob.token`（补上 RFC-0004 §4.5 漏登记的方法，让原生配对设备真的能上传并提交附件）
status: proposed
date: 2026-11-15
owner: C2（C-05 契约与 `core/vade/blob/`）；落地跨 C3（`core/vade/session/` 的提交面）
proposer: 侦察批次 `s-blobtoken`（复核客户端 `w-adapt`「blob 凭证须由 shell 外部供给」的结论）；本棒 `s-blob-token` 把 `docs/rfc/DRAFT-blob-token.md` 提升为正式提案
rpc_version: "C-05 顶部 1.6.0 → 1.7.0（新 append 块版本 1.7.0）；C-04 1.0.0 不动；C-06 2.0.0 不动；C-03 / C-18 不动"
appends_rpc: 11 → 12
appends_events: 8 → 8（不变：本提案无事件）
affects: C-05 `schema/rpc.json` —— `directions.ui_to_core` 追加 1 个方法 `ui/blob.token`（参数只有 `ttl_s`，回包 `{token, expires_at, max_bytes, endpoints{6}}`）；顶部 `version` 1.6.0 → **1.7.0**；`x-vade.appends` 追加第 **12** 块（`adds:["ui/blob.token"]`、`rewrites:"none"`）；`http_endpoints.auth.device` 的字符串补上方法名（现文案只有 `***` 与一个悬空的 `>`，见 §2.1）。落地要动 `core/vade/blob/__init__.py`（把 handler 发布进 `handler_providers`）、新增 `core/vade/blob/rpc.py`、`core/vade/app.py` + `core/vade/identity/whitelist.py`（撤销接线）；**不动** C-06（无新事件）、**不动** C-04（无新错误码，仍 34）、**不动** C-03/C-18（无新配置键）、**不动** `core/vade/blob/http.py` 的六条路由鉴权（一行不改，见 §5.1）
related: RFC-0004（§4.5 设计本方法、§7.1 #2 把它列为 C-05 追加项，但合并进 schema 时方法掉了；§4.3 分片幂等；§4.5 鉴权表；§6.4 `file_id` 归属；§8.2 cookie 四道闸；§11 备选 B）；RFC-0005 / RFC-0006 / RFC-0010 / RFC-0012 / RFC-0014 / RFC-0015（「方法缺失 → 客户端降级」的同一纪律，见 §7.1 的订正）；`core/vade/transport/RESULT-device-id.md` §未做 1（原生连接拿到 `device_id`、没拿到 HTTP 凭据）；`docs/status.md` 的 `rfc0004a` 行（把 `ui/blob.token` 列为「未做」）；`docs/rfc/DRAFT-blob-token.md`（本提案的前身，处置见 §14）
type: C-05 追加（1 个方法）；不改 C-04 错误码、不改 C-06 事件族、不改 C-03/C-18、无破坏性改名、无客户端强制升级
stage: 提案（未实现）——本文件只定契约与实现约定，**不改** `schema/**`、**不改** `core/**`、**不改** `sdk/**`、**不改** `tools/check_*.py`
---

# RFC-0016 · 附件上传的设备凭据铸造

## 1. 摘要

`/ui/blob*` 六条路由（`core/vade/blob/http.py:157-200` 的 `_dispatch`）今天只认两种凭据：`blob_tokens` 表里的设备 Bearer，或浏览器 cookie 的四道闸。**原生（Noise 配对）客户端两种都拿不到**：

- Bearer 只能由 `BlobService.issue_token()` 铸造（`core/vade/blob/service.py:173`），而这个方法**在整个生产代码里没有任何调用方**（§2.2）；
- cookie 只能由 `POST /ui/session` 用 **browser 用途**的配对码换，换出来的是一个**新的** `ui-<16 hex>` 身份（`core/vade/transport/server.py:222`），不是原生设备那一个（`module_id#instance_id`）。

于是原生端「已配对、能收发消息、能 `ui/submit` 纯文本」，到附件上传这一步却没有凭据；即便硬拿 cookie 去传，`ui/submit` 也会在归属校验上拒掉（`core/vade/session/parts.py:519-524`）。桌面/Android 拖文件进会话这条路因此是**断的**（§2.3）。

RFC-0004 §4.5 早就设计了解法 —— `ui/blob.token`（`docs/rfc/RFC-0004-attachments-multimodal.md:128-131`），§7.1 #2 还把它明确列为「C-05 新方法」（`:290`）。但合并时只落了 `ui/submit.content` 与 `http_endpoints` 两个 append 条目（`schema/rpc.json:18-34`），方法既没进 `directions.ui_to_core`，也没进任何 handler 表。**本提案把它补成契约**：形状照 RFC-0004 §4.5，不重新设计；只补登记、补语义、补安全边界、补降级义务。

| # | 决定 | 理由 |
|---|---|---|
| 1 | 新方法 `ui/blob.token`，方向 `ui_to_core`，C-05 1.6.0 → **1.7.0** | 形状 RFC-0004 §4.5 已冻结（`RFC-0004:131`），本提案只补登记与实现约定 |
| 2 | token **绑定 `connection.device_id`**，**不接受任何 `device_id` 参数** | 设备身份只能来自传输层已证明的东西（Noise 静态键 → 白名单，`transport/server.py:354-378`）；让客户端传 `device_id` 等于把提示当声明，正是 `parts.py:466-472` 与 `server.py:364-370` 反复警告的那类错误 |
| 3 | **只允许原生（Noise）连接铸造**；cookie 会话调它 → `PERMISSION_DENIED`，`data.reason="cookie_session"` | 浏览器已经有能用的凭据（cookie 四道闸）；给它再发一张 bearer，等于把「环境凭据」升级成「可搬运凭据」，是为零收益扩大爆炸半径（**这是对草稿决定 7 的显式反转**，见 §5.3） |
| 4 | TTL 默认 **600 s**、范围 **60..900**；越界/非整数**拒**而不是夹取 | 与 `TOKEN_TTL_DEFAULT/MIN/MAX`（`service.py:56-58`）一致；`issue_token` 现在的 `max/min` 夹取（`service.py:184`）会把 5 s 悄悄变成 60 s、把 `True` 变成 60 s、把 `"600"` 变成 600 —— 对**凭据有效期**做静默修正不可接受（§5.2） |
| 5 | 返回 `{token, expires_at(ms), max_bytes, endpoints{6}}` | `expires_at` 用毫秒是本仓统一口径（`service.py:63-68` 的 `_ms()`）；六条 `endpoints` 让客户端不必内嵌路径 |
| 6 | token **在 TTL 内可重放**（不是一次性） | 一次上传 = 1 次 `begin` + N 次 `PUT` + 1 次 `complete` + 可能 1 次 `HEAD`；一次性 token 会逼客户端每个分片铸一次。`store.py:479-492` 的 `get_token` 本来就不消费行 |
| 7 | 错误码**只复用**：`INVALID_ARGUMENT` / `PERMISSION_DENIED` / `READ_ONLY_SESSION` / `UNSUPPORTED` | C-04 冻结 34（`schema/errors.json`；`tools/check_contracts.py:1540` 硬钉 `len(codes)==34`），五种失败形态全部能被既有码 + `data.reason` 表达（§6.6） |
| 8 | 不新增配置键、不新增事件、**不新增表** | `blob_tokens`（`store.py:76-82`）就是为这件事建的；C-06 无状态变更可广播；`attach.enabled=false` 用 `UNSUPPORTED` 而不是新开关 |
| 9 | `params` **严格**：只认 `ttl_s`，出现任何其它键 → `INVALID_ARGUMENT`，`data.field=<键名>` | 这是铸凭据的方法，静默忽略 `device_id` / `read_only` 会让客户端误以为自己拿到了别的东西；也把「身份永不出自参数」变成可测断言（A8） |
| 10 | 不选「逐请求设备密钥签名」（RFC-0004 §11 备选 B） | nonce 表 + ±300 s 时钟窗 + 每片一次签名，成本高于一次 RPC；RFC-0004 Q6 已倾向前者，本提案只是落地 |

## 2. 动机与静态证据

三条独立证据，每条都能追到文件与符号名。**它们指向同一个结论：缺的不是客户端解码，是契约面上的一条铸造通道。**

### 2.1 方法在契约里根本不存在

| 检查 | 命令（在本 worktree 实跑） | 结果 |
|---|---|---|
| `schema/rpc.json` 有 `ui/blob.token` | `grep -c "ui/blob.token" schema/rpc.json` | **0** |
| 有任何 `ui/blob*` 的**方法名** | `grep -n "ui/blob" schema/rpc.json` | 只有 `:29` 的 append 说明与 `:1241 / :1248 / :1255 / :1262 / :1269 / :1276` 六条 `http_endpoints` 的**路径**；`directions` 里没有 `ui/blob*` 条目 |
| core 有对应 handler | `grep -rn "blob.token\|blob_token" core/vade` | 只有两处 docstring（`blob/auth.py:7`、`blob/service.py:181`）与表名 `blob_tokens`；**无 handler、无路由注册** |
| 契约怎么描述凭据来源 | `schema/rpc.json:1217`（`http_endpoints.auth.device`） | 逐字是 `"Authorization: Bearer *** minted over the already-authenticated device channel>"` —— **既没点名方法，`<` 还掉了**（`***` 像是脱敏残留）。这句话声称「有一条在已认证设备通道上铸造的路」，但那条路不存在 |
| RFC-0004 自己怎么说 | `RFC-0004:128-131`（§4.5 表与形状）、`:290`（§7.1 #2「C-05 新方法 `ui/blob.token`」）、`:446`（实施清单要求改 `schema/rpc.json`） | 设计、参数形状、契约变更全写了 |
| 实际落进 schema 的 append | `schema/rpc.json:18-34`；`docs/rfc/LEDGER.md` §1 | `adds` 只有 `["ui/submit.content typed part union", "http_endpoints"]`，**没有方法名** |

> 不是「契约没写清」，而是**契约写了、合并时掉了**：与 `ui/submit.content` 定型同批进 schema，后者落了、前者没落。所以本缺口属「确实存在、需要补方法」，不是「设计未定」。

### 2.2 铸造入口零调用方

```
$ grep -rn "issue_token" core/ --include=*.py
core/vade/blob/service.py:173:    def issue_token(          <- 唯一实现
core/vade/blob/auth.py:7:  ... `BlobService.issue_token` is the seam ...   <- docstring
core/tests/blob/conftest.py:149,263
core/tests/blob/test_gc.py:46,90
core/tests/blob/test_mount.py:66
core/tests/blob/test_review_fixes.py:257,281                        <- 只有测试
```

`service.py:181` 的 docstring 自己写着 `"""Mint a device bearer token. `ui/blob.token` (next slice) calls this."""` —— 这个「next slice」就是本提案。生产代码里**零调用方**，所以 `blob_tokens` 表在真机上永远是空的：Bearer 分支（`auth.py:159-176`）查到的只有 `None`。

### 2.3 两种身份永不匹配 ⇒ `ui/submit` 必被拒

| 连接类型 | `connection.device_id` 的来源 | 形态 | 行号 |
|---|---|---|---|
| 浏览器 cookie | `/ui/session` 当场新铸：`f"ui-{secrets.token_hex(8)}"` | `ui-<16 hex>` | `transport/server.py:222`（经 `connection.ui_token`/`device_id` 落到 `connection.py:124`） |
| 原生 Noise | 握手验过的静态键指纹 → 白名单反查 | `module_id#instance_id` | `transport/server.py:354-378`（`_bind_device_identity`）→ `identity/whitelist.py:111-119` |
| 原生 Noise，白名单没命中 | 保持 `None`（fail-closed） | `None` | `transport/server.py:366-370` |

两者**不同源、不同形态、永不相等**。而 `ui/submit` 把连接的 `device_id` 传给 `resolve_attachments`（`session/rpc.py:149-155` → `session/manager.py:882`），后者：

- 没有身份 → `PERMISSION_DENIED`，`data.reason="device_unknown"`（`parts.py:493-500`），fail-closed；
- 附件归属 ≠ 连接身份 → `PERMISSION_DENIED`，`"file belongs to another device"`（`parts.py:519-522`），**不受** `read_scope` 影响（`service.py:816` 只在 `read_scope="device"` 时查归属，默认是 `"core"`，`settings.py:39`）。

即「cookie 传、原生 ws 提交」**必然**在提交那一步被拒：blob 属于 `ui-…`，连接身份是 `module_id#instance_id`。要两边一致，整条链（ws + 上传）都得走 cookie 会话 —— 那就不是原生模式了。这条路径的 fail-closed 已有测试承重：`core/tests/session/test_parts.py:625`（无身份）、`:393`（别人的 blob）、`:408`（`read_scope="device"`）。

### 2.4 客户端侧现状（旁证，不改客户端）

- `w-adapt` 的结论「blob 凭证须由 shell 外部供给」**成立**，且不是误判：客户端 `BlobUploaderReal` 只接收注入的 `BlobHttpChannel`、自己不看凭据（`vade-client` `composeApp/src/commonMain/kotlin/vade/ui/data/BlobUploaderReal.kt:19-26`），装配点 `sessionSourceFrom()` 在无配对码时构造纯 Noise 连接（`.../data/SourceConfig.kt:154-170`），拿不到 cookie；有配对码时只抓 `__Host-ac_ui`、**丢掉 `__Host-ac_csrf`**（`.../protocol/VadeTransport.kt:112-119`），而 `/ui/blob` 的 cookie 分支要求双提交（`blob/auth.py:140-155`）。
- `shots/w-adapt/RESULT.md:66` 的原话与 core 三处自有记录一致（`blob/auth.py:5-8`、`blob/RESULT.md:80-81`、`transport/RESULT-device-id.md` §未做 1）。

### 2.5 「六条路由一行不改」的依据

六条路由全部走同一个 `Authenticator.authenticate()`（`blob/auth.py:159-181`），没有任何端点有自己的一套：

| 方法 | 路径 | 处理器（`blob/http.py`） |
|---|---|---|
| POST | `/ui/blob`（begin） | `_begin` 205 |
| PUT | `/ui/blob/upload/{id}?offset=N` | `_put_part` 213 |
| HEAD | `/ui/blob/upload/{id}`（续传锚点） | `_head_upload` 224 |
| POST | `/ui/blob/upload/{id}/complete` | `_complete` 229 |
| DELETE | `/ui/blob/upload/{id}`（abort） | `_abort` 237 |
| GET\|HEAD | `/ui/blob/{file_id}[?v=thumb]` | `_get_blob` 242 |

Bearer 分支只做 `self._tokens.get_token(hash_token(bearer), now)`（`auth.py:160-171`）→ `BlobService.get_token`（`service.py:204-205`）→ `BlobStore.get_token`（`store.py:479-492`），**只看 `blob_tokens` 表**。本提案不需要它改一行：只是终于有人往那张表里写行了。

## 3. 目标 / 非目标

| | 内容 |
|---|---|
| G1 | 一条已配对的原生（Noise）连接，能**不借助任何外部输入**（不需要用户在 shell 里填 token、不需要浏览器）拿到一张上传凭据 |
| G2 | 拿到凭据后，客户端能跑完 `begin → PUT… → complete`，并 `HEAD` 续传、`DELETE` abort、`GET` 取回 |
| G3 | 用该凭据上传的 blob，其 `device_id` 与这条连接的 `device_id` **同一**，因此同一连接的 `ui/submit` 能引用它（`parts.py:519-522` 通过） |
| G4 | 凭据的生命周期（有效期、撤销、过期）与既有 `blob_tokens` 语义**完全重合**：没有第二套状态机、没有第二张表、没有第二个 TTL 概念 |
| G5 | 旧 core（无此方法）上**优雅降级**：附件入口显示「该 core 不支持附件上传」，**不弹错误、不重试、不影响纯文本发送**（§7） |
| N1 | **不做**逐请求设备密钥签名（RFC-0004 §11-B）：那是另一套 nonce 表与时钟窗 |
| N2 | **不做**「原生 `module#instance` ↔ 浏览器 `ui-…`」的归属映射：本提案让原生设备**在自己的身份下**上传与提交，跨端互通另开提案（`transport/RESULT-device-id.md` §未做 2） |
| N3 | **不改**任何现成路由的鉴权、body 上限、content-type 规则或错误码 |
| N4 | **不新增**事件 / 错误码 / 配置键 / 表 / 迁移 |
| N5 | **不实现**：本棒只交付「可被批准的设计」 |
| N6 | **不做** token 级的读隔离（默认 `read_scope="core"` 的跨设备可读性不在本提案收紧，理由与代价见 §6.4、Q7） |

## 4. 方法与字段表

### 4.1 签名

```
method : ui/blob.token       方向: ui_to_core       幂等性: 每次调用铸一张新 token（非幂等，无副作用地追加）
params : {ttl_s?: int}       严格：未知键 → INVALID_ARGUMENT
result : {token: string, expires_at: int(ms), max_bytes: int, endpoints: {…6}}
errors : INVALID_ARGUMENT | PERMISSION_DENIED | READ_ONLY_SESSION | UNSUPPORTED
```

可直接粘进 `schema/rpc.json` 的 `directions.ui_to_core`：

```json
"ui/blob.token": {
  "params": {
    "ttl_s": "int 60..900, optional, default 600 (absent -> 600; bool / float / string / null / out of range -> INVALID_ARGUMENT, data.field=\"ttl_s\", NO row written). Any other key is REFUSED with INVALID_ARGUMENT + data.field=<key>: this method mints a credential, so a silently ignored `device_id` / `read_only` would let a client believe it got something it did not."
  },
  "result": {
    "token": "string (43 chars base64url = secrets.token_urlsafe(32); only sha256(token) is stored, never the token itself)",
    "expires_at": "int (unix MILLISECONDS; mint_time + ttl_s)",
    "max_bytes": "int (per-file ceiling baked into this token at mint time = [attach].max_bytes; NEVER null — AttachSettings.max_bytes always has a concrete value, unlike Principal.max_bytes which is int|null for cookie principals)",
    "endpoints": {
      "begin": "POST /ui/blob",
      "part": "PUT /ui/blob/upload/{upload_id}?offset=N",
      "complete": "POST /ui/blob/upload/{upload_id}/complete",
      "abort": "DELETE /ui/blob/upload/{upload_id}",
      "resume": "HEAD /ui/blob/upload/{upload_id}",
      "fetch": "GET|HEAD /ui/blob/{file_id}[?v=thumb]"
    }
  },
  "errors": ["INVALID_ARGUMENT", "PERMISSION_DENIED", "READ_ONLY_SESSION", "UNSUPPORTED"],
  "notes": "RFC-0016 §4. The ONLY minting surface for a device bearer: the token is bound to the calling connection's proven `device_id` (Noise static key -> whitelist, transport/server.py:354-378); there is no `device_id` parameter and no other device to name (no oracle). Cookie sessions are refused (PERMISSION_DENIED, reason=\"cookie_session\") because they already hold a credential that works and minting would turn an ambient, four-gate cookie into a portable bearer. TTL 60..900 s, default 600; out-of-range or non-integer (including true and \"600\") is INVALID_ARGUMENT and writes NO row — the RPC layer validates and never relies on issue_token's silent clamp (service.py:184). Only sha256(token) is stored (blob_tokens), so a row survives a core restart; prune_tokens drops expired rows and revoke_device(device_id) drops the whole set. expires_at is unix ms. Expired, revoked and never-issued are indistinguishable (401 AUTH_FAILED, no oracle). The token is REPLAYABLE until it expires (an upload is 1 begin + N parts + 1 complete + 1 HEAD): it is a channel credential, not a one-shot. max_bytes is the value baked in at mint time ([attach].max_bytes) and `begin` enforces it. `endpoints` are aliases of the same listener's six routes; content-type and body limits still come from http_endpoints. Attachments disabled -> UNSUPPORTED with reason=\"attachments_disabled\" (the same code+reason ui/submit uses, session/parts.py:486-493). Explicitly read-only connection -> READ_ONLY_SESSION. No device identity -> PERMISSION_DENIED with reason=\"device_unknown\". The token authenticates HTTP only: it can never send a message."
}
```

### 4.2 参数表（逐字段）

| wire 键 | 类型 | 必填 | 默认 | 可空? | 校验与拒绝 |
|---|---|---|---|---|---|
| `ttl_s` | int | 否 | `600` | 否 | `60 ≤ ttl_s ≤ 900`。`bool`（含 `true`）、`float`（含 `600.0`）、`str`（含 `"600"`）、`null`、越界 → `INVALID_ARGUMENT`，`data.field="ttl_s"`，**不写行** |
| （任何其它键） | — | — | — | — | `INVALID_ARGUMENT`，`data.field=<键名>`，**不写行**。特别地 `device_id` / `session_id` / `read_only` 一律拒，绝不忽略 |

> `params` 是 `object`（不是 `{}`）。这一点与 RFC-0015 的 `ui/modules` 不同：那个方法没有任何输入，所以 `params:{}`；这个方法有 `ttl_s`，就必须有键集合，否则「未知键忽略」会让 `device_id` 变成一条静默的假声明。

### 4.3 回包字段表（逐字段，含可空标注）

| wire 键 | 类型 | 必填 | 可空? | core 来源 | 客户端接收 |
|---|---|---|---|---|---|
| `token` | string | 是 | **否** | `new_token()` = `secrets.token_urlsafe(32)`（`blob/auth.py:59-60`）；库里只存 `sha256(token)`（`store.py:471-477`） | 放进 `Authorization: Bearer <token>`，**只发这六条路由** |
| `expires_at` | int | 是 | **否** | `issue_token` 的 `_ms(expires_at)`（`service.py:63-68`、`:196-200`）；= 铸造时刻 + `ttl_s` | 定时器用；到期前主动重铸，不要等 401 |
| `max_bytes` | int | 是 | **否**（见下注） | 铸造时 `[attach].max_bytes`（默认 `104857600`，`settings.py:26`）；`begin` 用它判 413（`service.py:217-222`） | 本地预检文件大小；**不能**当成剩余配额 |
| `endpoints.begin` | string | 是 | 否 | 六条路由的字面路径（`rpc.json:1239-1279`，`check_contracts.py:792-810` 已钉住路由表） | 拼 URL |
| `endpoints.part` | string | 是 | 否 | 同上（含 `?offset=N` 模板） | 拼分片 URL |
| `endpoints.complete` | string | 是 | 否 | 同上 | 结束上传 |
| `endpoints.abort` | string | 是 | 否 | 同上 | 失败清理 |
| `endpoints.resume` | string | 是 | 否 | 同上 | 断点续传锚点 |
| `endpoints.fetch` | string | 是 | 否 | 同上（含 `[?v=thumb]` 变体） | 显示已上传附件 |

> 关于 `max_bytes` 的可空性：`Principal.max_bytes` 的类型是 `int | None`（`blob/auth.py:48`），cookie principal 的它是 `None`。但**本方法签发的一定是 int** —— `AttachSettings.max_bytes` 有确定默认值（`settings.py:26`），`issue_token` 的回包也只在 `max_bytes is None` 时才回退到 `settings.max_bytes`（`service.py:194-200`），两条路都得到 int。把这个字段写成 `int|null` 会让客户端为一个不可能出现的 `null` 写分支。**这是本表唯一需要显式讨论可空性的位置**；其余字段恒非空。

### 4.4 六个 `endpoints` 的边界

`endpoints` 是**同一 listener 上六条路由的别名**，不是第二份契约：

- 路径字符串必须与 `http_endpoints.endpoints[].path` **逐字一致**；`check_contracts.py:792-810` 已经在钉路由表，落地时新断言直接比这两处，不许手写第二份。
- 语义（body 上限、content-type、`Range`、幂等、413/415/429）**仍以 `http_endpoints` 为准**，`endpoints` 只省一次硬编码。
- 客户端**不得**从 `endpoints` 推断能力（例如「有 `fetch` 就等于支持缩略图」）：缩略图仍是 `?v=thumb` + 派生失败 415 的既有语义。

### 4.5 与 RFC-0004 §4.5 的差异（四处，评审时需逐条点头）

| # | RFC-0004 §4.5 原文 | 本提案 | 理由 |
|---|---|---|---|
| 1 | `errors` 只有 `READ_ONLY_SESSION` / `UNAVAILABLE` | 改成 `INVALID_ARGUMENT` / `PERMISSION_DENIED` / `READ_ONLY_SESSION` / **`UNSUPPORTED`** | 写 §4.5 时 `Connection.device_id` 还不存在（它是 `transport/RESULT-device-id.md` 那批才有），`ttl_s` 也没定边界。绑不到身份只能拒，用 `UNAVAILABLE` 描述「你没有身份」是撒谎，C-04 的 `PERMISSION_DENIED` 正是这个语义，且与 `parts.py:499` 同码同因；`ttl_s` 越界是 `INVALID_ARGUMENT` |
| 2 | `UNAVAILABLE`（附件关闭） | **`UNSUPPORTED`** + `data.reason="attachments_disabled"` | **草稿在这里是错的**：`ui/submit` 遇到同一条件（`service is None`）用的是 `UNSUPPORTED`（`parts.py:488-493`）。同一条件两个码会让客户端为「附件关掉了」写两个分支。选与实现一致的 `UNSUPPORTED` |
| 3 | `result.endpoints{begin,part,complete}` 三条 | 六条（补 `abort` / `resume` / `fetch`） | 失败清理必须 `DELETE`、断点恢复必须 `HEAD`、提交后回显必须 `GET`；只给三条等于让客户端自己拼路径。纯追加，无兼容面 |
| 4 | `max_bytes` 未说明来源 | 明确 = 铸造时 `[attach].max_bytes` | `issue_token` 的现有默认就是 `settings.max_bytes`（`service.py:194`），写实它，避免客户端猜 |

> 另有一处**未**沿用 RFC-0004：它写「token 是 32 字节随机串，只存 `sha256(token)`」——本提案补上「32 字节 → 43 字符 base64url」的确定形态，否则客户端验长度时无从下手。

## 5. 语义（逐条可验）

### 5.1 绑定与身份

1. token 行的 `device_id` = 调用连接的 `connection.device_id`；该列 `NOT NULL`（`store.py:78`），所以「绑不到身份」在写入前就必须拒。
2. 身份只来自传输层：Noise 静态键指纹 → 白名单（`server.py:354-378`、`whitelist.py:111-119`）。白名单没命中 ⇒ `device_id is None` ⇒ `PERMISSION_DENIED`（`data.reason="device_unknown"`），**不写行**。
3. 没有任何参数能改变绑定对象；`device_id` 键本身会被 `INVALID_ARGUMENT` 拒掉（§4.2）。因此**不存在**「为别的设备铸 token」的调用形态 —— 也就没有「这台设备是否存在」的预言机。
4. 六条路由的 Bearer 分支一行不改：`record.device_id` 直接成为 `Principal.device_id`（`auth.py:166-171`），于是 `begin` 的配额记账（`service.py:376-392`）、`BlobRecord.device_id`（`service.py:705-722`）、去重键 `(device, sha256, size)`（`service.py:220`）全都自动落到这台设备名下。

### 5.2 TTL 与「拒而不是夹」

5. `ttl_s` 缺省 600；`60 ≤ ttl_s ≤ 900`；越界或非整数 → `INVALID_ARGUMENT`，`data.field="ttl_s"`，**不写行**。
6. RPC 层必须自己做类型与范围判定，**不能**把值原样丢给 `issue_token`：后者是 `ttl = max(TOKEN_TTL_MIN, min(int(ttl_s), TOKEN_TTL_MAX))`（`service.py:184`）。`int()` 会接受 `"600"`、`True`（→1）、`1.5`（→1），再被夹成 60/600/900。**对凭据有效期做静默修正**的后果是客户端以为「我只要了 5 秒」，实际拿到 60 秒；或者以为「我要 1 小时」，实际 900 秒。这条在 §10 有专门断言（A4）。
7. 600 s 不是随便挑的：一次 100 MiB / 10 MiB 分片的手机上传在弱网下可能超过 60 s，但 900 s 以上的凭据已经比多数会话的注意力还长。常量已存在（`service.py:56-58`），本提案不新造。

### 5.3 只给原生连接（对草稿决定 7 的反转）

8. 判据用 `connection.browser_session`（`connection.py:119`，由 `server.py:311-320` 在 `/ui/session` cookie 命中时置真），**不用** `connection.ui_token` —— 后者是 `server.py:350` 动态挂的属性，不在 `Connection.__init__` 里，属于实现细节。
9. cookie 会话调用 → `PERMISSION_DENIED`，`data.reason="cookie_session"`。理由三条，任一独立成立：
   - 浏览器**已经有**一条能用的路：cookie + `Origin` + `Sec-Fetch-Site` + CSRF 双提交（`auth.py:115-156`）。铸 token 对它零收益。
   - 铸出来的 bearer **绕过**那四道闸：它不绑 Origin、不绑 `Sec-Fetch-Site`、不带 CSRF。等于把「环境凭据」升级成「可搬运凭据」，而 `auth.py:5-8` 的模块 docstring 明确把 cookie 定位成「只给浏览器上下文用的被动凭据」。
   - 默认 `read_scope="core"` 下，持有者知道 `blb_…` 就能读（§6.4）。凭据副本越少越好。
10. 客户端分支成本是一行：`if (browserSession) 走 cookie else 铸 token`。这两条路本来就都在客户端里（`SourceConfig.kt:154-170`）。

### 5.4 可重放（不是一次性）

11. token 在过期前**可重复使用**：`BlobStore.get_token` 命中后不删行（`store.py:479-492`），只有过期才删。这是既有语义，也是六条路由能工作的前提（1 次 begin + N 次 PUT + 1 次 complete）。
12. 一次性 token 的代价：每个分片铸一次 = 每个分片一次 ws 往返；且 `HEAD` 续传、`DELETE` abort、`GET` 回显都要再铸。收益是把「泄露窗口」从 600 s 缩到一次请求。本提案选**短 TTL + 按设备整批撤销**，不选一次性（Q3 留给用户改）。

### 5.5 上限、配额与去重

13. `max_bytes` = 铸造时的 `[attach].max_bytes`，写进 `TokenRecord.max_bytes`（`store.py:81`），经 `Principal.max_bytes` 在 `begin` 判 413（`service.py:217-222`）。它**不**随设备剩余配额收缩：配额是每次 `begin` 的实时判定（`service.py:376-392`），把动态值写进 token 会让「同一 token 今天能传、明天不能」变成不可解释的 413（Q2）。
14. 去重键含 `device_id`（`find_blob_by_content(device, sha256, size)`，`service.py:220`），所以 A 的 token 永远 dedupe 不到 B 的 blob —— 去重面不是跨设备泄露通道。

### 5.6 撤销、过期与无预言机

15. 三条撤销路径，全部复用现有实现：
    | 路径 | 实现 | 生效范围 |
    |---|---|---|
    | TTL 到期 | 行自然过期；`get_token` 命中过期行时顺手删（`store.py:484-486`） | 单张 token |
    | 周期清扫 | `prune_tokens(now)`（`store.py:494-497`），由 sweeper 周期调用 | 所有过期行 |
    | 设备撤销 | `BlobService.revoke_device(device_id)` → `store.revoke_device_tokens`（`service.py:207-208`、`store.py:499-502`） | 该设备**整批** token |
16. 过期 / 已撤销 / 从未签发**一律** `401 AUTH_FAILED`，响应字节相同（`auth.py:161-165` 的注释原文：「No oracle: expired, revoked and never-issued are identical」）。
17. **现状缺口（本提案要求补上）**：解绑一台设备时，`Whitelist.revoke()` 只回调 `on_revoke(fingerprint)`（`whitelist.py:200-209`），而 `App._revoke_connection` 只撤注册表里的连接（`app.py:144-146`），**从不调用 `blob.revoke_device(...)`**。也就是说今天解绑一台设备，它已签发的 token 仍活到 TTL 上限。因为本提案把 TTL 上限压在 900 s，这个缺口**不是**紧急漏洞，但必须在本方法落地时一并接上（§13 清单第 5 项）。`whitelist.revoke()` 的返回值就是被删的 `module#instance` 元组（`:209`），调用侧拿得到 id；要么在调用侧用返回值清，要么把回调签名扩成 `(fingerprint, removed)` —— 这是实现选择，本 RFC 只要求「解绑后该设备的 token 立刻失效」进验收（A9）。

### 5.7 只读与禁用

18. 显式只读的连接（`connection.read_only` 或 `connection.principal.read_only`，`session/rpc.py:518-522` 的同一判据）→ `READ_ONLY_SESSION`，不铸。理由：`blob_tokens.read_only` 列虽然存在（`store.py:80`），但本提案**不启用**它 —— 同一方法里塞「可写 token」与「只读 token」两种语义，等于让客户端靠一个布尔位猜行为（Q1 的附带问题，倾向另开提案）。
19. `attach.enabled=false` ⇒ `app.blob is None`（`blob/__init__.py:56-58`）⇒ `UNSUPPORTED`，`data.reason="attachments_disabled"`；同时六条路由仍然 404（`blob/http.py:138-140`）。两处同码同因，客户端只需一个分支。
20. 组件整体缺失（`app.blob` 不存在，dev 模式）→ 同一个 `UNSUPPORTED`。**不**用 `INTERNAL`：这不是故障，是「这个 core 没开附件」。

### 5.8 审计与并发

21. 每次成功铸造记一条 `blob.token` 审计（`BlobService._record`，`service.py:161-171`），字段 `kind/status/device_id/expires_at`，**不含 token 明文、不含 `sha256(token)` 的前缀**。失败不写「成功」行。
22. 同一设备可同时持多张 token（各一行、各自 TTL），互不排斥；客户端过期重铸即可，不需要「先撤旧的」。
23. 并发铸造 N 次 = N 行（`INSERT OR REPLACE` 以 `token_hash` 为主键，碰撞概率可忽略），去重不做 —— 做「每设备最多 1 张」会引入踢掉正在上传的那张的竞态。

## 6. 安全分析

### 6.1 信任边界与绑定对象

| 问题 | 答案 | 依据 |
|---|---|---|
| token 绑谁？ | **`connection.device_id`**，即 Noise 静态键 → 白名单给出的 `module_id#instance_id` | `server.py:354-378`、`whitelist.py:111-119` |
| 能不能绑 cookie 会话？ | 能，但本提案**拒**（§5.3） | `auth.py:115-156` 的四道闸把 cookie 定位成浏览器专用被动凭据 |
| 客户端能不能指定绑定对象？ | 不能。没有 `device_id` 参数，出现即 `INVALID_ARGUMENT` | §4.2；`server.py:364-370` 的注释：「guessing an identity from anything the peer sent over the wire … would turn a hint into a claim」 |
| 凭据的权威来源是什么？ | 传输层在 IK 握手**验过**的静态键；`read_handshake` 在 msg2 之前就拒掉不在白名单的键 | `server.py:355-362` |
| 一张 token 能做什么？ | **只能走 HTTP 的六条路由**。它不能 `ui/submit`、不能 attach、不能读会话 —— 那些要 ws 连接，而 ws 的身份来自 Noise/cookie，不来自这张 token | `app.py:222-241`（ws dispatch 只看 connection）、`session/rpc.py:149-155` |

这一条很重要：**token 泄露 ≠ 会话被接管**。攻击者拿到 token 只能以该设备名义搬字节（消耗其配额、可能读到已知 id 的 blob），不能发消息、不能读历史。

### 6.2 有效期与「一次性 vs 可重放」

- 有效期 60..900 s，默认 600。到期后行被惰性删除（`store.py:484-486`），无需客户端做任何事。
- **可重放**：TTL 内同一 token 可反复用于任何分片与任何一次重试。这是六条路由的工作前提（§5.4）。
- 缓解手段不是「一次性」，而是：短 TTL + 按设备整批撤销（`revoke_device`）+ 只存哈希（DB 泄露拿不到可用 token）+ 只在 Noise 加密的 ws 上发一次。
- **token 不放在 URL 里**（`Authorization` 头），所以不会进访问日志、不会进 `Referer`。这条要写进客户端接线要求（§8）。

### 6.3 撤销路径与 `BlobService.revoke_device` 的关系

`revoke_device(device_id)` 的语义**不新造**：它已经是「删该设备所有 token 行」（`service.py:207-208`），且已有测试（`core/tests/blob/test_auth.py:73`）。本提案要求的是**接线**，不是新语义：

```
Whitelist.revoke(fingerprint)
  ├─ 删白名单条目，返回 (module_id#instance_id, ...)     whitelist.py:200-209
  ├─ on_revoke(fingerprint) → App._revoke_connection     app.py:78, 144-146
  │     └─ registry.revoke_fingerprint(...)              只关连接
  └─ 【缺失】blob.revoke_device(每个被删的 device_id)     本提案要求补上
```

补上之后，「解绑设备」= 连接断 + 注册表清 + **token 立刻失效**，三件事同一语义。不补的话最坏窗口 = TTL 上限（900 s），且**只在解绑后**存在。

### 6.4 越权读别人的 blob：怎么被拒、哪里不拒

必须分三层说，否则会写成假话：

| 层 | 行为 | 依据 |
|---|---|---|
| **提交面（强）** | 用别人的 `file_id` 发消息 → `PERMISSION_DENIED`，**与 `read_scope` 无关**。这是硬闸：`resolve_attachments` 逐个比对 `blob.device_id != device_id` | `parts.py:519-522`；测试 `test_parts.py:393` |
| **无身份（强）** | 连接没有 `device_id` 却提交带 `file_id` 的 part → `PERMISSION_DENIED`，`reason="device_unknown"`，不落库 | `parts.py:493-500`；测试 `test_parts.py:625` |
| **HTTP 读取面（弱，且是既有设计）** | 默认 `read_scope="core"` 时，**持有有效 token 者可读任何已知 `file_id` 的 blob**；只有 `read_scope="device"` 才按设备隔离 | `service.py:816`、`settings.py:39` |

第三层不是本提案引入的：它自 RFC-0004 起就是默认值，理由是**多设备历史图片必须能渲染**（手机传的图，桌面要看得见）。所以：

- **能怎么被拒**：拿不到 id。`file_id` 是随机生成的 `blb_…`（`blob/ids.py`），core **没有任何列出 blob 的方法**（`ui_to_core` 35 个方法里没有「列附件」，只有按 id 取），唯一的 id 来源是「自己的上传回包」与「自己已经能看到的会话消息」。
- **不能声称什么**：不能声称「token 绑了设备 ⇒ 只能读自己设备的 blob」。在默认配置下这句话是**假的**。本 RFC 不写假话，也不顺手收紧（收紧会让桌面看不到手机的历史图片，属产品决策，见 Q7）。
- **想收紧的现成开关**：`attach.read_scope="device"`（`settings.py:39`、`service.py:816`）已经存在，`test_parts.py:408` 承重。它是运维/产品选择，不是本方法的能力。

### 6.5 泄露面与日志

| 面 | 处置 |
|---|---|
| 传输 | 只在已建立的 ws 上发一次；Noise 加密（原生连接）。不经 HTTP 明文、不进 URL、不进 `Referer` |
| 落库 | 只存 `sha256(token)`（`store.py:76-82`）；DB 被读走也拿不到可用凭据 |
| 审计 | `kind=blob.token`，只有 `device_id` / `expires_at` / `status`；**永不写 token 或其哈希前缀**（A10 用全文搜索承重） |
| 日志 | 客户端不得把 `Authorization` 头写进日志（接线要求，§8.3） |
| 枚举 | 没有 `device_id` 参数 ⇒ 无法用它探测「哪些设备已配对」；没有「列 token」的方法 ⇒ 无法枚举自己的凭据 |
| 错误面 | 铸造失败的四种原因都是**确定的**（ttl 坏 / 无身份 / cookie / 只读 / 附件关），没有一个依赖「这个设备存在吗」 |
| 重放 | TTL 内可重放是设计（§5.4）；缓解靠短 TTL + 整批撤销，不靠一次性 |

### 6.6 为什么一个错误码都不新增

C-04 冻结 34 个码（`schema/errors.json`；`tools/check_contracts.py:1540` 硬断言 `len(codes)==34`）。本方法的五种失败形态逐个映射：

| 失败形态 | 复用码 | 区分靠什么 |
|---|---|---|
| `ttl_s` 类型/范围错 | `INVALID_ARGUMENT` | `data.field="ttl_s"` |
| 未知参数键（含 `device_id`） | `INVALID_ARGUMENT` | `data.field=<键名>` |
| 连接无设备身份 | `PERMISSION_DENIED` | `data.reason="device_unknown"`（与 `parts.py:499` 同码同因） |
| cookie 会话调用 | `PERMISSION_DENIED` | `data.reason="cookie_session"` |
| 显式只读 | `READ_ONLY_SESSION` | 无 `data`（与 `session/rpc.py:141-144` 同形） |
| 附件功能关闭 / 组件缺失 | `UNSUPPORTED` | `data.reason="attachments_disabled"`（与 `parts.py:492` 同码同因） |

**明确不新增**：`BLOB_TOKEN_DENIED`、`TOKEN_EXPIRED`、`DEVICE_UNBOUND`、`ATTACH_DISABLED`。理由三条：

1. 每加一个码，`check_contracts.py:1540` 的 `len(codes)==34` 要动、C-04 的 34 码台账要动、所有客户端的 `when(code)` 分支要动；收益是零（上面的映射已覆盖）。
2. `TOKEN_EXPIRED` 这类码会**制造预言机**：C-04 的 no-oracle 纪律要求「过期 / 撤销 / 从未签发」不可区分（`auth.py:161-165`），单独一个「过期」码直接破坏它。
3. C-04 的既有形态就是「码 + `data.reason`」，`parts.py` 已经在用（`attachments_disabled` / `device_unknown` / `scan_pending`）。跟随既有形态而不是发明新码，是这条契约的既定纪律。

## 7. 客户端降级（旧 core 上没有这个方法）

### 7.1 先订正一件事：本仓的 core 不会回 `-32601`

`RFC-0015 §6/§7.1`、`RFC-0006 §6.5`、`schema/rpc.json:62,107,154,168,1209` 都写「未注册方法 → JSON-RPC `-32601`」。**这句话在本仓是错的**，证据链完整：

| 层 | 事实 | 位置 |
|---|---|---|
| dispatch | 未注册方法返回 `RpcError("NOT_FOUND", f"unknown method {method!r}")` —— **字符串码** | `core/vade/session/rpc.py:52-55`、`core/vade/app.py:237-241` |
| 序列化 | `{"jsonrpc":"2.0","id":…,"error": result.to_dict()}`，`to_dict()` = `{"code": <str>, "message": <str>}` | `core/vade/transport/connection.py:59-63`、`:234-235` |
| 实测 | `SessionRPC(log=…).dispatch(conn, "ui/blob.token", {})` → `{'code': 'NOT_FOUND', 'message': "unknown method 'ui/blob.token'"}` | 本棒实跑（Python，非猜测） |

所以客户端**不能**只判 `code == -32601`：那会让它在真机上永远认不出「方法不存在」。判定必须是**析取**：

```
methodAbsent(err) =
     err.code == -32601                                  // JSON-RPC 标准码；本仓从不产生，留给代理/极旧构建兜底
  || (err.code == "NOT_FOUND" && err.message.startsWith("unknown method"))
```

两行的成本，换掉一整类「降级识别不出来 → 当成故障弹窗」的 bug。落地批次**顺手**把 `rpc.json` 那五处 note 与 `rpc.py:82` 的注释改成实话（**不改行为**，只是文案），并给 `check_contracts.py` 加一条「note 里不许再出现 `-32601` 作为预期行为」的断言 —— 这是文案修正，不属本棒。

### 7.2 三态与表现

| 铸造结果 | 客户端状态 | 表现 | 禁止 |
|---|---|---|---|
| 成功 | `Supported` | 附件入口可用；拖放/粘贴/文件选择正常 | — |
| `NOT_FOUND` + `unknown method`（或 `-32601`） | **`CoreTooOld`**（每连接 epoch 判一次） | 附件入口置灰 + 一行说明「**该 core 不支持附件上传**（需要更新 core）」；拖放时显示同一行内联说明，**不弹对话框**；纯文本照常发送 | 不许 toast/错误卡、不许重试循环、不许把 `attachment` 按钮藏起来又留个死快捷键 |
| `UNSUPPORTED` + `attachments_disabled` | `Disabled` | 「附件功能已在此 core 上关闭」（配置原因，非版本原因） | 不许说「更新 core」——那会误导用户去升级一个没坏的东西 |
| `PERMISSION_DENIED` + `device_unknown` | `NoDeviceIdentity` | 「此设备未完成配对，无法上传附件」+ 指向配对入口 | 不许静默降级成「上传中」再失败 |
| `PERMISSION_DENIED` + `cookie_session` | 不应发生（浏览器不该调它） | 客户端自身 bug：记一条本地诊断，走 cookie 上传路径 | 不许弹用户可见错误 |
| `READ_ONLY_SESSION` | `ReadOnly` | 「只读会话不能上传」 | 不许自动重试 |
| 其它（网络/超时/`INTERNAL`） | `FailedOnce` | 退避重试 ≤1 次/30 s，最多 2 次 | 不许在这个路径上改变附件入口的可用性 |

### 7.3 降级的硬约束

1. **`CoreTooOld` 只在连接 epoch 内探测一次**：一次调用、一次判定；同一连接不重复探。重连（新 epoch，例如 core 升级后重连）清标记、再试一次。
2. **绝不回落 cookie**：原生连接拿 cookie 去传，`ui/submit` 必拒（§2.3）。「上传成功但消息发不出去」比「上传入口置灰」坏得多。降级必须是**功能不可用**，不是**假成功**。
3. **纯文本路径零影响**：附件是加法；`ui/blob.token` 失败不得阻断 `ui/submit` 的文本分支，也不得让 `ui/attach` 失败。
4. **不许伪造**：不许客户端自己生成 token、不许跳过 `Authorization` 直传（会 401）、不许把 `file_id` 猜出来塞进 part。
5. **文案分层**：「core 太旧」与「附件被关掉」是两句不同的话（上表），因为用户的下一步动作不同（升级 vs 找管理员开开关）。

## 8. 兼容性

| 面 | 现状 | 加方法后 |
|---|---|---|
| 老客户端 | 不调用 `ui/blob.token`；浏览器走 `/ui/session` cookie | 行为零变化（多一个方法对它不可观测） |
| 老 core + 新客户端 | 方法不存在 → `NOT_FOUND` + `unknown method`（§7.1） | 客户端按 §7.2 降级为「不支持附件上传」，**不报故障** |
| 新 core + 老客户端 | 方法存在但没人调 | 零影响；不构成强制升级 |
| 六条 HTTP 路由 | 鉴权只有 `Authenticator.authenticate`（`http.py:207/215/225/231/238/243`） | **一行不改**：Bearer 分支本来就查 `blob_tokens`，本提案只是终于有人往那张表里写行 |
| cookie 路径 | 四道闸（`auth.py:115-156`） | 不动 |
| 只读语义 | `Principal.require_write()`（`auth.py:50-56`）在 `begin` 拒写 | 不动；铸造面先用同一判据早拒 |
| 配额/去重/GC | 按 `device_id` 记账（`service.py:376-392`、`:220`、`:912-940`） | 不动；token 让「设备」这个维度第一次真的被填满 |
| 错误码 | C-04 冻结 34 | 复用 4 个，**不加码** |
| 事件 | C-06 34 条 | 不发事件（铸造不是会话状态变更） |
| 配置 | `[attach]` 键集冻结（C-18） | 不加键；关闭时 `UNSUPPORTED` |

## 9. 契约变更

### 9.1 汇总

| # | 契约 / 文件 | 变更 | 类型 |
|---|---|---|---|
| 1 | C-05 `directions.ui_to_core` | 追加 `ui/blob.token`（`params` / `result` / `errors` / `notes` 四件齐全，形状见 §4.1） | 追加 |
| 2 | C-05 `x-vade.appends` | 追加第 **12** 块：`contract:"C-05"`、`version:"1.7.0"`、`owner:"C2"`、`doc:"docs/rfc/RFC-0016-blob-token.md"`、`adds:["ui/blob.token"]`、`rewrites:"none"`、`notes` 三条（绑身份 / 只给原生 / 无新码无新事件） | 追加 |
| 3 | C-05 顶部 `version` | 1.6.0 → **1.7.0** | 升 minor |
| 4 | C-05 `http_endpoints.auth.device` | 文案从 `"Authorization: Bearer *** minted over the already-authenticated device channel>"` 改成 `"Authorization: Bearer <token from ui/blob.token>（绑调用连接的 device_id，TTL 60..900 s）"` | 注记（不改结构） |
| 5 | C-04 `errors.json` | **不动**（仍 34） | — |
| 6 | C-06 `events.json` | **不动**（仍 34 条，逐字节不变） | — |
| 7 | C-03 `manifest.schema.json` | **不动** | — |
| 8 | C-18 `config.schema.json` | **不动**（`[attach]` 键集不变） | — |
| 9 | C-09（UI 组件） | **不动**：`AttachmentUploader` 的形状不因凭据来源而变 | — |

计数口径（落地后应成立）：`ui_to_core` **35 → 36**，三方向合计 **48 → 49**；`appends_rpc` **11 → 12**；`appends_events` 仍 **8**；`appends_manifest` 仍 **3**；错误码仍 **34**；事件仍 **34**。

### 9.2 为什么是 1.7.0（append-per-minor），而不是「悄悄加个方法」

- C-05 的既有纪律：每个 RFC 的追加都有自己的块级版本，块版本 ≠ 顶部版本（RFC-0013 块 1.2.0 而顶部当时 1.3.0，见 LEDGER §4-4）。这次两者都是 1.7.0，因为本块就是 head 上的下一次追加。
- 不升版本会让 `check_contracts.py` 的三处版本钉继续声称「head 是 1.6.0」，而那三处断言的存在意义正是「顶部版本必须能指名是谁追加的」。
- 追加是**纯加法**：没有既有方法被改、没有既有字节被改写、`rewrites:"none"`。

### 9.3 为什么 `http_endpoints.auth.device` 必须一起改

那句话是客户端唯一能读到的「凭据怎么来」的说明。它现在既没点名方法，`<` 还掉了（§2.1 逐字引用），读起来像一个被截断的占位符。方法落地而不改这句话，等于契约里仍然写着「有一条路」却不说是哪条 —— 下一个客户端还会再猜一次。这是**注记**级别的改动（字符串内容），不动 `http_endpoints` 的结构，不动六条路由的 `path`/`body_max`/`content_types`。

### 9.4 台账口径（本棒不动 schema，因此也不能动这些数字）

本棒只写文档，所以**不能**把 `appends_rpc` 记成 12：契约里还没有这一块。落地那一棒才动 `docs/rfc/LEDGER.md` 的 aggregate 与 RFC-0016 行（`impl_state` 从 `未实施` 改 `已实现`、填 impl 路径与测试数）。本棒在 LEDGER 里加的那一行按实际状态写 `未实施`，这是真话，也是 `tools/check_ledger.py:203-208` 允许的唯一写法（无 append 块的 RFC 只能 `未实施` / `客户端仓（未核实）`）。

## 10. 验收条件（可机械判定）

A1–A12 要求真实执行；截图与手写记录不算证据（沿用 RFC-0004 / RFC-0015 口径）。落点为计划路径。

| # | 断言 | 落点 |
|---|---|---|
| A1 | `schema/rpc.json` 的 `ui_to_core` 有 `ui/blob.token`，`params`/`result`/`errors`/`notes` 与 §4.1 逐键一致；`tools/check_contracts.py` 新增的 `check_blob_token()` 覆盖它 —— 改任一键名、删一个 `endpoints`、把 `errors` 换成 `UNAVAILABLE`，三种变异都必须红 | `core/tests/blob/test_rpc_token.py` + `tools/check_contracts.py` |
| A2 | 一条真 Noise + 白名单的原生连接调用 → 200/`result`；用该 token 跑完 `begin → PUT（≥2 片）→ complete`，再 `HEAD` 续传、`DELETE` abort、`GET` 取回（含 206 单段 Range） | `core/tests/blob/test_rpc_token.py`、`core/tests/e2e/test_blob_token_ws.py` |
| A3 | token 绑身份：A 的 token 上传的 blob，`BlobRecord.device_id == A 的 connection.device_id`；同一连接的 `ui/submit` 引用该 `file_id` **成功建 turn**；换 B 的连接提交同一 `file_id` → `PERMISSION_DENIED`（`parts.py:519-522`） | `core/tests/blob/test_rpc_token.py` + `core/tests/session/test_parts.py` 既有用例 |
| A4 | `ttl_s` 边界：缺省 → 600；`60` / `900` 接受；`59` / `901` / `1.5` / `true` / `"600"` / `null` → `INVALID_ARGUMENT`、`data.field=="ttl_s"`、**`blob_tokens` 零新增行**（逐个断言，不许只测一个） | `core/tests/blob/test_rpc_token.py` |
| A5 | `expires_at` 是 unix **毫秒**，落在 `[now + ttl_s − 2 s, now + ttl_s + 2 s]` 窗口内；`token` 长度 43、字符集 `[A-Za-z0-9_-]` | 同上 |
| A6 | 过期后同 token → `401 AUTH_FAILED`，与「从未签发」「已撤销」三者**响应字节完全相同**（无预言机） | 同上 + `core/tests/blob/test_auth.py` 既有口径 |
| A7 | `device_id is None` 的连接（未列白名单）→ `PERMISSION_DENIED`、`data.reason=="device_unknown"`、`blob_tokens` 零新增行 | 同上 |
| A8 | 参数严格性：`{"ttl_s":600,"device_id":"module#victim"}` → `INVALID_ARGUMENT`、`data.field=="device_id"`，**且没有为该 device 写行**；`{"read_only":true}` / `{"session_id":"s1"}` 同 | 同上 |
| A9 | 重启 core 后未过期的 token 仍能用（行在 sqlite）；`revoke_device(device_id)` 后同 token **立刻** 401；**解绑白名单设备后同 token 立刻 401**（这是 §5.6-17 的接线断言） | `core/tests/blob/test_rpc_token.py`、`core/tests/identity/test_revoke_purges_tokens.py` |
| A10 | 每次成功铸造写一条 `blob.token` 审计（`kind/status/device_id/expires_at`）；把审计文件全文搜 token 字面量 → **0 命中**；失败不写「成功」行 | 同上 |
| A11 | 旧 core 模拟（handler 表里删掉该方法）→ 客户端单测：状态 `CoreTooOld`、**0 弹窗 / 0 重试**、附件入口置灰并显示「该 core 不支持附件上传」、纯文本 `ui/submit` 仍成功；重连后只重探一次；`-32601` 与 `NOT_FOUND+unknown method` 两种形状都判为 `CoreTooOld` | `vade-client` `BlobTokenDegradeTest.kt` + `core/tests/blob/test_rpc_token.py` 的 dispatch 形状断言 |
| A12 | **端到端（真机/真桌面）**：桌面拖一个 1 MiB 文件进会话 → 消息里出现附件 chip → **刷新（关窗重开 / 重新 attach）后 chip 仍在**（`ui/attach` 的 `snapshot.messages[].parts` 带该 part），且 `blobs` 表对应行 `pinned=1`、`sha256` 与源文件一致 | `shots/<impl-batch>/` 截图 + `core/tests/e2e/test_blob_token_ws.py`（协议层复现：submit 后用全新连接 `ui/attach` 取回） |

文档级：本 RFC 落地后 `python docs/check_docs.py` 仍 12 项全绿（新文件不参与 C1–C10 的计数，实测见本棒报告）。

## 11. 未决问题（需要用户拍板，本 RFC 不自作决定）

| # | 问题 | 本提案的默认 | 备选与代价 |
|---|---|---|---|
| Q1 | **token 绑谁？** 设备身份（`module#instance`）／cookie 会话／两者皆可（草稿的决定 7） | 只绑设备身份，cookie 会话拒 | 「两者皆可」= 把浏览器 cookie 升级成可搬运 bearer（§5.3 三条理由）；若产品确实需要「浏览器也能拿 bearer」（例如 WebView 里跑非浏览器 HTTP 栈），要单独论证它如何不绕过四道闸 |
| Q2 | **有效期多长？** 600 s（60..900）够不够 | 沿用现有常量 | 更短（120 s）→ 弱网长传要中途重铸，客户端必须实现「401 → 重铸 → 重试当前分片」；更长（>900）→ 超出既有常量，等于改 `TOKEN_TTL_*`，且泄露窗口变大 |
| Q3 | **一次性还是 TTL 内可重放？** | 可重放 | 一次性要改六条路由的鉴权模型（每片一铸），并把 `HEAD`/`GET` 也变成「一次性」——不推荐，但技术上不是不可能 |
| Q4 | **解绑设备是否立刻清 token？** | 要（补 `revoke_device` 接线） | 不补 = 最坏 900 s 窗口；补的代价是 `whitelist.on_revoke` 的回调签名（现在只给 fingerprint）要动，属 core 改动 |
| Q5 | **跨设备/跨主机复用要不要允许？** 持有即可用 vs 要求同源证明 | 持有即可用（bearer 本义） | 要求同源证明 = 逐请求设备签名（RFC-0004 §11-B），要 nonce 表 + 时钟窗；这是另一个 RFC 的量级 |
| Q6 | **参数严格还是忽略未知键？** | 严格（未知键 → `INVALID_ARGUMENT`） | 宽松 = 前向兼容更好，但 `device_id` 会被静默忽略，客户端可能误以为自己铸了别的东西；宽松还要额外断言「忽略后绑定不变」 |
| Q7 | **要不要把读面收紧成「token 只能读自己设备的 blob」？** | 不（维持 `read_scope="core"` 现状） | 收紧 = 桌面看不到手机上传的历史图片（同一 session 跨设备），且要动 `stat()`/`settings` 语义；这是产品决策，不是本方法的附带品 |
| Q8 | **要不要给只读连接发 `read_only=true` 的 token？**（`blob_tokens.read_only` 列已存在，`store.py:80`） | 不发（只读连接直接 `READ_ONLY_SESSION`） | 发 = 原生只读设备也能看历史图片；代价是同一方法两种语义，客户端要靠布尔位猜行为。倾向另开提案 |

## 12. 备选方案（未采用）

| 方案 | 为什么不采用 |
|---|---|
| A. 客户端伪装浏览器补齐 cookie 四道闸（`Origin` + `Sec-Fetch-Site` + CSRF 双提交） | 客户端从来没保存过 CSRF（`VadeTransport.kt:112-119` 把 `__Host-ac_csrf` 丢了），`Origin` 还要求它在 `server.ui_origins` 精确白名单里（默认 `[]`，`core/vade/config.py:198-206` 禁 `*`）。这等于让非浏览器上下文伪造浏览器证据 —— `blob/auth.py:124-131` 的注释明说这种被动凭据不该给非浏览器用 |
| B. 逐请求设备密钥签名（RFC-0004 §11-B） | nonce 表 + ±300 s 时钟窗 + 每片一次签名；成本高于一次 RPC，且要动 `admin/auth.py` 的 p256/ed25519 解析面 |
| C. 把 token 放进 URL query（`/ui/blob?token=…`） | 进访问日志、进 `Referer`、进浏览器历史；`Authorization` 头是唯一不落这些地方的位置 |
| D. 让 `ui/submit` 接受「cookie 上传的 blob」 | 需要一张 `ui-…` ↔ `module#instance` 的归属映射表，且要定「谁能声明这层映射」——那是另一个提案（`transport/RESULT-device-id.md` §未做 2），且会让归属校验从「比对」退化成「查表」 |
| E. 开放 `/ui/blob`（无凭据直传） | 任何同网段进程都能写盘、吃配额、读回；不可接受 |
| F. 每个分片一张一次性 token | 见 Q3；每片一次 ws 往返 + `HEAD`/`GET` 无法覆盖 |
| G. 复用 `/ui/session` 的 browser 配对码给原生端换 cookie | `pairing.consume(code, "browser")` 用途固定（`identity/pairing.py:68-71`、`:229-235`），且换出来的是**新** `ui-…` 身份，与原生 `module#instance` 不同源 —— 正是 §2.3 的死结 |

## 13. 影响面与后续

### 13.1 本棒落了什么 / 没落什么

**落了**：`docs/rfc/RFC-0016-blob-token.md`（本文件）；`docs/rfc/README.md` 索引两行（0015 补登 + 0016）；`docs/rfc/LEDGER.md` 一行（`impl_state=未实施`）+ §2/§3 的对应条目；`docs/status.md` 附录一行；`docs/rfc/DRAFT-blob-token.md` **删除**（§14）。

**没落（明确不属本棒）**：`schema/**` 一行未动、`core/**` 一行未动、`sdk/**` 一行未动、`tools/check_*.py` 一行未动、客户端一行未动。

### 13.2 实现阶段要动的文件

| 文件 | 要做什么 | 锚点 |
|---|---|---|
| `schema/rpc.json` | 加 `ui/blob.token`；加 append 第 12 块；顶部 1.6.0 → 1.7.0；改 `http_endpoints.auth.device` 文案 | `:3`（version）、`:16-…`（appends 数组）、`:420-…`（`ui_to_core`）、`:1217`（auth.device） |
| `core/vade/blob/rpc.py`（新） | `BlobTokenRPC`：`handlers() -> {"ui/blob.token": self.mint}`；`mint()` 做 §4.2 严格校验 → 判 `browser_session` / `device_id` / `read_only` → 调 `BlobService.issue_token` → 组装 `endpoints` | 照 `core/vade/modulepage/rpc.py` 的形状 |
| `core/vade/blob/__init__.py` | `install()` 里发布 handler：写进已存在的 `app.session_handlers`（`setdefault`）**并且**总是 append 到 `app.handler_providers`；`app.blob is None` 时 handler 仍要存在（回 `UNSUPPORTED`） | 照 `modulepage/__init__.py:27-59` 的两条路径写法（那一段的注释解释了为什么两条都要） |
| `core/vade/blob/service.py` | `issue_token` 的**夹取保留为内部兜底**；RPC 层单独做严格判定（两处口径必须一致：RPC 拒、service 兜底）。审计 kind 用 `blob.token`（复用 `_record`） | `:173-201`、`:184` |
| `core/vade/app.py` | 解绑设备 → `blob.revoke_device(device_id)`（§5.6-17） | `:78`（`on_revoke` 来源）、`:144-146` |
| `core/vade/identity/whitelist.py` | `revoke()` 已返回被删的 `module#instance`（`:209`）；要么在调用侧用返回值，要么把回调签名扩成 `(fingerprint, removed)` | `:200-209` |
| `tools/check_contracts.py` | ① 三处版本钉**追加** 1.7.0（保留 1.3.0–1.6.0）；② 新 `check_blob_token()`：方法在 `ui_to_core`、`result` 键集合、`endpoints` 六个键、`errors ⊆ C-04`、`ttl` 常量与 `service.py:56-58` 相等、handler 真在 `app.session_handlers` 里（照 `check_session_meta` 对 RFC-0010 的做法，`:1341-1346`）；③ 断言 `http_endpoints.auth.device` 含 `ui/blob.token` | `:1096-1097`、`:1356-1357`、`:1480-1481`、`:1540`（`len(codes)==34` 不动） |
| `tools/check_ledger.py` | **不改代码**；只让 LEDGER 的数据跟上（aggregate 与 RFC-0016 行） | — |
| `docs/rfc/LEDGER.md` | §1 表：版本 1.7.0、`ui_to_core` 36、合计 49、appends_rpc 12；§2 加 RFC-0016 行（`已实现` + impl 路径 + 测试数）；§3 facts 块同步 | — |
| `docs/status.md` | 落地批次回填一行 | — |
| `core/tests/session/test_meta.py:639`、`test_rfc0013_partial_persistence.py:488`、`test_rfc0014_search.py:481` | 三处「C-05 顶部版本」断言 1.6.0 → 1.7.0（沿用 RFC-0014 `6b568b9` / RFC-0015 `s-rfc0015-register` 的同一做法：只改版本号与相邻注释，断言仍是**精确相等**） | — |
| 客户端仓 `vade-client` | `BlobHttpChannel` 在原生模式先调 `ui/blob.token`；新增「凭据源」抽象；`CoreTooOld` 三态（§7.2）；`Authorization` 头不进日志 | `BlobUploaderReal.kt:19-26`、`SourceConfig.kt:154-170` |

### 13.3 版本与计数（落地时须逐项对齐）

| 项 | 现在 | 落地后 |
|---|---|---|
| `schema/rpc.json` 顶部 `version` | 1.6.0 | **1.7.0** |
| `ui_to_core` 方法数 | 35 | **36** |
| 三方向合计 | 48 | **49** |
| `x-vade.appends` 块数（rpc.json） | 11 | **12** |
| `events.json` 顶部 / 条数 / appends | 2.0.0 / 34 / 8 | 不变 |
| `errors.json` 码数 | 34 | 不变 |
| `manifest.schema.json` appends | 3 | 不变 |

### 13.4 门禁咬合（落地时必须实测，不许口头声称）

- `tools/check_contracts.py`：三处版本钉**只追加**；把顶部临时改成 1.8.0 后仍须 FAIL（证明钉还在咬），把 `endpoints` 的 `fetch` 删掉后 `check_blob_token()` 须 FAIL。
- `tools/check_ledger.py`：`aggregate.rpc_methods_by_direction.ui_to_core` 36 / `rpc_methods_total` 49 / `appends_rpc` 12；RFC-0016 行 `impl_state=已实现` 且 impl 路径真实存在（`check_ledger.py:203-223`）。
- `python docs/check_docs.py`：12/12（本棒已实测通过）。
- `pytest core/tests -q`：全量绿（本棒不跑，纯文档）。

## 14. 草稿处置：**删除** `docs/rfc/DRAFT-blob-token.md`，不保留「已被取代」的副本

选择「删」而不是「标注取代」，理由四条：

1. **内容已被完整吸收**：草稿的每一条证据（六条路由的鉴权行、`issue_token` 零调用方、`ui-…` vs `module#instance`、`rpc.json:1217` 的文案、RFC-0004 §4.5 的形状）都进了本文件 §2 / §4，且行号按当前 HEAD 重新核过（草稿引的是旧行号，例如它说 `rpc.json:1169`，现在是 `:1217`；说 `blob/auth.py:159-181`，现在 `authenticate` 在 159、文件到 181 结束，一致，但 `_begin` 等行号已漂）。
2. **草稿含三处已被本 RFC 推翻的断言**：① 附件关闭用 `UNAVAILABLE`（错，实现是 `UNSUPPORTED`）；② 「不是 `-32601`」那段方向对但引用的行号与结论需要重写（§7.1）；③ 决定 7「浏览器也允许铸造」（本 RFC 反转，§5.3）。留一份**错的**设计文档在 `docs/rfc/` 里，是给下一个 agent 埋雷。
3. **本仓自己的纪律**：「两份拼装迟早漂移」（RFC-0015 §4.2 尾注、RFC-0009 §9.2 的「schema 有、`Manifest` 丢」）。同一份设计留两份文本，等价于第二个真相源。
4. **可恢复**：草稿在 git 历史里，取回命令写在这里 —— `git log --oneline -- docs/rfc/DRAFT-blob-token.md`，然后 `git show <sha>:docs/rfc/DRAFT-blob-token.md > /tmp/draft.md`。审计链没断，只是不再占用工作树。

`docs/rfc/README.md` 的索引里从未登记这份草稿（它只有 RFC-0001…0014 的行），所以删除不需要改索引表；本次只补 0015 与 0016 两行。

---

**参考**：`schema/rpc.json:3/16-…/1217/1239-1279`、`schema/errors.json`（34 码）、`core/vade/blob/http.py:130-247`、`core/vade/blob/auth.py:44-56/59-61/111-181`、`core/vade/blob/service.py:56-68/161-208/211-226/376-392/695-720/796-817`、`core/vade/blob/store.py:76-82/471-502`、`core/vade/blob/settings.py:26/39`、`core/vade/session/rpc.py:52-55/68-101/126-155/518-522`、`core/vade/session/parts.py:459-525`、`core/vade/session/manager.py:882`、`core/vade/app.py:78/144-146/237-241`、`core/vade/identity/whitelist.py:111-119/200-209`、`core/vade/transport/server.py:191-243/311-320/344-378`、`core/vade/transport/connection.py:44-63/119/124/234-235`、`tools/check_contracts.py:792-810/1096-1097/1341-1346/1356-1357/1480-1481/1540`、`tools/check_ledger.py:158-230`、`docs/rfc/RFC-0004-attachments-multimodal.md:110-146/290/446`、`docs/rfc/RFC-0015-ui-modules.md:§6/§7.1/§9.4`、`docs/rfc/LEDGER.md` §1/§2/§3、`docs/status.md`（`rfc0004a` 行）、客户端 `BlobUploaderReal.kt:19-26`、`SourceConfig.kt:154-170`、`VadeTransport.kt:112-119`。
