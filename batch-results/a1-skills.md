# a1-skills 批次结果（admin 技能管理 API 落地）

## 改了什么

| 文件 | 内容 |
|---|---|
| `core/vade/admin/skills.py`（新） | 7 条路径 / 9 个操作的实现。所有读写都调 `SkillLoader`：`list_all` / `preview` / `install` / `approve` / `reject` / `rollback` / `uninstall` / `set_status`。本文件不打开 `SKILL.md`、不解析 frontmatter、不自己拼 `pending/`、`.history/`、`.trash/`、`.rejected/` 路径。 |
| `core/vade/admin/app.py` | 装配 `skill_routes(ctx, guard, _json)`；`AdminContext` 新增 `skills` 端口，`from_app` 与 `bind_ports` 都从 `app.skill_loader` 解析（admin 先于 skill 安装，只有 `from_app` 会拿到 `None`）。501 兜底列表只剩 memory 段。 |
| `core/vade/skill/loader.py` | 新增只读 `pending(name)`：`get(include_pending=True)` 在 active 与 pending 共存时只返回 active，详情页的 `pending_diff` 需要提案正文。没有第二份解析逻辑，就是同一个 `_pending` 映射的只读入口。 |
| `schema/admin-api.yaml` | skills 段 7 条路径、9 个操作的 `x-vade-implemented` 全部翻 `true`。结构、字段、错误响应一律未动。 |
| `core/tests/admin/test_skills.py`（新） | 10 个测试，覆盖列表来源、两步安装、无 token、approve 的 rev+1 与 `.history`、pending diff、reject 进 `.rejected`、rollback 生成新 rev、启停/卸载、browser cookie 403、无 loader 时 501。 |
| `core/tests/admin/test_admin_api.py` | `test_unimplemented_feature_endpoints_answer_501_not_404` 只保留 memory 的 6 条形状。 |

## 路由与语义

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/admin/api/skills` | `loader.list_all(include_pending=True)`，`status` 原样取自 loader（`invalid` 类状态归到 `unavailable`；`rejected` 是目录，不是状态） |
| POST | `/admin/api/skills/install/preview` | `source=upload` 取 `content`，`source=url` 取 `url` → `loader.preview`；返回 `preview_token`、frontmatter、摘要、缺工具、重名冲突、`valid`、`missing_sections`、`files` |
| POST | `/admin/api/skills/install` | 只收 `preview_token`（schema 如此）。缺失/未知/过期/已用 → `400 INVALID_ARGUMENT`；成功只落 `pending/`，`status=pending` |
| GET | `/admin/api/skills/{name}` | 详情：`body`、`revision`、`status`、后端算的 `pending_diff`、`.history` 摘要 |
| PATCH | `/admin/api/skills/{name}` | `{status: active\|disabled}` → `loader.set_status`；其它值 400（与 approve 是两件事） |
| DELETE | `/admin/api/skills/{name}` | `loader.uninstall` → `.trash/`，返回 `{ok:true}` |
| POST | `/admin/api/skills/{name}/approve` | `{revision}` 必须等于 pending 的 revision，否则 `409 TOOL_CONFLICT`；成功后由 loader 做 rev+1、写 `.history/`、热加载 |
| POST | `/admin/api/skills/{name}/reject` | `{revision, reason}`，reason 必填非空；`loader.reject` 移到 `.rejected/` |
| POST | `/admin/api/skills/{name}/rollback` | `{to_revision}` → `loader.rollback`，复制旧版为**新** revision，不删历史 |

## 关键行为

1. **列表就是 loader 的视图**：不做状态推断，pending 也在列表里（UI 要从这里点通过）。验收口径「install 后 `GET /skills` 是 pending、而 `SkillLoader.list_all()` 不含它」由测试同时断言两侧。
2. **两步安装是服务端约束**：`install` 无 `preview_token` 直接 400，不依赖前端禁用按钮。token 在 loader 侧绑 `(name, sha256(内容))`；admin 侧另存 60s 的源内容（与 loader 的 token TTL 同长、上限 16 条），因为 schema 的 install body 只带 token。token 两侧都是一次性，复用即 400。
3. **approve 的 revision 闸门**：拿 pending 的 revision 与请求比对（proposal 的 `base_revision == active revision`，install 也沿用 active revision），不一致 409，避免"审的是 A、升的是 B"。字节是否被改仍由 loader 的 `_verify_pending_digest` 判，admin 不重复。
4. **diff 在后端**：`pending_diff` 用 `difflib.unified_diff` 拼 active 与 pending 的（loader 已截断的）正文；只有 pending 时旧侧为空文件。响应上限 20k 字符。前端不需要解析 YAML。
5. **浏览器只读**：没有新增豁免路径，`guard()` 对 browser cookie 的 POST/PATCH/DELETE 一律 403 `READ_ONLY_SESSION`；GET 列表仍可读。`test_browser_readonly.py` 自动枚举写路由，已覆盖新路由。
6. **没有 loader 的构建**：`AdminContext.skills is None` 时仍回 501 `UNSUPPORTED`，与 schema 声明一致，UI 可以照旧标"本构建不含此功能"。

## 验证

- `python -m pytest core/tests/admin/test_skills.py -q` → **10 passed**
- `python -m pytest core/tests/admin -q` → **42 passed**
- `python -m pytest core/tests/test_skill -q` → **162 passed**
- `python tools/check_contracts.py` → **contracts: OK**（C-03/C-04/C-05/C-06/C-18、event gate 全绿；未新增事件/错误码）
- 路由表核对：`/admin/api/skills` 9 个操作全部注册，`/install*` 在 `/{name}` 之前（Starlette 顺序匹配）。
- 未跑全量 pytest（按要求）。

## 注意 / 后续

- **zip 上传没有出口**：schema 的 preview body 只有 `{source, content, url}`、`source` 枚举只有 `upload|url`，没有字段能带二进制。所以当前只支持 `.md` 文本与 https URL；`SkillLoader` 本身支持 `kind="zip"`，要在管理面开 zip 需要 schema 增字段（本批按要求只翻 flag，不动结构）。
- **install 依赖进程内 stash**：preview 与 install 必须打到同一个 core 进程（正常情况下就是如此，admin 面是单实例）。进程重启后旧 token 一律 400，需要重新预检——与 loader 自己的内存 token 行为一致。
- **reject 的 7 天清理**不在本层：loader 的 `_expire_pending` 负责 pending 过期，`.rejected/` 的清理仍是后续任务。
- **`pending()` 是 C3b 的公开读口**：如果其他分支也在改 `loader.py`，冲突点只在 `get()` 之后这 9 行。
