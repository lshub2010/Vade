---
rfc: RFC-0004
title: 附件与多模态：`POST /ui/blob` 上传协议、压缩图单 `file_id` 与 content parts 消息结构
status: approved
approved: 2026-11-14
date: 2026-11-14
proposer: 编排（依据 `research/ui-decisions.md` #9 / #10、`research/ui-02-messages-input-attachments.md` §3.6 与 §3.9 的 C1/C2/C3/C9/C13；AMEND-21 §8.21.2 R1、AMEND-22 §8.22.2 R1/R5/R8）
affects: C-05 `schema/rpc.json`（追加 `ui/blob.token` 与 `http_endpoints`，`ui/submit.content` 定型）、消息模型 `core/vade/session/models.py` + `sqlite_store.py`（`Message.content: str` → parts）、C-18 `schema/config.schema.json`（新增 `[attach]`）
related: RFC-0003（提示词注入分层，本文件格式与章节风格来源）；AMEND-22 §8.22.2 R1（blob 例外）/ R5（单 `file_id`、只存压缩图）/ R8（Web 可写 + CSRF，`READ_ONLY_SESSION` 收窄）；AMEND-21 §8.21.2 R1；RFC-0005（模型能力查询，编号预留，**未起草**）
type: 契约追加为主（C-05 / C-18）+ 消息模型破坏性（Python API）；线上格式对旧客户端与旧会话均为追加
stage: 草稿，未实现；不新增 C-04 错误码，不新增 `attachment.*` 事件
---

# RFC-0004 · 附件与多模态

## 1. 摘要

附件字节不进 JSON-RPC：上传走数据面同一 HTTPS listener 上的 `/ui/blob` 端点族（begin / part / complete / abort + 取回），控制面仍只有 ws。消息体从 `content: str` 变为 `parts[]`；core 只存**压缩图**（长边 ≤2048、≤5 MiB）与**一个** `file_id`，原图不保留。旧会话不改写，`parts` 缺失即视为单个 `text` part。

| # | 决定 | 理由 |
|---|---|---|
| 1 | 端点族挂**数据面** listener（与 `/ws`、`/ui/session` 同进程），不挂管理面 | 管理面 `/admin*` 在数据面刻意 404；数据面也不该借管理面鉴权 |
| 2 | 原生端用 ws 铸造的短期 Bearer；浏览器用 cookie + CSRF | 设备密钥今天只在 Noise 握手内证明，HTTPS 面没有凭据（§2.4、§4.5） |
| 3 | `file_id` 单值、opaque；缩略图是**同一 id 的变体**（`?v=thumb`） | D96 只允许一个 id，FR-8.40 要服务端缩略图；变体≠第二个 id |
| 4 | `content` 保留为**文本投影属性**，新增 `parts` | `user_text`、压缩、搜索、审计全读文本，改名会连带炸一片 |
| 5 | C-06 **不加** `attachment.*` | 提交即 pin，v1 没有“已提交附件被回收”这个需要通知的状态（§7.5） |

## 2. 动机与证据

### 2.1 65519 字节帧上限是硬约束

`schema/rpc.json:66`：`The transport frame cap is 65519 bytes of plaintext.` 一张 5 MiB JPEG base64 后约 6.99 MB，是上限的 **107 倍**；按每帧 48 KiB 净荷需 **140+ 次**往返，还要自造流控与重传。附件进 JSON-RPC 不是“慢”，是**不可行**。

### 2.2 纯图片消息当前 100% 被拒

`core/vade/session/manager.py:323-325` 先取 `_text_of(blocks)`，文本为空即 `raise VadeError("INVALID_ARGUMENT", "content has no text block")`；而 `_text_of()`（同文件 384-386）只挑 `type == "text"` 的块。于是“发一张图，不问问题”在今天必然失败——FR-8.42 已把这条写成“RFC 落地前不通过”。这不是 UI 能绕的。

### 2.3 多端历史只能存 `file_id`

`models.py:59` 是 `content: str`；`to_dict()`（77-88）只输出 `content`；snapshot 走 `session.message_tree()`（136-137），落库列 `messages.content TEXT NOT NULL`（`sqlite_store.py:53-63`）。本地 URI 写进消息，手机发的图在桌面必然 404——**跨端一致性的最低要求是消息里存服务端 id**；本地 URI 只能留在客户端的可丢弃派生状态里（AMEND-22 R2 已把“附件本地缩略图缓存”划入该类）。

### 2.4 上传通道不存在，连凭据面都没有

- `rpc.json` 的 `ui_to_core` 只有 6 个方法：`ui/attach`、`ui/detach`、`ui/submit`、`ui/interrupt`、`ui/queue.cancel`、`approval/decide`，无任何 upload/blob 方法。
- `transport/server.py` 的数据面只有 3 条路由：`/healthz`、`/ui/session`、`/ws`；`/admin*` 在此**故意 404**。
- 设备密钥的证明在 Noise 握手内（`_ws()` 的 `_make_crypto()`，仅非浏览器路径）；`/ui/session` 走配对码 → `__Host-ac_ui` cookie（`SameSite=Strict`、`HttpOnly`、`Secure`）。
- 结论：**HTTPS 面上今天没有可用于原生端的凭据**。这是本提案必须自己回答的第一个问题（§4.5），不能假装“复用设备密钥鉴权”就够。

### 2.5 三条结论

begin/part/complete 是**三个状态**，一个 `POST` 装不下断点续传；只存压缩图就必须把“原图未保留”做成**协议事实**（响应字段 + UI 文案）；改 parts 的同时必须拆掉“无文本即拒”，否则 parts 白设计。

## 3. 目标 / 非目标

| | 内容 |
|---|---|
| G1 | 8 MiB 上传全程 **0 个 ws 帧 >64 KiB**；控制面抓包仍 0 明文（AC-3） |
| G2 | 断点续传：断网 5s 后从最后确认片继续，**总进度单调不减**，重复分片幂等 |
| G3 | 同 `(device, sha256, size)` **零重传**（二次 begin 直接拿 `file_id`，0 次 PUT） |
| G4 | 只发 1 图不发文字 → turn 正常创建；三端 snapshot 的 parts 数量与顺序一致 |
| G5 | 旧会话零迁移可读；旧客户端收到多出的 `parts` 不崩 |
| G6 | 只在**显式只读会话**拒绝写；cookie 会话按 D100 可写，缺 CSRF 必拒 |
| G7 | 任何一层拒绝都给**字段级**错误（`error.data.field`），不静默丢图 |
| N1 | 不做原图保留（D96）：无“原图 + 派生图两 id”，无“发送原图”开关 |
| N2 | 不做语音上传：`audio` 只保留类型，v1 返回 `UNSUPPORTED`（D97 端上转写） |
| N3 | 不做跨端草稿（FR-8.44 v1 口径） |
| N4 | 不做服务端转码/降采样：服务端只**复校**，唯一例外是 `?v=thumb` |
| N5 | 不改 C-04 枚举；不新增 `attachment.*` 事件；不做对象存储/预签名 URL |
| N6 | 不做消息内附件删除（消息不可变，FR-8.41）；不做附件版本树 |

