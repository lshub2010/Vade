# C3b · skill-loader（core 内）

| 项 | 值 |
|---|---|
| Agent 代号 | `c3b-skill` |
| 波次 | W4（C3 之后） |
| 依赖 | C3 的 agent-loop 骨架 + prompt 组装点；C4 之前可先用假 tool 列表 |
| 目录所有权 | `core/vade/skill/`、`schema/skill.schema.json` |
| 出口 | 三个 skill 的注入、激活、截断、审计全部可验 |

## 一、一句话任务

让 agent 能加载**跨模块的编排知识**（skill）：扫描本地目录、按需激活、渐进式注入 system prompt、在审计里留名字——**不提供任何执行通道**。

> 边界先记住：skill 是**说明书**，不是能力。它不能注册工具、不能执行脚本、不能联网加载。要执行请做模块（`docs/design.md` §2.7.4）。

## 二、必读

- `docs/design.md` §2.7（扩展点三层、skill 规格、prompt 管线 §2.7.3、为什么不能自带执行通道 §2.7.4）、§2.7.5
- `docs/requirements.md` D38、D37
- `docs/agents/01-contracts.md` C-11
- `docs/agents/22-C3-session-loop.md` §prompt 组装

## 三、目录

```
core/vade/skill/
├── __init__.py
├── model.py        # SkillMeta dataclass、frontmatter → 对象
├── loader.py       # 目录扫描、schema 校验、reload
├── matcher.py      # @mention / triggers 子串匹配、上限控制
├── render.py       # 拼进 system prompt、截断、稳定排序
└── audit.py        # 脱敏扫描、审计字段
schema/skill.schema.json
```

## 四、数据模型

```python
@dataclass(frozen=True)
class SkillMeta:
    name: str            # ^[a-z][a-z0-9-]{1,31}$
    description: str     # ≤ 200 字
    when_to_use: str     # ≤ 300 字
    triggers: tuple[str, ...] = ()
    requires_tools: tuple[str, ...] = ()
    body: str = ""       # 正文，已截断
    truncated: bool = False
    path: Path
    available: bool = True
    unavailable_reason: str = ""
```

frontmatter 解析用 `pyyaml.safe_load`，**不用 `yaml.load`**。解析失败 → 跳过该 skill + 一条 warning，**不能让 core 起不来**。

## 五、任务分解

### T1 · 加载与校验（0.5 天）

- 扫描 `<data_dir>/skills/*/SKILL.md`，深度 1，不递归子目录。
- **绝不扫 `skills/pending/`**（待批准区，C3d 提案与 T4.8 安装共用）。用 `skills/*/SKILL.md` 而非 `skills/**/`，并在代码里**显式**排除 `pending`、`.history`、`.trash`、`.rejected` 四个保留目录（这四个名字不允许作为 skill 名，加载时遇到即 warning 跳过）。
- 每个 skill 目录带 `meta.json`：`{revision, status: pending|active|disabled|stale|unavailable, origin: manual|install|proposal, last_used_at, fail_count, derived_from?, hardware?}`。缺失时视为 `revision=0`、`status=active`、`origin=manual`（兼容手工创建的 skill）。`pending` 只出现在 `skills/pending/<name>/` 里，扫描根目录时永远看不到它。
- 校验 `schema/skill.schema.json`（C-11）：name 唯一（重复 → 后加载的跳过并 warning）、description 非空、body ≤ 8KB、**四节齐全**（`When to Use` / `Procedure` / `Pitfalls` / `Verification`）。
- 缺节 → 该 skill 标 `status=invalid`，**不进目录、不注入**，warning 写 `missing section: <name>`。不要“尽力猜”——少了 `Verification` 的 skill 无法判断是否成功，注入了也是误导。
- `references/` 子目录允许存在（安装的 zip 会带模板/示例），但**只扫 `SKILL.md`，不递归**；正文里用相对路径引用。
- body 超过 8KB 或 200 行 → 截断，`truncated=True`，**不报错**。
- `path` 必须是 `realpath` 后仍在 skills 根内（防软链逃逸，和 fs 模块同一套检查）。
- reload：`SIGHUP` 触发一次全量重扫；不做文件热更（避免半写状态被读进来）。

### T2 · 匹配与激活（1 天）

| 触发 | 规则 |
|---|---|
| `@mention` | 用户消息里出现 `@skill-name`（`^[a-z][a-z0-9-]{1,31}$`），大小写不敏感匹配 `name` |
| `triggers` | 用户消息 `lower()` 后对所有 `triggers` 做**子串**匹配，命中任一即激活 |
| 显式禁用 | `@!skill-name` 强制不激活（优先于 triggers） |

