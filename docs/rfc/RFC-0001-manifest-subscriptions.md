# RFC-0001 · 在 C-03 `manifest` 中追加 `subscriptions`

| 项 | 值 |
|---|---|
| 状态 | 已实施（C3d，待 C2 追认） |
| 提出 | C3d（`docs/agents/24-C3d-events-skill-proposal.md` 任务书 §三：C2 漏预置则提 RFC，不得直接改冻结文件） |
| 影响契约 | C-03 `schema/manifest.schema.json`、`core/vade/registry/manifest.py` |
| 关联契约 | C-14 `schema/events.json`（订阅门禁读这个字段） |
| 类型 | 纯追加（新增可选字段），无删除、无默认值变更 |

## 1. 背景

C-14 的订阅门禁第二关是「主题必须在 `manifest.subscriptions` 里声明过」（FR-13.16 / AC-27）。
C-03 的 `manifest.schema.json` **没有**这个属性，只靠 `additionalProperties: true` 让模块「写得进去」；
而 `core/vade/registry/manifest.py` 的 `Manifest` dataclass 只保留自己认识的字段，**把它丢了**。

后果在真实链路上可复现（C3d 复审 G1）：

```
validate_manifest kept subscriptions attr: False
events/subscribe → {"accepted": [], "rejected": [{"topic":"session.turn","reason":"not in manifest"}], "replayed":0}
```

即：任何通过 `module/register` 正常注册的 evolve 模块，**永远**被判 `not in manifest`，
C-14 等于装好了但打不开。`EventBus.declare()` 是预留缝，但全仓无生产调用方。

任务书 §三要求：C-03 已冻结，C3d 无权直接改；若 C2 未预置，**提 RFC 补**。

## 2. 决定

在 C-03 追加一个**可选**字段，并在 `Manifest` 上保留它：

1. `schema/manifest.schema.json`：新增 `subscriptions`
   （`array`，`maxItems: 16`，`uniqueItems: true`，item 为 `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`，`maxLength: 64`）。
   并在 `x-vade.appends` 登记本 RFC。
2. `core/vade/registry/manifest.py`：`Manifest` 增加
   `subscriptions: tuple[str, ...] = ()`（**尾部默认值**，既有构造与旧 manifest 不用改）；
   `validate_manifest` 读 `raw["subscriptions"]`，保持原顺序去重。
3. 传递是隐式的、无需新增调用：`Registry.register()` / `update_capabilities()` 把
   `Manifest` 存进 `NodeRecord.manifest`，`EventBus.identify()` 本来就读
   `record.manifest.subscriptions`（该读取分支在 C3d 已存在，正是空的）。
   `EventBus.declare()` 降级为「不保存 manifest 的注册表」的兜底缝，不再是主路径。

## 3. 边界：注册期只校验形状，不校验主题名

主题名合法性**仍然**在 `events/subscribe` 判定（C3d 所有）：

- C-03 冻结后不应开始拒绝 C-14 拥有的名字（否则加一个主题要改两个契约）；
- 未声明/未授权的主题必须是**逐主题**的 `rejected: [{topic, reason}]`，不能变成
  `MANIFEST_INVALID` 把整个模块踢下线（§四：拒绝不断连接）。

因此 `subscriptions` 里的未知主题是**合法的 manifest**，但订阅时会被拒。

## 4. 兼容性

| 场景 | 行为 |
|---|---|
| 旧模块（无该字段） | `Manifest.subscriptions == ()`，一切照旧，订阅被拒 `not in manifest` |
| 新模块（有该字段） | 注册成功；订阅按四重门逐主题判定 |
| 未知字段 | 仍然忽略（`additionalProperties: true` 未变） |
| 已冻结的 C-06 条目 | 未触碰；`git diff` 对该文件只有插入 |

## 5. 备选方案（未采用）

- **注册期调用 `bus.declare()`**：要求 C2 的注册路径知道 `EventBus`，把 C2 和 C3d 耦合成环；
  且 `capabilities.update` 也要再调一次，容易漏。字段随 manifest 走是单向依赖。
- **把 `subscriptions` 放进 `capabilities`**：`capabilities` 是「能力」，订阅是「权限诉求」，
  混在一起会让 `capabilities.update` 的语义变成隐式扩权。顶层字段更准确。
- **改 `additionalProperties: false`**：会把所有前向兼容字段打死，违背 C-03 的兼容承诺。

## 6. 验收

- `python -m pytest core/tests/integration/test_c3d_registration_chain.py -q` → 5 passed
  （真实 `App.startup()` → `install_components()` → `make_rpc_handler()`：
  `module/register` → `events/subscribe` accepted → `pump()` → `events/deliver`，
  另含「无 `subscriptions` 仍被拒」与「`capabilities.update` 补订阅」两例）。
- `python tools/check_contracts.py` → `contracts: OK`（C-03 的结构检查未变）。