## 4. 上传协议

### 4.1 归属与端点族

挂**数据面** listener（`core.listen`，默认 `127.0.0.1:8443`，与 `/ws` 同进程）：浏览器要发就得吃 `[server].ui_origins` 的 CORS 与 `__Host-ac_ui` cookie；管理面（8444）是另一套凭据与只读矩阵，混进去会造出第二条权限路径。

| 方法 | 路径 | 语义 | 幂等键 |
|---|---|---|---|
| `POST` | `/ui/blob` | begin：声明 sha256/size/mime/尺寸 | `(device_id, sha256, size)` |
| `PUT` | `/ui/blob/upload/{upload_id}?offset=N` | 写一片（原始字节） | `(upload_id, offset)` |
| `HEAD` | `/ui/blob/upload/{upload_id}` | 查已收区间（丢状态后恢复） | 读 |
| `POST` | `/ui/blob/upload/{upload_id}/complete` | 复校总量 + sha256，出 `file_id` | `(upload_id)` |
| `DELETE` | `/ui/blob/upload/{upload_id}` | abort | `(upload_id)`，重复删 204 |
| `GET` / `HEAD` | `/ui/blob/{file_id}[?v=thumb]` | 取回（`Range`、`ETag`）/ 元数据 + `scan` | 读，`immutable` |

id 形状：`blb_` / `upl_` + 26 位 Crockford base32，服务端**只按正则接受**（`^bl[ub]_[0-9a-z]{26}$`）；落盘路径**只由 sha256 十六进制推导**，不用任何客户端字符串拼路径（§8.4）。`POST /ui/blob` 体上限 64 KiB，片体上限 `part_size + 1 KiB`；JSON 端点只收 `application/json`，片端点只收 `application/octet-stream`——**不收 `multipart/form-data` / `text/plain`**，那是 CORS 简单请求，能绕过预检（§8.2）。

### 4.2 begin

```json
POST /ui/blob
{"kind":"image","sha256":"<64hex>","size":1834211,"mime":"image/jpeg",
 "name":"IMG_0042.jpg","width":2048,"height":1536,"part_size":10485760,"client_msg_id":"c-7f3a"}
→ 201 {"file_id":"blb_...","upload_id":null,"dedupe":true,
       "size":1834211,"mime":"image/jpeg","pinned":false,"expires_at":1763200000000}
```

| 服务端状态 | `dedupe` | `upload_id` | 客户端动作 |
|---|---|---|---|
| 该设备已有同 sha256 的**已提交** blob | `true` | `null` | 直接用 `file_id`，0 次 PUT（G3） |
| 该设备有同 sha256 的**未完成**上传 | `false` | 原 id + `received` | 从 `offset_next` 续传 |
| 全新 | `false` | 新 id | 从 0 开始 |

- **去重按设备隔离**，不全局共享：全局去重省盘，但会把“这台 core 上有人已有此文件”变成**存在性预言机**（发一个已知 sha 就能问出答案）。
- `sha256` 必填——没有它就没有幂等键，也没有 complete 的复校锚点；`part_size` 可提，服务端夹到 `[5 MiB, attach.part_size]`；`name` 只作展示（剥控制字符、截 255），**绝不参与路径**。

### 4.3 part：偏移、区间、并行、进度

- 形状：`PUT /ui/blob/upload/{upload_id}?offset=N`，体是原始字节（≤`part_size`），头 `Content-Type: application/octet-stream`、可选 `x-vade-chunk-sha256`；返回 `{received:[[s,e],…], offset_next, expires_at}`；**偏移必须落在 `part_size` 边界**，否则 400，`data.field="offset"`、`data.expected=[...]`。允许乱序（`received` 是区间表），所以“3 片并行”（FR-8.39）不需要合并写。
- **幂等**：同 `(upload_id, offset)` 重复 PUT 且字节摘要相同 → 返回同样 `received`，不重复占盘。移动网络下“超时但服务端其实收下了”是常态，不幂等会把续传做成死循环。
- **并发上限** `attach.part_max_concurrent`（默认 3），超出 429 `OVERLOADED` + `Retry-After`——可以等，不是状态冲突。
- **进度只由 `received` 算**：`confirmed_bytes = Σ(e-s)`，UI 除以 `size`；分片重传不回退（G2）。
- 超过 `upload_idle_ttl_min`（15 min）无新片 → 404，客户端重新 begin（旧临时文件已 GC）。

### 4.4 complete / HEAD / abort / 取回

- 形状：`POST /ui/blob/upload/{upload_id}/complete`，体 `{sha256, size}`；成功返回 `{file_id, sha256, size, mime, width, height, pinned:false, expires_at, thumb}`；complete **重算整文件 sha256**（不信客户端声明）并与 begin 声明比对；不一致 → 400 `INVALID_ARGUMENT`，`data.field="sha256"`、`data.expected/actual`。**上传会话保留**（区间仍满），可重挑片，不必从头。
- 通过后：临时文件 `fsync` → 原子 `rename` 到内容寻址路径 → 写 `blobs` 行 → 删上传会话。顺序**文件先、行后**：崩在中间只留无主文件（sweeper 清），不会留指向空气的行。
- `HEAD /ui/blob/upload/{upload_id}` → `{size,part_size,received,offset_next,expires_at,state}`，是“杀进程重开后恢复”的唯一依据（FR-8.40 的“不显示假进度”）。
- `DELETE` 幂等返回 204，且**不删已提交 blob**：abort 只作用于上传会话，已 pin 的东西归消息生命周期管。这条必须写进实现注记，避免“删附件把历史消息里的图删了”。
- `GET /ui/blob/{file_id}` 支持单段 `Range`（206 + `Content-Range`）；`ETag: "sha256:<hex>"`、`Cache-Control: private, max-age=31536000, immutable`——内容寻址天然不可变。
- `?v=thumb` 返回长边 `attach.thumb_px`（320）的 WebP 派生图，**同一 `file_id`**，首次请求生成并落 `blobs/<sha>.thumb`。这是 D96“单 `file_id`”与 FR-8.40“服务端缩略图”的交点：**变体是地址的一部分，不是第二个 id**。派生失败返回 415 `UNSUPPORTED`，客户端回落类型图标；派生图不参与去重、不进消息 parts。

