# RFC-0002 · 在 C-06 `turn.completed` 中追加 `skills`

| 项 | 值 |
|---|---|
| 状态 | **草案**（未实施；C-06 为 `frozen`，需编排者裁决后才能改契约与实现） |
| 提出 | 技术债 A（`PROGRESS.md` §四.2：`turn.completed.skills` 未接） |
| 影响契约 | C-06 `schema/events.json` → `events.turn.completed.payload` |
| 关联契约 | C-04 `schema/errors.json`（无新增错误码）；C-14 `session.turn` 的 `turn` 摘要（不涉及） |
| 关联需求 | FR-13.4 / FR-13.26 / AC-32；`docs/design.md` §10（skill 审计）、§2.7.3（`PromptParts`） |
| 类型 | 纯追加（新增一个可选字段），无删除、无默认值变更、无错误码变更 |

## 1. 背景

设计、需求都要求 `turn.completed` 带**当轮实际激活的 skill 名**（只名字，不正文）：

- `docs/design.md:404`：`| 审计 | turn.completed 带 skills: ["kb-guide"]，**只记名字，不记正文** |`
- `docs/design.md:436`：`activated_skills` 是回传通道。loop 不 import skill 包，但要把它写进 `turn.completed.skills`。
- `docs/requirements.md:505` FR-13.4：审计记录 `turn.completed.skills`（仅名字，不含正文）。
- `docs/requirements.md:607` FR-13.26：`activated_skills` 是 loader→loop 的**唯一回传通道**；loop 只把该字段写进 `turn.completed.skills`。
- `docs/requirements.md:648` AC-32：`turn.completed.skills` 与当轮实际激活一致。

实现侧已走到一半：C3b 的 loader 在 hook 返回时回填 `PromptParts.activated_skills`
（`core/vade/skill/loader.py:741`，`core/vade/loop/prompt.py:32`），C3 的 `TurnOutcome`
（`core/vade/loop/engine.py:41`）**没有**这个字段；`TurnQueue._run_one` 里组装的 COMPLETED
payload 字面量（`core/vade/session/queue.py:170-171`）也没有它 —— 那个字面量在 `_run_one`
里，是作为参数传给 `_finalize`（同文件 175 行）的，`_finalize` 只负责落库与发事件，
自己不构造 payload。

但**契约里没有这个字段**。`schema/events.json:149-162` 原文：

```json
"turn.completed": {
  "payload": {
    "turn_id": "string",
    "state": "turn.state",
    "usage": "object",
    "stopped_reason": "stop|max_steps|no_capability|null"
  },
  "required": [
    "turn_id",
    "state",
    "usage"
  ],
  "notes": "state is exactly 'completed' (never 'done')."
}
```

C-06 顶部 `status` 是 `frozen`，`docs/agents/01-contracts.md` §七 写明「**禁止**边做边改：
实现里先加字段、回头补契约，一律打回」。`tools/check_contracts.py` 只校验 payload
`required` 是声明字段的子集，**不会**拦住一个多出来的键，所以这件事只能靠 RFC 拦住。

结论：需求与实现语义都已定，缺的是契约里的一行。本次**不改契约、不改实现**，
只留这份草案。

## 2. 决定（待批准）

在 C-06 `turn.completed.payload` 追加一个可选字段：

```json
"skills": "array<string> (activated skill names, sorted, never bodies; [] when none)"
```

并在 `notes` 补一句：`skills` 只含名字，与当轮 `PromptParts.activated_skills` 一致；
没有激活时是 `[]`，不是缺省。

**不加入 `required`**。理由见 §3。

批准后需要同步落地的实现（一次性改完，不边做边改）：

1. `core/vade/loop/engine.py`：`TurnOutcome` 增加
   `activated_skills: tuple[str, ...] = ()`（尾部默认值，既有构造不用改）；
   `run_turn` 在 `apply_hooks` 之后把 `parts.activated_skills` 记下，随 `TurnOutcome` 返回。
2. `core/vade/session/queue.py`：`_run_one` 的 COMPLETED payload 增加
   `"skills": list(getattr(outcome, "activated_skills", ()) or ())`。
3. 测试：`core/tests/loop/test_engine.py`（hook 回填 → outcome 带出）与
   `core/tests/session/test_queue.py`（`turn.completed` 事件 payload 的 `skills`
   等于当轮 `activated_skills`，含空列表的正例）；AC-32 的
   「同一输入两次 `assemble` → sha256 相同」已有覆盖，不重复造。

## 3. 为什么不放进 `required`

- **重放兼容**：`events` 表里已持久化的 `turn.completed` 事件没有这个键。客户端按 seq
  重放时会读到旧事件；把字段设为必填，等于要求消费方对「历史事件」也做失败处理。
- **消费方默认值明确**：缺失 = `[]` = 当轮没有激活任何 skill，语义唯一，不需要区分
  「不知道」和「没有」。
- **C-06 rule 4 的现有分界**：`state` 只强制在状态变更事件上。`skills` 是审计附加信息，
  与 `usage` 同类但比 `usage` 更弱（`usage` 是必填，因为它是计费口径）。

