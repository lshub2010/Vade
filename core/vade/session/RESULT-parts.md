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
4. **ws 连接目前不带 `device_id`**：跨设备归属只在调用方传入时生效（`SessionRPC` 读 `connection.device_id`）。真实连接要把设备身份接进握手。
5. `ui/blob.token`（§7.1 #2）在 schema 里**没有声明**，`issue_token()` 也没有 RPC 面；本批**没有**声明一个未实现的方法（宁缺不假）。