### 4.5 鉴权

| 客户端 | 凭据 | 来源 | CSRF |
|---|---|---|---|
| 原生 | `Authorization: Bearer <upload_token>` | ws 新 RPC `ui/blob.token`（设备密钥已在 Noise 握手中证明），TTL 600s，绑 `device_id` | 不需要 |
| Web | `__Host-ac_ui` cookie | `/ui/session` 配对码换取 | **必需**：`x-vade-csrf` + `Origin` + `Sec-Fetch-Site`（§8.2） |

`ui/blob.token` 的 `params` 是 `{ttl_s: int(60..900), default 600}`，`result` 是 `{token, expires_at, max_bytes, endpoints{begin,part,complete}}`，`errors` 只有 `READ_ONLY_SESSION` / `UNAVAILABLE`。token 是 32 字节随机串，只存 `sha256(token)`；重启后仍有效（`blob_tokens` 行），吊销设备时整批删。

- **`READ_ONLY_SESSION` 只对显式只读会话**：AMEND-22 R8 已废止“cookie 即只读”（D100）。`/ui/blob` 不得因“看到 cookie”就拒；只有会话/设备被显式标 `read_only`（只读分享、管理员锁定）才 403，且**在 begin 就拒**，不等传完 8 MiB。未认证一律 401 `AUTH_FAILED`，不区分“token 过期 / 设备吊销”（C-04 的 no-oracle 规则）。

### 4.6 配额

| 维度 | 键 | 默认 | 超限 |
|---|---|---|---|
| 单文件硬上限 | `attach.max_bytes` | 100 MiB | 413，`data.field="size"` |
| 图片上限 | `attach.image.max_bytes` | 5 MiB | 413，`data.kind="image"` |
| 单 turn 附件数 | `attach.max_per_turn` | 10 | 400，`data.field="content"` |
| 设备累计 / 临时区 | `attach.quota_bytes` / 同键 1/4 | 2 GiB / — | 413，`data.reason="quota"` / `"tmp_quota"`、`limit/used` |

按 `device_id` 记（与去重口径一致），`pinned=false` 也算占用——否则“传完不提交”就是免费扩容。C-04 无 `QUOTA_EXCEEDED`，本提案**不新增**：用 `PAYLOAD_TOO_LARGE + data.reason`，UI 文案不同即可（§11-D）。

### 4.7 TTL / GC / 孤儿回收

上传会话 `open → complete | aborted | expired`；blob `unpinned → pinned → (无引用) → collected`。

| 对象 | 过期条件 | 谁清 |
|---|---|---|
| 上传会话 `open` | `upload_idle_ttl_min`（15）无新片 | sweeper（`gc_interval_s`=60） |
| 临时文件 | 其会话被删/过期 | 同上 + 启动对账 |
| `unpinned` blob | `attach.ttl_hours`（1h）内未被消息引用 | sweeper |
| `pinned` blob | 引用它的消息全消失（会话删除） | 会话删除减引用计数，归零转 `unpinned` |
| 缩略图 | 随主 blob | 同上 |

- **pin 时机**：`ui/submit` 落库消息的同一事务里把 parts 的 `file_id` 置 `pinned=true`、`expires_at=null`。这就是“提交即固化”，也是 §7.5 不需要 `attachment.expired` 的原因。
- **孤儿回收**：启动扫 `${data_dir}/blobs/**` 与 `blobs` 表对账。有文件无行 → 移 `blobs/orphan/` + 审计，24h 后删；有行无文件 → `file_id` 标 `missing`，`GET` 返回 410（不复用 404，让 UI 能区分“没权限”与“真没了”）。 sweeper 单实例语义：多进程时用 SQLite `BEGIN IMMEDIATE` 抢 `blob_gc` 锁行，不做分布式协调；审计 `blob.gc` 每次只记一行汇总（`scanned/deleted/bytes`），不逐文件写。

### 4.8 错误码映射（全部取自 C-04）

| 场景 | HTTP | `code` | `data` |
|---|---|---|---|
| 无凭据/过期 | 401 | `AUTH_FAILED` | `{}` |
| 显式只读会话 | 403 | `READ_ONLY_SESSION` | `{"field":"session"}` |
| 跨设备读（`read_scope=device`） | 403 | `PERMISSION_DENIED` | `{"field":"file_id"}` |
| id 不存在/已过期 | 404 | `NOT_FOUND` | `{"field":"upload_id"}` |
| 偏移越界、sha 不匹配 | 400 | `INVALID_ARGUMENT` | `{"field":"offset"\|"sha256"}` |
| 大小/配额超限 | 413 | `PAYLOAD_TOO_LARGE` | `{"field":"size","limit":N,"reason":"quota"}` |
| 类型/EXIF/像素不合规 | 501 | `UNSUPPORTED` | `{"field":"mime"\|"exif"\|"pixels"}` |
| 并发片过多/限流 | 429 | `OVERLOADED` | `{"retry_after":N}` |
| 写盘失败/内部异常 | 500 | `INTERNAL` | `{}`（不回显路径与堆栈） |

## 5. 压缩规则

### 5.1 客户端硬指标（唯一允许上传的形态）

| 项 | 值 | 说明 |
|---|---|---|
| 长边 | ≤ **2048 px** | 短边等比；旋转先烘进像素（EXIF Orientation 不再需要） |
| 字节 | ≤ **5 MiB**（5 242 880 B） | D96 写“≤5MB”，实现取**更严**的 5 MiB，发布说明写明口径 |
| 类型 | `image/jpeg`、`image/png`、`image/webp` | 同 FR-8.38 白名单；HEIC/BMP/TIFF 客户端先解码再编码 |
| 元数据 | EXIF/XMP/IPTC **全剔**（含 GPS）；ICC 转 sRGB 后剔除 | 隐私是协议不变量，不是“尽量”（§5.3 复校） |
| 尾部 | 不得有 EOI/IEND 之后字节 | 防 polyglot（JPEG+ZIP、PNG+HTML） |

### 5.2 格式、质量、动画

| 源 | 输出 | 策略 |
|---|---|---|
| JPEG | JPEG | q82、4:2:0、渐进；仍超限则 82→70→60，再缩长边 2048→1600→1280（1024 仍超则客户端报错，不上传） |
| PNG（有 alpha） | PNG 8-bit 或 WebP | PNG 超 5 MiB 且有 WebP 编码器 → WebP q90；否则量化到 256 色 |
| PNG（无 alpha） | JPEG | 白底合成，避免透明变黑 |
| WebP | WebP | q80；不二次编码 |
| GIF（静态） | PNG | 单帧直转 |
| GIF（动画） | **首帧**静态图 | 同 ui-02 §3.6.3“gif 只取首帧”；动画本身见 Q3 |