- **不做语义匹配**：v1 不引 embedding，不依赖 model 模块。
- 激活上限 3 个。超出时按 `(mention 优先, trigger 命中数降序, name 字典序)` 排序，取前 3，其余只出现在目录行。
- `requires_tools` 里的工具不在当前工具快照中 → `available=False`，`unavailable_reason="missing tool: x"`，**不注入正文**，只在目录行标 `[unavailable]`。
- `meta.status == "stale"` → 同样不注入，只在目录行标 `[stale]`（腐化治理，见 T4.5）。

### T3 · 渲染与注入（0.5 天）

注入顺序（固定，不可调）：

```
1. 角色与人设
2. 工具说明（由 router 的工具快照生成）
3. 【skill 目录 L0】—— 每行 `- name: description`，含 unavailable / stale 标记
4. 【已激活 skill L1】—— 每个一节，正文原样
5. 会话上下文
```

**三层预算**（AMEND-19/D83）：

| 层 | 预算 | 超了怎么办 |
|---|---|---|
| L0 目录 | `skills.l0_token_budget`（默认 3000） | 按 `(激活优先, hit_count 降序, name 字典序)` 截断；**不要“只留 name”**——只留名字等于没信息 |
| L1 正文 | 激活的 ≤3 个，合计 ≤12KB | 从排序末尾砍正文，被砍的标 `[truncated]` |
| L2 引用 | 不进 prompt | 模型用 `fs` 读 `references/<file>` |

- 目录段与正文段之间用空行分隔，**不用 XML 标签包**（模型对标签的遵循不稳定）。
- 排序**必须稳定**：按 name 字典序。prompt 抖动会让上游 prompt cache 失效，成本翻倍。
- 正文超 200 行 → 末尾追加 `[... truncated at 200 lines, use fs to read {path} ...]`。
- 砍了什么要写日志（`skill_render_trimmed {l0_dropped:[], l1_dropped:[]}`），但**不记正文**。

### T4 · 审计与脱敏（0.5 天）

- `turn.completed` 增加 `skills: ["kb-guide", "code-review"]`，**只记名字**；`turn.failed` 同理。
- 加载时对 body 做一次扫描：`sk-[A-Za-z0-9]{20,}`、`BEGIN [A-Z ]*PRIVATE KEY`、长度 > 200 的连续 base64。命中只 **warning + 计数**，不改写、不阻断（可能误报）。
- 日志里**禁止**出现 body 全文；要 debug 只打 `path` 与 `sha256(body)[:8]`。

### T4.5 · 腐化标记（0.5 天）

skill 指向的工具失效了，靠人工发现太慢。加一条轻量回路：

```python
def record_tool_failure(tool_names: list[str]) -> None:
    # C3 的 router 在 tools/call 失败时，遍历 app.tool_failure_hooks 调用它
    # 你只累加「requires_tools 里包含这些工具」的 skill 的 fail_count
    ...
```

- 计数在**内存**，重启清零（v1 接受；不为此加持久化写放大）。
- 单个 skill `fail_count >= 3` → `status = stale`，写回 `meta.json`，下一轮起不注入。
- `stale` **不删除**，只是退出注入；恢复靠 admin 标记 active 或下一次 `approve`。
- 只统计 `requires_tools` 里声明过的工具，别的工具失败不牵连。
- **不要指望自己去找 router**：C3 的 `loop/ports.py` 暴露 `ToolFailureHook`，router 在失败时遍历调用。你只注册，不改 router。

### T4.8 · 管理面 API（1.5 天，AMEND-16 追加，AMEND-17 加 pending 流程）

配置页的技能页要能装、能卸、能启停、能看。**这些方法的唯一实现放在 `SkillLoader` 里**，C5a 只做 HTTP 包装——否则 frontmatter 校验会有两份，迟早分叉。

```python
class SkillLoader:
    # 已有：render / record_tool_failure

    def list_all(self, include_pending: bool = False) -> list[SkillState]: ...
    def get(self, name: str) -> SkillState | None: ...
    def preview(self, source: InstallSource) -> PreviewResult: ...      # 不落盘
    def install(self, source: InstallSource, preview_token: str) -> SkillState: ...   # → pending
    def approve(self, name: str) -> SkillState: ...                     # pending → active
    def reject(self, name: str, reason: str) -> None: ...               # pending → .rejected/
    def uninstall(self, name: str) -> None: ...                        # 移到 .trash/
    def set_status(self, name: str, status: str) -> None: ...          # active | disabled | stale
    def reload(self) -> None: ...                                      # 等价 SIGHUP
```

