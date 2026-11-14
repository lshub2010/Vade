# RFC-0004 后半 · content parts + `ui/submit` 定型 · RESULT

分支 `tmp/rfc0004b`，基于 `3ca2a96`（前半上传通道已合入）。

## 实现点

| 文件 | 内容 |
|---|---|
| `core/vade/session/parts.py`（新） | `Part` 模型；**§6.4 校验阶梯**：`require_shape`（结构）→ `normalize`（类型/数量）→ `resolve_attachments`（`file_id` 格式 + 归属 + 状态，经 `BlobService.stat`）→ `check_limits`（总字节/数量）；`parts_text` / `has_renderable`；`PartLimits.from_config` |
| `session/models.py` | `Message.parts` 字段 + `__post_init__` 投影；`wire_parts()`；`to_dict()` 增 `parts`；`to_llm(images=...)`（纯文本路径字节不变；image → `image_url`，file → 文本提示） |
| `session/sqlite_store.py` | `parts TEXT` 列；`_migrate()` 用 `PRAGMA table_info` 探测（**可重入**）+ 并发重复列守卫；`_parts_json` / `_message_from_row` |
| `session/manager.py` | `submit(..., device_id=)` 按 §6.4 重排（幂等检查移到校验之前）；写入时 pin、事件发送失败时 unpin；`_blob_service` / `_pin` / `_unpin` |
| `session/rpc.py` | `ui_submit` 传 `device_id`；`dispatch` 现在**转发 `VadeError.data`**（原先丢掉 `data.field`，客户端拿不到字段名） |
| `session/__init__.py` | `_LazyBlobs` + `PartLimits.from_config(cfg)` 接入 `SessionLog` |
| `schema/rpc.json` | 1.0.0 → **1.1.0**；`ui/submit.content` → `oneOf[text\|image\|file\|audio]`、`minItems:1`、`maxItems:20`；`ui/attach` 注明 `parts`；新增顶层 `http_endpoints`（六条 blob 路由） |
| `tools/check_contracts.py` | `check_submit_parts()` + `check_http_endpoints()` |
| `core/tests/session/test_parts.py`（新） | **42 用例** |

## 校验顺序（实现位置）

`manager.submit()` 内依次：`require_shape`（`manager.py:354`）→ `normalize`（`:366`）→ `resolve_attachments`（`:369`，含 `file_id` 归属与状态）→ `check_limits`（`:373`）→ 落库。**顺序错即实现错**：先落库后查归属会留下越权消息。

## 迁移

- 旧库无 `parts` 列：`PRAGMA table_info(messages)` 探测后 `ALTER TABLE ADD COLUMN`，可重入；并发下重复加列被捕获并忽略。
- 旧消息 `parts` 为空：读取时用 `content` 回退（`parts_text`），snapshot 与 `to_dict()` 对旧客户端保持"有 `content` 字符串"的形状。

## 门禁（真实输出）

```
pytest core/tests -q      → 531 passed, 5 warnings（489 基线 + 42 新用例）
tools/check_contracts.py  → contracts: OK (… 33 error codes …)
docs/check_docs.py        → 合计 12 项，失败 0 项
```

## 已知缺口（诚实记录）

1. **`content` 仍是 dataclass 字段**（在 `__post_init__` 里派生），不是 property —— 否则冻结的 `core/vade/loop/` 里 `Message(content=...)` 与 `dataclasses.replace()` 的调用点会炸。loop 不在本批范围。
2. **按模型能力做图片门控（§6.4 第 5 步）未实现**：RFC-0005 未落地，当前只有 `attach.*` 静态上限。
3. `Message.to_llm()` 的图片块需要调用方传 `images` map；loop 目前不传，图片降级为 `[image: name]` 文本提示（loop 属后续批次）。
4. **ws 连接目前不带 `device_id`**：附件因此 **fail-closed** —— 带 `file_id` 的 turn 在真实连接上得到 `PERMISSION_DENIED data.reason="device_unknown"`（纯文本 turn 不受影响）。真实连接要把设备身份接进握手，附件面才算开通。
5. `ui/blob.token`（§7.1 #2）在 schema 里**没有声明**，`issue_token()` 也没有 RPC 面；本批**没有**声明一个未实现的方法（宁缺不假）。