- **确定性**：同输入同平台应稳定（同 libjpeg 版本 + 同参数），**跨平台不保证字节一致**。所以 sha256 去重是“同端重复选同一张图”的优化，不承诺“手机选过的图在 Web 零上传”（Q5）。 客户端必须在附件条标注“已压缩 · 原图未保留”（D96 的 UI 义务），并在选择时就提示压缩目标，而不是传完才说。

### 5.3 服务端复校（不信任任何客户端声明）

复校发生在 **complete**（字节已齐、未发 `file_id`）；任一项失败都不产生 `file_id`。

| 检查 | 方法 | 失败码 |
|---|---|---|
| 大小 | `Content-Length` 累计 + `stat` | 413 `data.field="size"` |
| 类型 | **magic bytes**：`FFD8FF` / `89504E47` / `RIFF....WEBP` | 501 `data.field="mime"` |
| 声明一致 | `begin.mime` == 嗅探结果 | 400 `data.field="mime"`、`data.sniffed` |
| 尺寸 | 解析头部，须等于 begin 声明且长边 ≤2048 | 400 `data.field="width"` / 413 `data.field="pixels"` |
| 像素与解码 | 像素 ≤ `attach.image.max_pixels`（4 194 304）；受限解码一遍（2s 超时、像素/内存上限） | 413 `data.field="pixels"` / 501 `data.field="decode"` |
| EXIF 残留 | 扫 APP1 `Exif\0\0`，出现即拒 | 501 `data.field="exif"` |
| 结构与帧数 | JPEG 止于 `FFD9`、PNG 止于 `IEND`、WebP RIFF size == 文件长；APNG/GIF 帧数 >1 一律拒（v1 只收单帧） | 501 `data.field="trailer"` / `"frames"` |

### 5.4 服务端为什么不转码，以及三种“太大”

转码让 `sha256` 与客户端声明脱钩（去重失效、复校失去锚点），还要在 core 维护编码器矩阵与 CPU 预算。**服务端只“验”，把“改”锁在客户端**；唯一例外是只读、可丢、按需生成的 `?v=thumb`。

| 触发 | 阶段 | `code` | `data` |
|---|---|---|---|
| 客户端压完仍 >5 MiB | 上传前（本地） | 不上行 | 文案：“压缩后仍超过 5 MB，请裁剪或换一张” |
| 服务端实测 >5 MiB | complete | 413 | `{"field":"size","limit":5242880,"actual":N}` |
| 模型不支持该尺寸/字节 | `ui/submit` | 501 | `{"field":"content[1]","capability":"image_size","limit":N}` |

## 6. 消息结构

### 6.1 `ui/submit` 的 content parts

```json
{"session_id":"s-1","client_msg_id":"c-7f3a","content":[
  {"type":"text","text":"这张报错是什么意思？"},
  {"type":"image","file_id":"blb_01H...","mime":"image/jpeg","name":"IMG_0042.jpg",
   "size":1834211,"width":2048,"height":1536,"sha256":"<64hex>"},
  {"type":"file","file_id":"blb_01H...","mime":"application/pdf","name":"spec.pdf","size":902144}]}
```

| 规则 | 值 |
|---|---|
| part 数 | 1..`attach.max_per_turn`（10），硬上限 20 |
| `text` | 单 part ≤32 000 字符；多 text part 按序以 `\n` 拼接（FR-8.45） |
| `image` | 必填 `file_id/mime/name/size/width/height/sha256`；`mime` 三值 |
| `file` | 必填 `file_id/mime/name/size`；`mime` 走 FR-8.38 文件白名单 |
| `audio` | **类型保留、v1 禁用**：501 `data.reason="audio_disabled"`（D97；FR-8.42 四类型不破） |
| 未知 `type` | 400 `data.field="content[i].type"` |
| 顺序 / 纯附件 | parts 顺序 = 消息内展示顺序（FR-8.41），服务端不得重排；无 text part 但 ≥1 image/file 合法 |

### 6.2 `Message` 模型与存储迁移

`Message` 新增 `parts: tuple[dict[str, Any], ...] = ()`（空 = 旧行），原来的 `content` 降为**只读属性**（不是字段），返回文本投影；`queue.py`、`llm`、`compaction` 里所有 `m.content` 读点一行不改。`Turn.user_text` 取同一投影：纯图片 turn 得到 `"[image: IMG_0042.jpg]"`（非空）——否则“user_text 空”会在别处冒出第二个拦截点。

- 投影规则（写死，供压缩与测试断言）：text 原样；image → `[image: {name}]`；file → `[file: {name}]`；`\n` 连接。
- 存储：`ALTER TABLE messages ADD COLUMN parts TEXT`（NULL = 旧行），启动用 `PRAGMA table_info(messages)` 判断，**幂等**；`content` 列**继续存文本投影**（NOT NULL 不动），搜索、压缩、既有 SQL 全照旧。
- 写：新消息同时写 `content`（投影）与 `parts`（JSON）；两者不一致视为缺陷，用 A6 钉住。回滚：旧版本读新库不认 `parts`，但 `content` 仍是合法文本 → 降级为“只有文字”的会话，不崩不丢主体。这是选“双写”而非“只写 parts”的唯一理由。

### 6.3 snapshot、`to_dict()`、兼容与模型侧渲染

`to_dict()` **只加键** `parts`（text 投影仍在 `content` 里），不动既有键；`snapshot.messages[]` 因此自动带上（`snapshot()` 直接 `session.message_tree()`）。C-05 的 `ui/attach.snapshot` 类型是 `object|null`，加键不改类型；但 `parts` 形状必须登记进 C-05 注记，否则三端各写一份必漂移。`to_dict()` 仍**不**输出 `tool_calls`（属 R8 工具卡族），本提案不夹带。

| 场景 | 行为 |
|---|---|
| 旧会话（`parts` 为 NULL） | snapshot 返回 `parts:[{"type":"text","text":<content>}]`（读时合成，**不落库**） |
| 旧客户端收到 `parts` | 未知键，忽略；`content` 仍在，渲染不变 |
| 旧客户端发 `[{"type":"text",...}]` | 仍合法，照收（parts 的最小子集） |
| 旧客户端发 `[{"foo":"bar"}]` | 以前也拒（无 text 块），现在同样拒，但 `data.field` 指到 `content[0].type` |