因此：**core 从实现批次起总是发出该字段（含 `[]`）**，契约只保证「键名与形状」，
不保证「一定存在」。消费方（M4b / UI）读不到时按 `[]` 处理。

## 4. 兼容性

| 场景 | 行为 |
|---|---|
| 旧客户端（不认识 `skills`） | 忽略未知键，`turn.completed` 语义不变 |
| 新客户端 + 旧事件（重放） | 缺失按 `[]` 处理，与当轮实际激活一致（旧事件本来就没记） |
| 新客户端 + 新事件 | 拿到排序后的名字数组；无激活时 `[]` |
| 正文泄漏 | 字段只允许名字；`core/vade/skill/audit.py` 的 `audit_names()` 已是唯一的规范化入口，禁止把 body / sha 放进该数组 |
| 已冻结的其他 C-06 条目 | 未触碰；`git diff` 对该文件只有插入 |

## 5. 备选方案（未采用）

- **新增独立事件 `skill.activated`**：要多一个 seq、多一条持久化记录，UI 与 M4b 仍得按
  `turn_id` 做 join；而 FR-13.4 已经把字段位置定在 `turn.completed`。多一个事件只会让
  「一轮用了哪些 skill」有两个真相。
- **塞进 `usage`**：`usage` 是 token/计费口径，把审计名字混进去会让所有读 `usage` 的
  代码都要防御性判断。
- **只写审计日志、不发事件**：AC-32 要求 `turn.completed.skills` 与当轮激活一致，
  evolve 通道（C-14 `session.turn`）也以事件为输入，日志不在链路上。
- **先改实现、后补契约**：`01-contracts.md` §七明令禁止，且 C-06 已冻结。

## 6. 未决问题

1. **台账登记是硬要求，不是可选项**：`docs/agents/01-contracts.md` §七.3 写的是「通知消费方：
   在台账表登记，列出需要适配的批次」。批准后必须在同文件 §八「变更记录」加一行：
   `C-06` / `turn.completed.payload + skills` / 影响面 `UI`（忽略未知键即可）与 `M4b`（审计需读取）。
   不登记就实施，按 §七.4「边做边改，一律打回」处理。
2. **版本号与登记方式（按既有文件的实际形态）**：C-06 的 `schema/events.json` 顶部只有一个
   **单数对象** `x-vade.append`（`333-347` 行）：`contract: C-14`、`owner: C3d`、
   `adds: [session.turn, session.closed, skill.proposed]`。它登记的是「C3d 在 C-06 之上**追加了
   哪些 topic**」，不是「谁给某个 payload 加了字段」的通用台账；C3 是 C-06 的 owner，改自己的
   契约不走这条通道，也不能把 `append` 扩成通用登记表。
   `x-vade.appends`（复数、数组）只出现在 `schema/manifest.schema.json:22`，属 C-03，记录的是
   C-14 给 manifest 追加 `subscriptions` 字段（RFC-0001），与 C-06 无关。
   所以落地方式只有一条：按 §七.2「先改契约文件 + 升版本，再改实现」，把 `schema/events.json`
   的 `version` 从 `1.0.0` 升到 `1.1.0`（纯追加），在 `turn.completed.payload` 加 `skills` 一行，
   并在 `notes` 补说明；`x-vade.append` 一个字都不动。需要编排者确认的是**版本号取 1.1.0**，
   而不是「要不要登记」。

## 7. 验收

本草案本身**不改契约、不改实现**：`skills` 的落地全部留给批准后的实施批次。因此 7.1 现在
就能跑，且必须「空 diff + 全绿」；7.2 是实施批次的验收口径，届时才允许出现 schema 改动。

### 7.1 本草案（当前批次，必须全绿）

```bash
# 1) 契约文件一行未动：输出必须为空
git diff --stat 35c4dde..HEAD -- schema/

# 2) 契约门禁：最后一行必须是 contracts: OK
python tools/check_contracts.py

# 3) 相关测试不受影响
python -m pytest core/tests/session/test_queue.py -q    # 期望：全 passed
```

### 7.2 批准后（实施批次）

```bash
# 4) 两处实现：TurnOutcome.activated_skills 带出 + _run_one 的 COMPLETED payload 加 skills
python -m pytest core/tests/loop/test_engine.py core/tests/session/test_queue.py -q
#    期望全绿，且必须新增两例（命名以此为准）：
#    core/tests/loop/test_engine.py::test_hook_activated_skills_reach_the_outcome
#    core/tests/session/test_queue.py::test_turn_completed_payload_carries_activated_skills

# 5) schema 改动只能是 events.json 一处：version 1.0.0→1.1.0 与 payload 新增 skills 一行
git diff --stat <impl-base>..HEAD -- schema/
#    期望：只有 schema/events.json 出现在输出里，其它 schema 文件不出现

# 6) 门禁仍绿：skills 是可选键，required 未变
python tools/check_contracts.py    # 期望：contracts: OK
```
