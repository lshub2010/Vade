# C3d · 事件推送与 skill 提案结果

| 项 | 状态 |
|---|---|
| C-14 事件追加（`session.turn` / `session.closed` / `skill.proposed`） | ✅ 纯追加，C-06 原条目未改 |
| C-13 `skill-proposal.schema.json` | ✅ 冻结 |
| EventBus（四重门禁 / 回放 / 背压 / 审计） | ✅ |
| 提案状态机（pending → approve/reject/rollback） | ✅ |
| `manifest.subscriptions`（M2） | ✅ 已打通（RFC-0001） |
| 契约门禁（M1） | ✅ 按 `x-vade.push` 标记校验，未删检查 |
| 单测 | ✅ **328 passed** |

## 一、实测

```
pytest core/tests -q          → 328 passed
tools/check_contracts.py      → contracts: OK（event gate green）
```

新增集成测试 `core/tests/integration/test_c3d_registration_chain.py`（5 条）：
`App.startup()` → `install_components()` → `make_rpc_handler()` → `module/register` →
`events/subscribe` accepted → `pump()` → `events/deliver`，并验证：
- manifest 无 `subscriptions` → `not in manifest`（fail-closed）
- `capabilities.update` 增加订阅后立即可投递
- 核心侧脱敏；`allow_transcript=false` 时 `disabled by config`、零帧
- 工具角色订阅 `skill.proposed` → `role not allowed`

## 二、M1 · 契约门禁（不删检查）

`tools/check_contracts.py` 新增：
- `PUSH_TRANSPORT` / `push_declared()` / `pushed_event_names()` / `code_topics()`
- 四条双向校验：推送主题必须在 C-06 声明；每个推送主题必须带 `x-vade.push: events/deliver`；
  标记集合必须等于 `subscribe.EVOLVE_TOPICS | ADMIN_TOPICS`；代码里的推送主题必须与 schema 一致

红证据（突变 A：删掉 `session.turn`）→ FAIL，三条消息同时命中（absent / missing marker / serves-not-declared）。

## 三、M2 · manifest.subscriptions

- `schema/manifest.schema.json`：可选 `subscriptions`（≤16、去重、名称模式）
- `core/vade/registry/manifest.py`：`Manifest.subscriptions: tuple[str, ...] = ()`，形状错误 → `MANIFEST_INVALID`
- `EventBus.identify()` 读真实 `NodeRecord.manifest.subscriptions`；`declare()` 降级为备用
- RFC：`docs/rfc/RFC-0001-manifest-subscriptions.md`

## 四、提案链路探针（仓外运行）

- create（revision 0）→ `skills/pending/kb-guide/{SKILL.md,meta.json}`
- approve → rev 1；patch（base 1）→ rev 2；approve → rev 2
- `catalog()`：`[] → [rev1] → [rev1] → [rev2]`，pending 全程不可见
- 审计：`skill.proposal` / `skill.proposal.approved`

## 五、未验证

- 真实浏览器/真实代理下的 SSE 重连
- 跨进程事件投递（EventBus 是进程内状态）
- `truncated` / `redacted` 字段未进 schema（仅审计记录里有）
- 发送时不复核权限（订阅时校验一次）