**模型侧渲染（`to_llm()`）**：
- **只有 text part** → 产出与今天**逐字节一致**的 `{"role","content":"<str>"}`（golden 断言 A6）。
- 含 image/file → `content` 变 block 数组：`{"type":"text"}` / `{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,...","detail":"auto"}}`。base64 膨胀只发生在**最后一次**序列化，不出 core 进程、不过 ws。
- `file` part 不内联：v1 只把文件名与 MIME 作文本提示附在末尾（`[file: spec.pdf · application/pdf]`）；正文抽取属工具族，不在本提案。
- 5 MiB 上界 ⇒ 单图块约 6.7 MB JSON，落在主流 provider 图片上限内（Claude API 10MB / Bedrock 5MB 的下界由 5 MiB 原图守住）；`detail` 等 provider 差异由 llm 层适配，不进契约。

### 6.4 `ui/submit` 校验顺序（实现必须按此序）

1. `content` 是数组、长度在界内、`client_msg_id` 非空。
2. **幂等查询先行**：同 `client_msg_id` 已存在 → 返回原 turn（今天先校验后查重，重试可能因“模型能力变了”返回不同错误，必须换序）。
3. part 类型与必填字段（§6.1）；text 总长 ≤32 000。
4. 每个 `file_id` 存在、可见、未过期 → 否则 404 `data.field="content[i].file_id"`。
5. image 的 `mime/size/pixels` 对**当前生效模型**能力（§9）。
6. 至少一个可渲染 part，否则 `INVALID_ARGUMENT` + `data.field="content"`；消息文案从 `content has no text block` 改为 `content has no renderable part`（code 不变）。
7. 落库消息（含 parts）+ pin blobs + `turn.queued`，同一事务/同一 `emit`（沿用现状 `messages=[message]`）。

## 7. 契约变更

### 7.1 汇总（8 项变更、7 行；追加 6，破坏性 2）

| # | 契约 | 改动 | 判定 | 兼容策略 |
|---|---|---|---|---|
| 1 | C-05 `ui/submit.content` | `["object"]` → `oneOf[text\|image\|file\|audio]` | **收紧**（破坏性，仅对畸形客户端） | 合法旧 `[{"type":"text"}]` 仍通过；发布说明列“以前静默忽略的块现在报字段错” |
| 2 | C-05 新方法 `ui/blob.token` | 见 §4.5 | **追加** | 旧客户端不调用；未实现方不受影响 |
| 3 | C-05 新键 `http_endpoints` | 6 条路由、两式鉴权、body 上限、`x-vade.appends` | **追加** | 校验面；`check_contracts.py` 加断言 |
| 4 | C-05 `ui/attach` 注记 | snapshot.messages[].parts 形状 | **追加**（`snapshot` 仍 `object`） | 旧客户端忽略新键 |
| 5 | 消息模型 `models.py` + `sqlite_store.py` | `content: str` → `parts` + `content` 属性；`messages.parts` 列 | **破坏性（Python API）**，线上追加 | 属性名保留，调用方零改动；库降级可读（§6.2） |
| 6 | C-06 / C-04 | **不改**：C-06 不加 `attachment.*`（§7.5 给未来候选定义）；C-04 复用 9 个已有码（§4.8） | 无变更 | 不新增事件、不新增 `QUOTA_EXCEEDED` |
| 7 | C-18 `config.schema.json` | 新增 `[attach]`（17 键） | **追加**，但 root `additionalProperties:false` 需随批发版 | 默认开启；灰度见 §7.4 |

### 7.2 C-05：`ui/submit.content` 定型

`content` 从 `{"type":"array","items":["object"]}` 改为 `{"type":"array","minItems":1,"maxItems":20,"items":{"oneOf":[…]}}`，四支分别是：`text`（`text: string(1..32000)`）、`image`（`file_id`、`mime: jpeg|png|webp`、`name(1..255)`、`size`、`width/height: int(1..2048)`、`sha256(64)`）、`file`（`file_id`、`mime`、`name`、`size`）、`audio`（同 `file` 但不含 `width/height`）。

`audio` 留在 `oneOf` 里是**刻意的**：类型被承认（FR-8.42 四类不缩水）、行为被禁用（D97）；加开关时只改 core 判据，不改 schema。

### 7.3 C-05：`http_endpoints` 新键

C-05 目前只描述 JSON-RPC（`transport` 一句 + `directions`）；`/ui/blob` 是同一 listener 上的 HTTP 入口，必须落成机器可读条目，否则下一批实现者会以为它是野路子。追加：

- `listener`：`data plane (core.listen) — same process as /ws and /ui/session`；
- `auth`：`device → Authorization: Bearer <ui/blob.token>`、`browser → __Host-ac_ui cookie + x-vade-csrf`；
- `endpoints`：§4.1 的 6 条（`POST /ui/blob`、`PUT|HEAD|POST|DELETE /ui/blob/upload/{id}[…]`、`GET|HEAD /ui/blob/{file_id}`）；
- `body_max`：`begin=65536`、`part="attach.part_size + 1024"`；
- `x-vade`：`{"appends":"RFC-0004","fr_exception":"FR-8.2（AMEND-22 R1）唯一例外"}`。

### 7.4 C-18：新增 `[attach]`

| 键 | 默认 | 生效 | 说明 |
|---|---|---|---|
| `enabled` | `true` | restart | 关掉后 `/ui/blob*` 全 404，`ui/submit` 拒 image/file part |
| `max_bytes` | 104857600 | restart | 单文件硬上限（100 MiB） |
| `image.max_bytes` / `image.max_edge` / `image.max_pixels` | 5242880 / 2048 / 4194304 | restart | D96 的 5 MiB、长边与解码炸弹闸 |
| `part_size` | 10485760 | restart | 片大小上限（Q1） |
| `part_max_concurrent` | 3 | hot | FR-8.39 的“3 片并行” |
| `max_per_turn` / `text_max_chars` | 10 / 32000 | hot | 与 FR-8.41 的 10/20 双层夹紧；FR-8.45 的 32k 上限 |
| `ttl_hours` / `upload_idle_ttl_min` | 1 / 15 | restart / hot | 未 pin 的 blob / 上传会话 |
| `quota_bytes` | 2147483648 | hot | 每设备 |
| `gc_interval_s` / `thumb_px` | 60 / 320 | hot | sweeper / 派生图长边 |
| `read_scope` | `"core"` | hot | `core`=任一已认证设备可读；`device`=只读自己（Q8） |
| `scan.enabled` / `scan.command` | `false` / `""` | restart | 可选病毒扫描（§8.5） |