## 复审应修（R1–R5）

审查报告 `D:/HermesWorkspace/Vade/Vade-REVIEW-parts.md` 的 5 条应修，逐条落地。**不改已通过的架构**：校验阶梯仍是 §6.4 的七步，只把第 4 步的后半段（元数据对账）从"缺失"补成一步。

### R1 · part 元数据以 `BlobRecord` 为准；`kind` 与 mime 对账

- 新增 `apply_attachment_metadata(parts, records)`（`parts.py`），在 `manager.submit` 的**第 4 步之后、第 5 步之前**执行（`manager.py` 注释标为 4b）。`mime/name/size/width/height/sha256` 全部用 `BlobRecord` 覆盖，客户端声明值只作为形状提示，不参与存储、不参与限额。
- `type=="image"` 只能指向**权威 mime ∈ `IMAGE_MIMES`** 的 blob，否则 `INVALID_ARGUMENT` + `data.field="content[i].kind"` + `reason="kind_mismatch"` + `actual=<记录 mime>`。判据用 mime 而非 `record.kind`：`blob/service.py` 明确把 `kind` 当客户端提示、按 mime 重新按图片解码，用 `kind` 会误伤"以 `kind:"file"` 上传但被服务端按图片验收"的合法记录。`file` part 接受任意记录。
- `check_limits` 因此变成真检查，并补上 `width/height > image_max_edge` 的兜底（`PAYLOAD_TOO_LARGE`，`data.field=content[i].width|height`）：C-05 冻结了 `maximum: 2048`，声明 1×1 不能给 3000 px 洗白。
- 图片记录缺解码尺寸时不落库：`UNSUPPORTED reason="image_dimensions_unknown"`（C-05 要求整数宽高）。

用例（`core/tests/session/test_parts.py`，全部通过）：

| 用例 | 断言 |
|---|---|
| `test_declared_metadata_is_replaced_by_the_blob_record` | 3 MiB / 1600×1200 / webp / 真名 的 blob 声明成 1 B / 1×1 / png / `sha256="0"*64` → 落库 part 逐键等于记录值 |
| `test_a_declared_small_size_cannot_hide_a_real_oversize_image` | 6 MiB 声明成 1024 B → `PAYLOAD_TOO_LARGE @ content[0].size`，`actual=6291456`，turns 为空 |
| `test_a_pdf_cannot_be_declared_as_an_image_part` | PDF（`application/pdf`）声明 `type:"image"` → `INVALID_ARGUMENT @ content[0].kind`，`reason=kind_mismatch`，turns 为空 |
| `test_a_real_image_over_the_edge_cap_cannot_hide_behind_1x1` | 3000 px 声明 1×1 → `PAYLOAD_TOO_LARGE @ content[0].width`，`limit=2048`，`actual=3000` |

### R2 · 无 `device_id` 时对 `file_id` fail-closed

- `resolve_attachments` 在 `service is None` 之后、逐个 `file_id` 之前：`device_id is None` 且 parts 里出现 image/file → `PERMISSION_DENIED`、`data.field="content[i].file_id"`、`data.reason="device_unknown"`、不落库。
- 有 `device_id` 时维持原判据（必须是本设备），并把 `if device_id and ...` 的短路去掉——走到那里 `device_id` 必非空，短路只会再造一个"测试过、生产不跑"的分支。
- 纯文本 turn 不受影响（`wanted == []` 直接返回）。**这是行为收窄，已写进缺口 4**：ws 握手接入 `device_id` 之前，附件面等同未开通。

用例：