**安装来源与校验（全部在这里做，不要在 admin 里重写）**：

| 来源 | 处理 |
|---|---|
| 单文件 `.md` | 直接读入 → `yaml.safe_load` frontmatter → 校验 → 写 `skills/pending/<name>/SKILL.md` |
| `.zip` | **逐 entry 校验后手动解压**（不 `extractall`）：拒绝 `..`、绝对路径、符号链接、条目 > 32、解压后 > 5MB、**任何可执行文件**（`.sh`/`.py`/`.exe`/`package.json` 等）；必须恰好一个 `SKILL.md`，深度 ≤ 2 |
| `https` URL | `httpx` 流式下载，`max_bytes=1MB`；**禁止**回环/私网/链路本地/云元数据（`169.254.169.254`、`::1`、`10/8`、`172.16/12`、`192.168/16`、`fc00::/7`），重定向 ≤ 3 跳且**每跳重新校验** |

- **预览与安装两步**：`preview()` 返回 `{frontmatter, body_head, missing_tools, conflict, preview_token}`，`preview_token` 是 32B 随机值，存内存 60s、一次性、绑 (name, sha256(内容))。`install()` 必须带对的 token，**否则拒绝**。这不是前端防呆，是服务端约束：防止脚本直接 POST 就落盘。
- **重名拒绝**，不覆盖，不自动加后缀。改名由人决定。
- **安装落 `skills/pending/<name>/`，`status=pending`，`origin=install`**（v1.15 改，AMEND-17/A54）。理由：`.zip` 上传时管理员看到的是文件名不是正文，而 skill 是直接进 system prompt 的文本；「装完即生效」等于让「批准」这个动作消失。**`pending/` 不参与注入、不进工具表**，与 C3d 的提案共用同一目录，靠 `meta.origin` 区分。
- **`approve(name)`**：`pending/<name>/` → `skills/<name>/`（同盘 `os.replace` 整目录），`status=active`，`revision+1`，正文快照写 `.history/<name>/<rev>.md`，然后 reload。**这是唯一把 skill 从「躺着」变成「生效」的入口**。
- **`reject(name, reason)`**：移入 `.rejected/<name>-<ts>/`，7 天后清。
- **pending 7 天 TTL**：启动时与每次 reload 检查，超时移入 `.trash/` 并写审计 `skill.pending_expired`。别让它无限堆积。
- **版本更新同样回 pending**：`install()` 发现同名 skill 已 `active` 且 `sha256` 不同 → 落 `pending/<name>/`（覆盖旧的 pending），`status=pending`，**不动生效中的那份**。唯一例外：该 skill 勾了 `trust_updates=true` 且带 `SKILL.md.sig` 验签通过 → 直接替换 + 审计 `skill.auto_update`。
- **卸载移入 `skills/.trash/<name>-<ts>/`**（同盘 rename），可恢复；`.trash/` 不参与扫描、7 天后清理由 admin 页或 CLI 触发，**不在启动时自动删**（避免误删后无法追）。
- `set_status` 只改 `meta.json`，**不改 `SKILL.md` 的 frontmatter**。frontmatter 是作者声明，`meta.json` 是运行态。
- **安装期不执行任何脚本**：没有 `post_install` 钩子、不 import、不 eval。skill 只有文本与资源文件。
- 所有写操作 `os.replace` 原子落盘；目录权限 `0700`。
- **`list_all()` 不返回 `path` 的绝对路径**（前端不需要，且泄露部署结构），只返回 `name`。
- 安装/卸载/启停各写一条审计（`skill.install{name, revision, source}` 等），**不记 body 内容**。

**C5a 通过 `app.skill_loader` 拿到实例**：`install(app)` 里 `app.skill_loader = loader`（C3 的装配点表要预留这个名字，没有就 append 到 `app.prompt_hooks` 时顺手挂上）。C5a 只调方法，**不 import `vade.skill.loader` 之外的任何东西，也不碰 `data/skills/`**。

### T5 · 接入 loop（0.5 天）