灰度：`enabled=true` 是默认，但 `/ui/blob` 需要 UI 端特性开关配合（不发就不会用到）。真正的风险是“5 MiB 上限把老用户的 20MB 图片拦掉”——发布说明必须写清“客户端会先压缩，用户无感”，服务端 5 MiB 是**压缩后**口径，不是原始选择上限。

### 7.5 C-06：为什么这次不加事件

候选 `attachment.expired {file_id, reason}` 只在“**已提交**消息里的附件被服务端回收”时才有意义。本提案用两件事消灭该状态：①提交即 pin（§4.7），已提交 blob 不参与 TTL 回收；②过期信息在 begin/complete 响应的 `expires_at` 里，客户端自己算得出，不需要推送。所以 v1 **不动 C-06**（冻结文件零改动，`check_contracts.py` 的 emitted-event 扫描不受影响）。

若将来要支持“配额回收已提交附件”，按追加权补：`payload {file_id, reason: ttl|quota|deleted, affected_messages[]}`，`required [file_id, reason]`，注记写“只在已 pin 的 blob 被回收时发送；未提交的 upload 过期不发事件”。

**契约归属澄清**：`Message.content: str` **不是**任何冻结契约的字段：C-02 是 `protocol/v1/kat.json`（已知答案向量），C-05 只描述 RPC 与 `snapshot: object`，C-06 只描述事件。这次改动的契约性只来自两处：**C-05 的 `ui/submit.content` 形状**与**C-06 snapshot 的实际内容**。§8.21.2 R1 把 `Message.content: str` 列进契约缺口是**事实描述，不是契约归属**——修它不需要新契约，需要的是在 C-05 注记里写明 `parts` 是“三端必须一致渲染的最小字段集”（§6.3）。

## 8. 安全

### 8.1 凭据矩阵

| 请求 | 设备 Bearer | cookie + CSRF | 显式只读 | 结果 |
|---|---|---|---|---|
| `POST /ui/blob` / `PUT` 片 | ✅ | ✅ | — | 201/200 |
| `GET` / `HEAD /ui/blob/{id}` | ✅ | ✅ | ✅（读允许） | 200/206 |
| 任何写 | ✅ | ✅ | 是 | 403 `READ_ONLY_SESSION` |
| 任何写 | ❌ | ❌ | — | 401 `AUTH_FAILED` |

读**不**受只读限制：只读会话仍要能看历史里的图。

### 8.2 CSRF（浏览器路径）

1. `Origin` 精确等于 `[server].ui_origins` 中某项（沿用 `_cors_headers` 的精确匹配，**不是**前缀/通配）。
2. `Sec-Fetch-Site: same-origin`；缺失按“非浏览器”处理，但此时也不接受 cookie 凭据。
3. `x-vade-csrf` 头必须等于 `__Host-ac_csrf` cookie（double-submit；该 cookie `HttpOnly=false`、`SameSite=Lax`），并与 `__Host-ac_ui` 会话绑定（服务端存 `csrf_hash`，登录轮换即轮换）。
4. 只收 `application/json`（begin/complete）与 `application/octet-stream`（part）——都不是 CORS 简单请求，跨站写必须先过预检，而预检已被第 1 条挡死；**拒收** `multipart/form-data`、`text/plain`、`application/x-www-form-urlencoded`。

任一条不过 → 403 `ORIGIN_MISMATCH`（与 AMEND-22 R8 的判据“缺 CSRF → `ORIGIN_MISMATCH` / `PERMISSION_DENIED`”一致）。注意 C-04 注记写“`ORIGIN_MISMATCH` 只在管理面用”，但数据面 `/ui/session` 的既有实现已经返回它——**这是既存漂移，本提案沿用数据面事实并建议在 C-02 批次补注记**，不改 C-04。Cookie 属性同理：现状 `__Host-ac_ui` 是 `SameSite=Strict`，R8 要求“`Lax` 起步”，两者不冲突（Strict 更严），本提案**不裁定**该属性（属 R8 的 CSRF 契约），`/ui/blob` 的校验不依赖 SameSite，可各自演进。

### 8.3 MIME 嗅探

- 只信 magic bytes + 受限解码，**扩展名与 `Content-Type` 只作提示**。`.exe` 改名 `.jpg`（PE 头 `MZ`）→ 501 `data.field="mime"`（FR-8.38 验收条件）。
- `file` 白名单同样按内容判定：PDF `%PDF-`、文本走 UTF-8 严格解码、Office 认 ZIP 容器里的 `[Content_Types].xml`；声明 jpeg 实际 png → 400，否则压缩策略与模型侧 MIME 全乱。

### 8.4 路径、文件名与解码炸弹

- `file_id`/`upload_id` 先正则再查表，任何不匹配都 404（不给“格式错 / 不存在”的差异）；落盘路径 = `${data_dir}/blobs/<sha[0:2]>/<sha[2:4]>/<sha>`，只由**已校验的 64 位十六进制**拼成，`..`、`/`、NUL 在 sha 与 id 上都不可达。
- `name` 只进数据库与 `Content-Disposition`：剥 `\r\n`、剥控制字符、截 255；下载头用 RFC 5987 `filename*=UTF-8''...` + ASCII 兜底名——防头注入与分号截断。
- 临时区与最终区**同一文件系统**（都在 `data_dir` 下），保证 `rename` 原子。
- 先解析头部拿尺寸，**像素超限直接拒，不进解码**；解码在受限上下文（2s 超时、像素/内存上限，`RLIMIT_AS` 或子进程），失败一律 501/413，不重试不落盘。拒多帧、拒尾部垃圾、拒 RIFF size 与文件长不符——三条把“图片里塞 zip”挡在存储外；`?v=thumb` 走同一套限制（它是二次解码入口，最容易被漏）。

### 8.5 授权、可见性与扫描

- `read_scope="core"`（默认）：本 core 任一已认证 principal 可读任意 `file_id`。理由是多端历史硬需求（手机发的图桌面要能看），而 core 是单用户自托管边界。`file_id` 不可猜（26 位 base32 ≈ 130 bit），但它是“不可枚举”而非“秘密”。**`read_scope="device"`**：只允许上传者设备读，跨端 403——给“共享 core / 多人”场景留出口，默认不开（Q8）。
- v1 **没有**消息级 ACL：能读会话就能读其附件。这条要写进安全评审的范围说明，别让评审以为有。浏览器会话是独立 principal（`ui-xxxx`），读权限同设备；吊销设备时其 token 与 blob 读权一起失效。
- `scan.enabled=true` 时 blob 进 `scanning`：`complete` 返回 `status:"scanning"`（仍给 `file_id`），`HEAD` 带 `scan:"clean|pending|infected"`，`ui/submit` 遇非 `clean` 返回 501 `data.reason="scan_pending"`，感染文件删除并记审计。**默认关**：v1 单机自托管、附件只来自本人设备，扫描器运维成本大于收益；用 `HEAD` 而非让 `GET` 返 425，因为 C-04 没有“稍后再试”的读语义，硬造会多一个码。

