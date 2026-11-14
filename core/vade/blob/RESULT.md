# RFC-0004 附件上传通道（前端半：`/ui/blob`）

状态：**已实现并通过门禁，未合并**。分支 `tmp/rfc0004a`。
范围：RFC-0004 §4（上传协议）+ §5.3（服务端复校）+ §8（凭据/路径/配额/GC）。
**不含** §6 消息结构、§7.2 C-05 `ui/submit.content` 定型、客户端与 UI 改动（见「未做」）。

## 1. 路由表（数据面同一 listener，`/admin*` 仍 404）

| 方法 | 路径 | 作用 | 成功 | 鉴权 |
|---|---|---|---|---|
| POST | `/ui/blob` | begin：声明 sha256/size/mime/尺寸，返回 `upload_id` + `part_size` | 201（去重 200） | 写 |
| PUT | `/ui/blob/upload/{upload_id}?offset=N` | 写一片原始字节；偏移须落在 `part_size` 边界，允许乱序 | 200 | 写 |
| HEAD | `/ui/blob/upload/{upload_id}` | 已收区间 / `offset_next` / `expires_at`（断点恢复的唯一依据） | 200 | 读 |
| POST | `/ui/blob/upload/{upload_id}/complete` | 重新测量 + 重算 sha256 + 容器复校，产出 `file_id` | 201 | 写 |
| DELETE | `/ui/blob/upload/{upload_id}` | abort；未知/畸形/已删一律 204（幂等，不区分） | 204 | 写 |
| GET/HEAD | `/ui/blob/{file_id}[?v=thumb]` | 取回（单段 `Range`、`ETag`、immutable）/ 元数据 + `scan` | 200/206 | 读 |

硬规则：JSON 端点只收 `application/json`，片端点只收 `application/octet-stream`；
`multipart/form-data`、`application/x-www-form-urlencoded`、`text/plain` 一律 **415**
（它们是 CORS 简单请求，会绕过保护 cookie 凭据的预检，§8.2）。体上限：JSON 64 KiB，
片 `part_size + 1 KiB`。

## 2. 本批修掉的 6 个真 bug

| # | 位置 | 症状 | 修法 |
|---|---|---|---|
| 1 | `ids.py` `ID_RE` | RFC 印的是 `^bl[ub]_…`，只认 `blb_`/`blu_`，`upl_` 全 404，整条协议不可用 | 按 §4.1 正文改为 `^(?:blb|upl)_[0-9a-z]{26}\Z` |
| 2 | `ids.py` | `$` 可匹配尾随换行 → 带 `\n` 的 id 能进临时文件名 | 改 `\Z` |
| 3 | `ids.py` `normalize_sha256` | `.strip()` 在「归一化」里悄悄修理空白，两种拼写被当成同一摘要 | 只归一大小写，不 strip |
| 4 | `ids.py` `blob_relpath(None)` | 抛 `TypeError` 而非 `VadeError`（500 且绕过错误码契约） | 加 `isinstance` 守卫 |
| 5 | `service.py` `_fsync()` | 以 `"rb"` 打开后 `fsync`，Windows 每次 complete 必崩 `OSError [Errno 9]` | 改 `"rb+"` |
| 6 | `service.py` `expires_at` | 输出秒，而 RFC / `events.json` / `X-Vade-Expires-At` 都是 unix **ms** | 新增 `_ms()`，7 个出口统一 |

## 3. 测试

`core/tests/blob/`：**9 个文件、96 个用例**（不是 mock：每个用例直接驱动
`BlobApp.__call__` / `CoreApp.__call__` 的 ASGI scope，时钟由 `Clock` 注入，
所以 TTL/GC 是确定性的而不是 `sleep(3600)`）。

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `test_paths.py` | 28 | id 形状、两处遍历、文件名不进路径、`\r\n` 注入 |
| `test_ranges.py` | 10 | 半开区间的 normalize/add/offset_next/gaps/covers |
| `test_ids.py` | 28 | 生成器形状与唯一性；正则拒绝（含尾随 `\n`）；前缀不匹配仍 404；摘要只归一大小写、不修空白；显示名清洗；路径只由摘要推导 |
| `test_upload.py` | 4 | 乱序片 → 201；缺片 complete 拒且会话保留；HEAD 报已收字节（非高水位）；abort 幂等且片失效 |
| `test_auth.py` | 10 | 六路由无凭据 401；错 token/吊销同答案；cookie 四道闸各自 403 + 全通过正例；三种简单请求 415（先于鉴权） |
| `test_validate.py` | 7 | .png 实为 zip 拒；超边长/超像素（独立预算）拒；PE/ELF 501；PNG/JPEG/WebP 全绿 |
| `test_quota.py` | 3 | 单文件超 `max_bytes` 413；tmp 区（1/4）先命中 `tmp_quota`；已提交字节计入 `quota` |
| `test_gc.py` | 3 | TTL 到期 sweep 清文件+行；孤儿文件 reconcile 移入 `orphan/`；空闲上传过期清临时文件（推进时钟后重发 token） |
| `test_mount.py` | 3 | `install()` 后六路由经 `CoreApp` 全可达；`attach.enabled=false` → 六路由 404；cookie 闸读的是 live session 表 |

## 4. 门禁原始输出（本机，2026-11-14）

```
$ .venv/Scripts/python.exe -m pytest core/tests/blob -q
96 passed in 1.45s

$ .venv/Scripts/python.exe -m pytest core/tests -q
480 passed, 5 warnings in 24.72s          # 384 基线 + 96 新增，0 failed/error

$ .venv/Scripts/python.exe tools/check_contracts.py
contracts: OK (C-03/C-04/C-05/C-06/C-18 frozen, 33 error codes, config metadata complete, event gate green)

$ .venv/Scripts/python.exe docs/check_docs.py
合计 12 项，失败 0 项。
```

`tools/check_contracts.py` 新增 `check_blob_routes()`：把 `vade.blob.http.ROUTES`
与 RFC-0004 §4.1 的六条路由逐条比对，防止实现私增/漏路由。`schema/config.schema.json`
新增 `[attach]`（C-18 追加，root 仍 `additionalProperties:false`）。

## 5. 未做（按 RFC 先后顺序）

1. **RFC-0004 §6 消息结构**：`content` 的 `parts[]`（text/image/file）、`Message`
   模型与 SQLite 存储迁移、旧会话「`parts` 缺失 = 单个 text part」兼容、模型侧渲染。
   目前 `ui/submit` 仍只接受 `content: ["object"]`（未定型）。
2. **RFC-0004 §7.2 C-05 定型**：`ui/submit.content` 的机器可读 schema 未落；
   `schema/rpc.json` 仍是 `["object"]`。本批只落了 `[attach]` 配置与路由表校验。
3. **`ui/blob.token`**：RFC §4.5 的「从已认证 ws 会话换 600 s 设备 token」RPC 未实现；
   `BlobService.issue_token()` 是预留 seam，测试直接调用它。
4. **缩略图**：`?v=thumb` 的派生依赖 Pillow（core 不带 codec）；未安装时按设计返回
   415 `data.reason=no_codec`。本批未测该分支，也未测 Range/ETag 之外的取回细节。
5. **扫描管线**：`attach.scan.*` 默认关闭；`run_pending_scans()` 有实现但无测试
   （单用户自托管默认不开，留到启用扫描的批次）。
6. **客户端 / UI**：Compose、Web 壳、上传 UI 全部不在本批。