| 用例 | 断言 |
|---|---|
| `test_a_file_id_without_a_device_identity_is_denied` | 无 `device_id` + image part → `PERMISSION_DENIED`，`reason=device_unknown`，`field=content[0].file_id`，turns 为空 |
| `test_a_text_only_turn_needs_no_device_identity` | 无 `device_id` + text → `queue_pos=1` |
| `test_rpc_submit_without_a_device_identity_denies_attachments` | 生产路径：`SessionRPC` 读 `connection.device_id`，匿名连接 → `RpcError(PERMISSION_DENIED, data={field,reason})` |

### R3 · 配置界被冻结界夹紧（选**夹紧 + 警告**）

- `PartLimits.__post_init__` 统一夹紧，不再只夹 `max_per_turn`：`text_max_chars ≤ 32000`、`image_max_bytes ≤ 5242880`、`image_max_edge ≤ 2048`、`image_max_pixels ≤ 4194304`、`max_per_turn ≤ 20`，下限统一 1；越界值用 `vade.session.parts` logger 打一条 warning 后夹紧。
- 选夹紧而非启动报错：C-18 的 `attach.image.max_edge` 合法上限是 8192（schema 冻结，改它要升版本），启动报错会让一份 C-18 合法的配置直接把 core 打死；夹紧保持"配置能启动、契约不被放宽"。**代价**：`attach.image.max_bytes > 5 MiB` 时上传通道允许的图会在 submit 被拒——这正是 D96 的 5 MiB 契约，已在上传侧文档口径内。
- `max_bytes`（每 turn 总量 / 单文件）不夹：C-05 没有它的冻结值。

用例：

| 用例 | 断言 |
|---|---|
| `test_configured_limits_are_clamped_to_the_frozen_c05_ceilings` | `PartLimits(max_per_turn=50, text_max_chars=64000, image_max_bytes=10 MiB, image_max_edge=8192, image_max_pixels=64 MiP)` → 生效值 20 / 32000 / 5242880 / 2048 / 4194304；且 `PartLimits(image_max_edge=8192)` 下 3000 px 仍被拒（`limit=2048`）、40000 字仍被拒（`limit=32000`） |

### R4 · 变更记录兼容方向改正

`docs/agents/01-contracts.md` 的 C-05 行：删掉"**破坏性**：`content` 不再是单个对象；旧客户端读新消息需回退到 `parts_text` 拼接"，改为"**收紧（仅畸形块）**：`content` 本来就是数组，合法旧 `[{"type":"text"}]` 仍通过；`content` 仍是文本投影，旧客户端读新消息无需改动；**新客户端读旧消息**才需回退到 `content` 字符串（旧行 `parts` 为 NULL，读时合成、不回写）"。

### R5 · RFC 注记：`content` 是字段派生，不是 property

`docs/rfc/RFC-0004-attachments-multimodal.md` §6.2 正文后加一行实现注记：`content` 保留为 dataclass 字段、在 `__post_init__` 由 `parts` 派生（只读语义）；改 property 会让冻结的 `loop/` 里 `Message(content=...)` / `dataclasses.replace(..., content=...)` 全部失效。RFC 语义未变。

### 复审后门禁（真实输出）

```
pytest core/tests -q      → 539 passed, 5 warnings（531 基线 + 8 新用例）
tools/check_contracts.py  → contracts: OK (C-03/C-04/C-05/C-06/C-18 frozen, 33 error codes, …)
docs/check_docs.py        → 合计 12 项，失败 0 项
```

同时修正 3 个原有用例的调用（`test_file_part_reaches_the_model_as_a_text_hint`、`test_malformed_file_id_is_not_found_with_the_part_pointer`、`test_stored_content_equals_the_projection`）：它们原来不带 `device_id` 提交附件，正是 R2 指出的"生产不会这么走"的路径；现在显式传 `device_id="dev-1"`。没有删除任何断言。