**审计与日志**：每次 begin/complete/abort 记一条审计：`{actor_kind, device_id, file_id?, upload_id, bytes, sha256, mime, outcome}`；`ui/submit` 的审计带 `file_ids[]`，AMEND-22 R1 的判据“`ui/blob` 与 `ui/submit` 属同一条可回放链”可据此对账。**永不**记录字节、base64、原始文件名全文（截 64 字符）、路径；`file_id` 在日志里按需脱敏（`blb_01H…`），避免日志被当成取回凭据。

## 9. 模型能力门控

与 **RFC-0005**（模型能力查询，编号预留）的接口只有四个字段，缺一个就做不到“选中即提示、不白传”：

| 字段 | 用途 | 缺省行为 |
|---|---|---|
| `input_modalities[]` | 是否含 `"image"`；选中图片即提示 | 未知 → 允许上传、UI 标“未知”，提交时 core 判 |
| `max_image_bytes` | 与 `attach.image.max_bytes` 取 **min** | 未知 → 只用 attach 上限 |
| `max_image_px` | 与 `attach.image.max_edge` 取 min | 同上 |
| `max_images_per_turn` | 与 `attach.max_per_turn` 取 min | 同上 |

1. **UI 在“选中”就判定**：能力已知且不含 `image` → 立刻提示“当前模型不支持读图，图片会作为文件保存”+ [切换模型] [仍然发送]（FR-8.50 ①）。**此时不得先上传**（“不白传”）；用户点“仍然发送”后，客户端把 `type:"image"` 降级成 `type:"file"` 再传——**这一步是协议要求**，否则 core 会给文本模型塞一个它读不了的 image block。
2. **core 在提交时二次判定**：以**当前生效模型**（会话级选择 > 全局默认 > `manifest.priority`，AMEND-22 R5）的能力为准；不符返回 501 `UNSUPPORTED` + `data.capability="vision"` + `data.field="content[i]"`。不得用“配置里的默认模型”顶替。

3. **能力未知不得乐观假设**（FR-8.50 ②，与 FR-8.47 一致）：`input_modalities` 缺失时不展示“视觉 ✓”、不预先压到模型专属尺寸、不在提交前拦截，只在提交后用 `UNSUPPORTED` 错误卡给 [切换模型] [移除图片重发]。RFC-0005 未落地时的降级：`ui/submit` 只能用 `attach.*` 静态上限（无 per-model 上限），第 2 条退化为“不校验视觉能力”，UI 徽标显示“未知”。**这条降级必须写进 U1 任务书**，否则 UI 会等一个不存在的 RPC。

## 10. 验收

A1–A14 全部要求真实执行；截图与手写记录不算证据。

| # | 断言 | 承载 |
|---|---|---|
| A1 | 只发 1 图（无 text part）→ turn 创建成功；`parts[0].type=="image"`；`turn.user_text` 非空（占位符） | `core/tests/session/test_submit_parts.py` |
| A2 / A3 | 8 MiB 上传到第 2 片后断网 5s → 重新 begin 返回同一 `upload_id` 且 `received` 覆盖前两片，**PUT 请求数不增加**、`confirmed_bytes` 单调不减；同 `(device,sha256,size)` 二次 begin → `dedupe=true`、`upload_id=null`、0 次 PUT，不同设备同字节 → `dedupe=false` | `core/tests/blob/test_resume.py` |
| A4 | 显式只读会话 begin → 403 `READ_ONLY_SESSION`；cookie+CSRF 的正常会话 begin → 201（D100：cookie 不再隐式只读）；缺 `x-vade-csrf` → 403 `ORIGIN_MISMATCH` | `core/tests/blob/test_auth.py` |
| A5 | 5.5 MiB 图 complete → 413 `data.field="size"`；6000px 图 → 413 `data.field="pixels"`；`.exe` 改名 `.jpg` → 501 `data.field="mime"`；三者都不产生 `file_id` | `core/tests/blob/test_validate.py` |
| A6 | 造旧库行（`parts` NULL）→ snapshot `parts == [{"type":"text","text":<content>}]`，`content` 逐字节等于原值；`to_llm()` 与旧版 golden 一致 | `core/tests/session/test_message_compat.py` |
| A7 | 带 GPS 的 JPEG 上传 → 存储字节里搜不到 `Exif\0\0` 与 GPS IFD 标签 `0x8825`；解码后方向与拍摄方向一致 | `core/tests/blob/test_exif.py` |
| A8 | complete 传错 sha256 → 400 `data.field="sha256"` + `expected/actual`；随后 `HEAD` 仍显示完整区间，重传正确摘要可成功 | `core/tests/blob/test_complete.py` |
| A9 | abort 两次都 204；之后 `PUT`/`GET` 404；临时文件在一轮 sweeper 内消失；启动对账把“有文件无行”移入 `orphan/` 并记审计 | `core/tests/blob/test_gc.py` |
| A10 | 设备配额降到 1 MiB → 第二个 2 MiB 图 begin 即 413 `data.reason="quota"`；已 pin 的旧 blob 不被回收 | `core/tests/blob/test_quota.py` |
| A11 | 提交 2 图 + 1 文件 → snapshot parts 顺序与提交顺序一致；三端用同一 fixture 渲染顺序一致 | `core/tests/session/test_parts_order.py` |
| A12 | 文本模型 + image part → 501 `data.capability="vision"`、`data.field="content[0]"`；同内容以 `type:"file"` 提交 → 通过，且 provider 请求体里**没有** image block | `core/tests/session/test_vision_gate.py` |
| A13 | 8 MiB 上传全程所有 ws 帧 <64 KiB（AC-3 抓包脚本扩展）；审计里 `ui/blob` 的 `file_id` 与 `ui/submit` 的 `file_ids[]` 能对上 | `core/tests/blob/test_frame_cap.py` |
| A14 | `GET /ui/blob/../../core.toml`、`GET /ui/blob/blb_%2e%2e%2f` → 404；文件名含 `\r\n` 时 `Content-Disposition` 无换行且 RFC 5987 编码 | `core/tests/blob/test_paths.py` |

文档级：本 RFC 落地后 `python docs/check_docs.py` 仍 12 项全绿（写本文件时已实测，新文件不参与 C1–C10 计数）。

## 11. 备选方案（未采用）

