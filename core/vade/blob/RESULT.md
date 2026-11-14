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

`core/tests/blob/`：**10 个文件、105 个用例**（不是 mock：每个用例直接驱动
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
| `test_review_fixes.py` | 9 | 审查返工回归：跨设备引用计数、GC 调度、配额顺序、image mime 不绕过、Windows 删除、有界解压（见 §6） |

## 4. 门禁原始输出（本机，2026-11-14）

```
$ .venv/Scripts/python.exe -m pytest core/tests/blob -q
105 passed in 1.79s

$ .venv/Scripts/python.exe -m pytest core/tests -q
489 passed, 5 warnings in 24.12s         # 384 基线 + 105 blob，0 failed/error

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

## 6. 审查返工（B1/B2/B3 + 应4/应5/应6）

回归用例全部在 `core/tests/blob/test_review_fixes.py`（9 个）。每条都先红后绿：
先对着旧实现写断言，确认它抓住的是真症状，再改实现。

| 项 | 症状 | 修法 | 用例 | 验证 |
|---|---|---|---|---|
| **B1** 跨设备引用计数 | 两台设备持同一摘要（去重只按设备，所以是两行），一行到期就 `unlink` 文件，另一台的 `GET` 变 410/404 —— 数据被别人的 TTL 删掉 | 新增 `blob_bytes(sha256,size,ref_count)` 表；`add_blob` 只在**新行**时 +1（去重分支不加，否则泄漏）；`release_blob` 删行、-1，返回「是否最后一个引用」；`sweep` 只有最后一个引用才 `_remove_blob_files`；`_rebuild_refcounts()` 做迁移与自愈；`unpin` 重起 TTL 而不是写 `NULL`（`NULL` = 永生，字节永远收不回） | `test_two_devices_sharing_a_digest_keep_the_bytes_until_the_last_row_goes`（pin dev2 → 推 1 h → sweep → 文件在、dev2 GET 200；都 unpin 再过期 → sweep → 文件消失、`ref_count==0`） | 手工把 `release_blob` 改回无条件 `unlink` → 该用例红（dev2 GET 变 404） |
| **B2** GC 没人调度 | `sweep()` 只有测试调用；生产里 TTL 永不触发，磁盘只增不减 | `ensure_sweeper/start_sweeper/stop_sweeper/aclose`：`ensure_sweeper` 同步建任务（`install()` 不能 await）；`_sweep_forever` 用 `asyncio.to_thread(self.sweep)`，周期 = `max(0.01, gc_interval_s)`；`install()` 启动时先 `reconcile()` + 一次 `sweep()`；`CoreApp._lifespan` 把 startup/shutdown 转给 blob；`stop_sweeper()` await 取消后的任务，不留悬挂 task；`usage()` 排除已过期行（否则「配额满」只能靠手动 sweep 解除） | `test_the_periodic_sweeper_collects_and_stop_leaves_no_task`（`gc_interval_s=0.05`，不调 `sweep()`，等 ~3 个周期后行与字节都消失；`stop_sweeper()` 后 `task.done()` 且 `_gc_task is None`） | 删掉 `ensure_sweeper` 里的 `create_task` → 红（行不消失） |
| **B3** 配额检查顺序 | `_check_quota` 在稀疏文件之后：拒绝路径留下一只没人认领的 `.part`（sweep 走行、orphan 走已提交路径，都不看它） | `_check_quota(device, size, now)` 移到 `truncate` **之前**；`sweep` 之外的 `_collect_stray_temp_files()` 按真实 mtime 兜底（见应5） | `test_a_refused_begin_never_leaves_a_part_file_behind`（`max_bytes` / `quota` / `tmp_quota` 三条拒绝路径，`tmp/` 目录必须为空） | **变异自证**：把 `_check_quota` 移到 `truncate` 之后 → `1 failed, 8 passed`，红在 `assert list((quota.service.root/"tmp").iterdir()) == []` → `Left contains one more item: ...upl_....part`。改回后 `9 passed`，`service.py` sha256 与变异前一致（`f789881d…`） |
| **应4** `kind:"file"` 绕过图片闸 | 图片只按 `kind` 判定，客户端声明 `kind:"file"` 就能跳过 `image_max_bytes`、边长/像素预算和 EXIF 剥离 | §5.1「kind 是提示、mime 是判据」：`image = kind == "image" or mime in IMAGE_MIMES`，begin 的字节/尺寸闸与 `complete` 的复校都按这个布尔走 | `test_a_six_mib_png_sent_as_kind_file_still_hits_the_image_ceiling`（6 MiB `image/png` + `kind:file` → 413，`limit == image_max_bytes`，无会话残留）；`test_an_exif_png_sent_as_kind_file_is_still_refused`（带 `eXIf` 的 PNG + `kind:file` → 501 `data.field=="exif"`） | 把 `image` 改回 `kind == "image"` → 两条都红（分别 201 / 501 变 201） |
| **应5** Windows 删除 | `unlink` 在 Windows 上遇到仍被打开的文件句柄抛 `OSError` → `abort()` 500、`sweep()` 整趟失败；并发写的分片会被 sweep 从写者脚下删掉 | `abort` / `sweep` / `_remove_blob_files` / `_expire_if_idle` 一律捕 `OSError` 并只删行；`sweep` 跳过 `_inflight` 中的上传；新增 `_collect_stray_temp_files()`（真实 mtime 年龄闸门，避免误删刚 `begin` 还没落行的文件） | `test_an_open_temp_handle_does_not_turn_sweep_or_abort_into_a_500`（持句柄时 abort 仍 204、sweep 不抛且行已清）；`test_an_upload_with_a_part_in_flight_is_not_swept_from_under_the_writer`（`_inflight` 里时行与临时文件都还在，移出后才可回收） | 去掉 `except OSError` → 首条红（`PermissionError` 冒到 ASGI）；去掉 `_inflight` 跳过 → 第二条红 |
| **应6** PNG 有界解压 | 只解容器头：IHDR 合法、IDAT 是垃圾/炸弹也 201（JPEG/WebP 仍是这样，见下） | PNG 走 `_bounded_decompress`（2 s 墙钟 + `pixels*4+64KiB` 输出上限 + `eof` 检查）；**返工中发现并修掉的真 bug**：`_bounded_decompress` 抛的 `_Malformed` 在 `validate_image` 里没人接，直接冲出 ASGI 栈（服务端 500，而不是 501）→ 在调用点捕成 `UNSUPPORTED / field=mime` | `test_a_png_with_a_good_header_and_garbage_idat_is_refused`（IHDR 合法 + IDAT 全 `0xff` → 501、`field=="mime"`、`store.all_blobs()==[]`） | 去掉 `except _Malformed` → 红（`vade.blob.validate._Malformed` 冒到测试，证明正是该 bug） |
| （附）`acomplete` | — | `complete` 的重算摘要/解头是这条通道上唯一的 CPU 活，`acomplete` 走 `asyncio.to_thread`，不堵 `/ws` | `test_acomplete_answers_exactly_like_complete`（同一批字段、同状态、同尺寸结论） | 无变异（等价性用例） |

仍未覆盖：JPEG/WebP 只有头部解析（RFC §5.3 的有界解码在 core 无 codec 的前提下只对 PNG 成立），
已在 §5 第 4 条记为已知缺口。

## 7. 复审 r2 返工（应修 1 + 建议 2/4/6）

复审报告 `Vade-REVIEW-blob-r2.md` 留了 1 条应修与 8 条建议；本轮处理 4 条（应修 + 建议 2/4/6）。每条都先写断言确认能抓住症状，再改实现，最后用变异自证「删掉修复必红」。

| 项 | 症状 | 修法 | 用例 | 变异自证（`cp` 备份还原，未用 `git checkout`） |
|---|---|---|---|---|
| **应修** 去重回落到 missing / infected 行 | 上传 → 字节丢失 → `reconcile()` 标 `missing` → 同内容重传：`begin` 因 `state != "ok"` 不复用，`complete` 走到 `add_blob` 的 `IntegrityError` 分支直接返回**旧行** → 201 带一个 `GET` 必 410 的 `file_id`，而字节其实已在磁盘上（「重传自救」失效）。`scan=="infected"` 同理：201 拿到扫描器已删掉的行 | `complete` 在 `add_blob` 之后比较 `stored.file_id != record.file_id`（= 去重命中）：<br>• `scan=="infected"` → 删掉刚放回的字节（按摘要全局清除，与扫描器路径同语义）、删会话，`PERMISSION_DENIED / data={field:sha256, reason:infected}`（403）；<br>• `state != "ok"` → `store.revive_blob()` 用本次复校过的 mime/name/kind/width/height/scan 重置该行、`state='ok'`、`expires_at` 按 `blob_ttl_seconds` 重算。引用计数不动（该行本就持有 1 个引用，再加会永久泄漏） | `test_a_missing_row_is_revived_when_the_same_content_is_uploaded_again`（→ 201、GET 200 且字节相等、`state=ok`、`scan=clean`、`name` 更新、`expires_at` 重算、`ref_count==1`）；`test_an_infected_row_is_not_revived_by_a_re_upload`（→ 403、字节不落盘、会话不残留、GET 404） | 把 `if stored.state != "ok":` 换成 `if False:` → 红在 `assert 410 == 200`（**正是复审描述的症状**）；把 `if stored.scan == "infected":` 换成 `if False:` → 红在 `assert 201 == 403` |
| **建议 4** lifespan 转发无回归 | 删 `server.py:111-112`（`elif kind == "lifespan"` 两行）后全量 489 仍绿；`start`/`stop` 的转发从没经过 ASGI 入口 | 新增 `core/tests/blob/test_lifespan.py`：假 blob 记录 `start`/`stop`，向 `CoreApp` 的真 ASGI 入口依次发 `lifespan.startup` / `lifespan.shutdown`，断言两条 `*.complete` 与调用顺序；另加 `blob=None` 仍 complete、`start` 抛 → `startup.failed`、真 `BlobApp` 走完 lifespan 后 `_gc_task is None` 且 `_closed` | 同文件 4 条（`test_lifespan_startup_and_shutdown_reach_the_blob_channel` 等） | 注释掉 `elif kind == "lifespan":` 两行 → **4 failed**：`assert [] == [{'type': 'lifespan.startup.complete'}...]`；`cp` 还原后 sha256 仍 `d846c69b…`、`grep MUTATION` 0 命中 |
| **建议 2** `App.shutdown()` 拿不到 `aclose` | `BlobApp` 只有 `close()`，`getattr(blob,"aclose",None)` 恒 `None` → 走同步 `close()`：`task.cancel()` 后不 await，注释「优先异步关闭」与实现不符 | `BlobApp` 新增 `async def aclose()`（= `await self.stop()` = `service.aclose()`）；`close()` 保留为无 loop 时的同步兜底；`app.py:104-116` 的注释改成实话（两条路都在，`aclose` 会 await） | `test_shutdown_prefers_the_async_closer`（两者都有 → 只调 `aclose`）、`test_shutdown_falls_back_to_a_sync_closer`（只有 `close` → 调它）、`test_shutdown_awaits_the_real_blob_app`（真 `BlobApp`：`aclose` 存在，shutdown 后任务 `cancelled()` 且 `_closed`） | 把 `async def aclose` 改名 → `test_shutdown_awaits_the_real_blob_app` 红（`callable(None)`）；改名前的同步兜底用例仍绿，说明这条红的正是「没等到 aclose」 |
| **建议 6** `_put_part` 只捕 `FileNotFoundError` | Windows 上 `open(target,"r+b")` 被外部句柄（AV/备份/索引器）占用抛 `PermissionError`（`OSError` 子类但不是 `FileNotFoundError`）→ 500 | `except OSError`；答案统一为 `NOT_FOUND / field=upload_id`（与「会话消失」同义），预检 `target.exists()` 保留 | `test_a_part_that_cannot_open_its_temp_file_is_not_a_500`（把 `.part` 换成目录：Windows 抛 `PermissionError`、POSIX 抛 `IsADirectoryError`，同一个 `OSError` 面） | 改回 `except FileNotFoundError` → 红，`PermissionError` 冒到 ASGI（500）。实测本机报 `PermissionError: [Errno 13] ... .part` |
| **建议 3**（未做，说明） | `stop()` 抛异常会从 `_lifespan` 冒出 | 本轮未改：`try/finally` 已保证 `shutdown.complete` 先发出（uvicorn 不卡），只影响日志噪音；留给下一批与审计/日志一起处理 | — | — |

未取用的复审建议：1（JPEG/WebP 头部-only，已在 §5 记为已知缺口）、5（`kind:"file"`+`image/*` 强制尺寸）、7（`mime=` 三元恒等式）、8（`__Host-ac_csrf` 的 C-05 登记）——均不在本批范围。

### 门禁（本机原始输出）

```
$ .venv/Scripts/python.exe -m pytest core/tests -q
499 passed, 5 warnings in 28.28s          # 基线 489 + 10 条新用例（7 lifespan + 3 review_fixes）

$ .venv/Scripts/python.exe tools/check_contracts.py
contracts: OK (C-03/C-04/C-05/C-06/C-18 frozen, 33 error codes, config metadata complete, event gate green)

$ .venv/Scripts/python.exe docs/check_docs.py
合计 12 项，失败 0 项。
```

### 改动文件

| 文件 | 内容 | sha256（本轮后） |
|---|---|---|
| `core/vade/blob/service.py` | dedupe 命中的 infected 拒收 / missing 复活；`_put_part_locked` 改捕 `OSError` | `2a8606c4586e…` |
| `core/vade/blob/store.py` | 新增 `revive_blob()`（只复活 `state='missing'`，不动 `ref_count`、不动 `created_at`） | `fb1be1501e90…` |
| `core/vade/blob/http.py` | `BlobApp.aclose()`；`close()` 补 docstring | `95dca9e40eb1…` |
| `core/vade/app.py` | `App.shutdown()` 注释与两条关闭路径对齐 | `cc1844c699ec…` |
| `core/tests/blob/test_lifespan.py` | 新增 7 条：lifespan 转发 4 + `App.shutdown` 3 | `eeccf4b76634…` |
| `core/tests/blob/test_review_fixes.py` | 新增 3 条（§8/§9），未删任何既有断言 | `add7a7b5a273…` |

结论：4 条全部真修并有变异佐证；全量 499 绿、契约与文档门禁绿；不 push。
