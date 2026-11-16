# docs/rfc

> 提案目录。状态 `proposed` = 已写、未批准、未实现；`已实施` 需附实施 commit 或契约版本。

| 编号 | 标题 | 状态 |
|---|---|---|
| RFC-0001 | 在 C-03 `manifest` 中追加 `subscriptions` | 已实施（C3d，待 C2 追认） |
| RFC-0002 | 在 C-06 `turn.completed` 中追加 `skills` 字段 | proposed（草稿，未实现） |
| RFC-0003 | 提示词注入分层：技能目录消息化、工具渐进披露与 stable/context 分段 | approved（已实现并合并） |
| RFC-0004 | 附件与多模态：`POST /ui/blob` 上传协议、压缩图单 `file_id` 与 content parts 消息结构 | approved（已实现并合并） |
| RFC-0005 | 模型能力查询与用量：`model/list`、会话级选择优先级与两级用量口径 | approved（已实现并合并） |
| RFC-0006 | turn 语义补充：中断留痕、工具卡字段、重生成/编辑重发、流式多段与单条长度上限 | approved（已实现并合并） |
| RFC-0007 | 子代理与后台任务：统一 `task.*` 事件族、`task/list` / `log_tail` / `cancel`、审批归属与通知去重 | approved（已实现并合并） |
| RFC-0008 | agent 要求输入：`waiting_input` 状态、`turn.input_*` 事件对与 `turn/input` 取值通道 | approved（已实现并合并） |
| RFC-0009 | 模块 UI 页面：原生声明式 schema（新契约 C-19）、只读数据绑定与三类动作 | approved（已实现并合并） |
| RFC-0010 | 会话元数据：分组（文件夹）/ 图标 / 颜色 / 置顶，与 `session/*` 十方法 | proposed（core 侧已实现：C-05 1.3.0 + C-06 两事件，见 `core/vade/session/RESULT-rfc0010.md`） |
| RFC-0012 | 子代理事件族 R3：`subagent.spawned` / `subagent.status` / `subagent.result` 三事件与 `subagent/list` / `subagent/get` 两方法 | proposed（core 侧已实现：C-05 1.4.0 + C-06 三事件，见 `core/vade/subagents/RESULT-rfc0012.md`） |
| RFC-0011 | InputBar 拆分为「输入区」与 `SendOrb`（C-09 组件 11 → 12，破坏性）：三个皮肤槽位独立撞到「框内发送」与 v2 框外 48dp 球冲突 | proposed（客户端仓已实现：`d667442`，三皮肤同步 + ChatThread 改回用 Skin；见 `vade-client/docs/rfc/RFC-0011-inputbar-split.md`） |
| RFC-0013 | 中断留痕落库（R9）：partial 消息进 snapshot、C-06「only from turn.delta」旧注记改写、C-05 `ui/attach` 声明 `partial` | proposed（落库复用 RFC-0006 §4 既有实现；契约修订 + 验收测试见 `core/vade/session/RESULT-rfc0013.md`） |
| RFC-0014 | 会话搜索：`session/search`（标题 + 正文参数化 LIKE 扫描、两档排序、片段硬截断，C-05 1.4.0 → 1.5.0） | proposed（core 侧已实现：`session/search` 方法 + `search.py` 语义模块，见 `core/vade/session/RESULT-rfc0014.md`） |
| RFC-0015 | 模块清单通道：`ui/modules`（一次拉全量模块与页面入口、`revision` 单调、`module.status` 作失效边，C-05 1.5.0 → 1.6.0） | proposed（core 侧已实现 + 契约已登记为 `x-vade.appends` 第 11 块；客户端未接线，见 `docs/rfc/LEDGER.md`） |
| RFC-0016 | 附件上传的设备凭据铸造：`ui/blob.token`（补上 RFC-0004 §4.5 漏登记的方法，让原生配对设备能上传并提交附件，C-05 1.6.0 → 1.7.0） | proposed（**未实施**：本棒只交付设计，`schema/**` 与 `core/**` 一行未动） |