```python
def install(app) -> None:
    loader = SkillLoader(
        skills_dir=app.config.data_dir / "skills",
        tool_snapshot=app.registry.tool_snapshot,   # 从 registry 取，不是 app 的属性
    )
    app.prompt_hooks.append(loader.render)          # C3 的装配点（T4.5）
    app.tool_failure_hooks.append(loader.record_tool_failure)   # C3 的另一处装配点
```

- **只 append hook，不改 `loop/` 与 `router/` 任何文件**。目录所有权到此为止。
- `loader.render(parts) -> PromptParts`：从 **`parts.user_message`** 读当轮原文做激活（不是从别处猜），填充 `skill_catalog` / `skill_bodies`，并用 `dataclasses.replace` 回填 `activated_skills`——loop 靠这个字段写 `turn.completed.skills`。
  - **不要自己维护「当前轮消息」的状态**：hook 被调用时只该看 `parts`，看全局状态会在并发会话下串味。
- `app.tool_snapshot` 不存在，别写这个名字。C3 提供的是 `app.registry.tool_snapshot()`（可调用，取当轮快照）。写成属性会在启动时 `AttributeError`。
- 启动时加载并校验；`SIGHUP` 重扫。加载失败**不阻塞 core 启动**。
- `install()` 必须幂等：重复调用不重复 append hook（`install_components` 在 dev 热重载下可能跑两次）。

### T6 · 自测（0.5 天）

```bash
pytest core/tests/test_skill -q
python -m vade.skill --check         # 打印目录 + 可用性，不启动服务（不改主入口）
```

## 六、验收

| # | 场景 | 期望 |
|---|---|---|
| 1 | 3 个 skill，其中 1 个 `requires_tools` 缺失 | 注入 2 个；第 3 个只在目录行标 `[unavailable]`；warning 1 条 |
| 2 | 用户消息含 `@kb-guide` | 该 skill 正文进 prompt，`audit.skills` 含 `kb-guide` |
| 3 | 消息含 trigger 词「之前定过」 | 对应 skill 激活 |
| 4 | 同时命中 5 个 | 只激活 3 个，其余在目录行；顺序符合排序规则 |
| 5 | body 12KB | 截断到 8KB / 200 行，`truncated: true`，prompt 末尾有提示行 |
| 6 | body 含 `sk-` 假密钥 | warning 1 条，注入内容不被改写 |
| 7 | 两次相同输入 | 生成的 prompt **逐字节相同**（稳定排序验证） |
| 8 | 软链指向 `/etc/passwd` 的 SKILL.md | 拒载 + warning，不影响其他 skill |
| 9 | core 重启 | skill 全部重新加载，无残留状态 |
| 10 | `skills/pending/x/SKILL.md` 存在 | 不出现在目录行、不注入正文、不在工具表（待批准区隔离） |
| 11 | 某 skill 依赖的工具连续失败 3 次 | `status` 转 `stale`，目录行标 `[stale]` 且不注入 |
| 12 | `install()` 一个合法 md | 落 `skills/pending/x/`，`status=pending`，`list_all()` 默认不返回它；`list_all(include_pending=True)` 返回 |
| 13 | 对 pending 调 `approve()` | 目录移到 `skills/x/`，`rev=1`，`.history/x/1.md` 存在，**同一进程内**下一轮 render 能激活它 |
| 14 | 同名 skill 已 active，再 install 不同内容的版本 | 落 `pending/x/`，生效中的 `skills/x/SKILL.md` **字节不变** |
| 15 | zip 内含 `setup.sh` | `INVALID_ARGUMENT`，且 `skills/` 下**没有**解出该文件 |
| 16 | pending 目录的 mtime 设为 8 天前，然后 reload | 移入 `.trash/`，审计有 `skill.pending_expired` |
| 17 | `meta.json` 里 `origin=proposal` 与 `install` 各一条 | `/skills` 分两栏显示，互不串 |

## 七、禁止

- 禁止 skill 注册或声明新工具（要工具 → 做模块）。
- 禁止从网络、模块注册、`capabilities.prompts` 加载 skill。
- 禁止执行 `scripts/` 下任何文件。模型要跑，只能走 `shell` 模块。
- 禁止把 body 全文写进日志或审计。
- 禁止 v1 引入语义匹配 / embedding。
- 禁止在 `render.py` 里做「按相关度重排」——排序必须确定。

## 八、交付格式

`schema/skill.schema.json` 冻结版 + 三个示例 skill（一个可用、一个缺工具、一个超长）+ 一份 `render` 输出的 prompt 快照（脱敏）+ 验收 9 条的实测结果。