| 方案 | 为什么不采用 |
|---|---|
| A. base64 分片走 JSON-RPC | 65519 字节帧 + 33% 膨胀 ⇒ 5 MiB 图 140+ 次往返，还要在 RPC 上自造流控与重传（§2.1） |
| B. 原生端逐请求设备密钥签名 | 可行（`admin/auth.py` 有 p256/ed25519 解析），但要自造 nonce 表与时钟偏移窗 + 逐片签名开销；ws 铸 token 只多一次 RPC 且复用既有身份（§4.5） |
| C. 单次 multipart `POST /ui/blob` | 无断点续传（FR-8.39 硬要求）、8 MiB 一次成功率低、服务端要整体缓冲；且 multipart 是 CORS 简单请求，CSRF 面更大 |
| D. 新增 C-04 `QUOTA_EXCEEDED` | 为一个可由 `PAYLOAD_TOO_LARGE + data.reason` 表达的场景扩枚举，要同步 Rust 枚举与 33 条码表，收益只是少一个 `if` |
| E. 原图 + 派生图两 id（ui-02 D3 推荐项） | 被 D96 否决：存储翻倍、要维护两 id 关系、客户端还要选“发哪张”；代价是“原图不可下载”，UI 必须明示 |
| F. 服务端转码到 2048/5MiB | 破坏 sha256 去重与 complete 复校锚点，另加编码器矩阵与 CPU 预算（§5.4） |

## 12. 未决问题

| # | 问题 | 选项 / 倾向 |
|---|---|---|
| Q1 | **分片 5 还是 10 MiB？客户端能否自选？** | FR-8.39 给 5–20 MiB、推荐 10。倾向服务端默认 10 MiB、允许客户端在 `[5 MiB, 上限]` 内自选（弱网 5、Wi-Fi 20），服务端只夹紧不强制——否则“推荐值”会被实现成硬编码 |
| Q2 | **Web 端 canvas 压缩质量与格式？** | `toBlob('image/jpeg',0.82)` 在 Chrome/Firefox/Safari 上体积可差 2×；WebP 编码在 Safari 16 之前不可用。倾向优先 WebP 0.8、回落 JPEG 0.82，并把“仍超 5 MiB 则缩长边”写成统一循环。代价：跨端 sha256 更难一致（Q5） |
| Q3 | **动画 GIF 保不保留？** | 倾向 v1 **只取首帧**（同 ui-02 §3.6.3）：5 MiB 上限下动画几乎必超限，多帧又是解码炸弹主要载体。若要保留，需 `image.max_frames` 与单帧像素双上限，并确认模型侧只读首帧（否则“用户看到动图、模型看到静帧”是新的静默降级） |
| Q4 | **配额默认值？** | 参考 10 MiB × 200 张 ≈ 2 GiB。倾向 2 GiB/设备 + 8 GiB/core 总量，两者都进 `[attach]`；需产品确认单机自托管用户会不会被这个限制打到 |
| Q5 | **去重按设备还是全局？** | 倾向按设备（§4.2）：全局省盘但造存在性预言机，且跨端压缩字节不一致时命中率本就低。若磁盘硬约束，可加 `attach.dedupe="device\|global"`，global 时**只对同设备返回** `dedupe`（内部共享存储但不暴露命中） |
| Q6 | **原生端凭据：ws 铸 token 还是逐请求签名？** | 倾向铸 token（§4.5）。若 C2 不愿为上传新增 RPC 方法，备选 B 的签名方案要自带 nonce 表与 ±300s 时钟窗；**不能两者都不做**（那就只剩 cookie，原生端等于没有上传） |
| Q7 | **缩略图变体 vs 第二个 id？** | 倾向 `?v=thumb` 同 id（§4.4）。需 C-09 `AttachmentChip` 确认它只吃一个 `file_id` 且能自己拼变体 URL；若组件要求“缩略图也是 id”，则必须回到 D96 重开“单 id”裁定 |
| Q8 | **`read_scope` 默认 `core` 还是 `device`？** | 倾向 `core`（多端历史硬需求）。若未来支持多用户共享 core，必须改默认并补消息级 ACL——v1 明确不做 ACL（§8.5），评审时不要假设有 |
| Q9 | **`audio` part：保留禁用还是从 `oneOf` 删掉？** | 倾向保留（§7.2）：FR-8.42 写的是四类 part，删掉会造成“契约比需求少一类”。代价是 schema 里有一个永远 501 的分支，需在注记写清，否则下一批实现者以为它没做完 |

## 13. 影响面与后续

实现阶段要动的文件：`core/vade/blob/*`（新增：存储、索引、sweeper、校验、thumb 派生）、`core/vade/transport/server.py`（6 条路由 + body 上限 + CORS 复用 + CSRF 中间件）、`core/vade/session/manager.py`（`_text_of` → `parts_text()`、校验顺序 §6.4、pin blobs）、`models.py`（`parts` + `content` 属性 + `to_dict()` 加键 + `to_llm()` 分支）、`sqlite_store.py`（`ALTER TABLE` + 读写映射）、`schema/rpc.json` 与 `schema/config.schema.json`（§7.2 / §7.3 / §7.4 三处追加）、`tools/check_contracts.py`（断言：6 条路由齐全、`ui/blob.token` 存在、`attach` 键与默认值、`ui/submit.content` 的四型 oneOf）。 **不动**：`schema/events.json`（§7.5）、`schema/errors.json`（§7.1 第 6 行）、`core/vade/policy/*`、`core/vade/router/*`、`docs/design.md` §9（Skin 接口，只引用 C-09 的 `AttachmentChip`）。

**方案层待同步**（AMEND-22 §8.22.6 已列，本文件不代改）：`design.md` §7.1.3“按凭据判定”与 §7.10 只读矩阵必须改成“显式只读会话才 403”，否则文档仍写“cookie 只读”，与 §4.5 直接矛盾。

**后续（不在本 RFC）**：RFC-0005 模型能力查询（§9 四个字段是硬依赖）；R2 工具卡字段（`tool_calls` 与 parts 同在 `to_dict()` 上，互不阻塞）；消息内附件删除/替换（属消息不可变原则的重开，需另案）；文件内容抽取（`file` part 只给模型文件名与 MIME，正文抽取属工具族）。

**参考**：`research/ui-02-messages-input-attachments.md` §3.6（流水线、校验矩阵、C1/C2/C3/C9/C13）、§3.9（core 缺口清单）；`research/ui-decisions.md` #9（独立 `POST /ui/blob`）、#10（只存压缩图、单 `file_id`）；`docs/requirements.md` §8.21.2 R1、§8.22.2 R1/R5/R8；RFC-0003（章节与元信息风格来源）。
