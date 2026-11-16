# RESULT · RFC-0002（`turn.completed.skills`）实施

- worktree：`D:/HermesWorkspace/Vade-wt/s-rfc0002`（分支 `tmp/s-rfc0002`，基于 `dev_gingshan` @ `54032ef`）
- commit：`5e44a04`（实现 + 测试 + LEDGER 同步，先于本报告提交）
- RFC 文本：`docs/rfc/RFC-0002-turn-completed-skills.md`（164 行草案，正文/frontmatter 未动）

## 1. 语义复述（5 行）

1. **范围**：C-06 `schema/events.json` 的 `turn.completed.payload` 追加**一个可选字段** `skills`（`array<string>`）；不加 `required`、无新事件、无错误码变化、无默认值变更，其余既有条目一字不动。
2. **时机**：core 在每个 turn 以终态 `completed` 收尾时随 `turn.completed` 一起发出；内容 = 当轮 `PromptParts.activated_skills`（C3b loader 在 prompt hook 返回时回填），loop 逐字透传，是 loader→loop→事件的唯一通道（FR-13.26）。
3. **字段**：只含技能**名字**，不含正文/sha——契约行与 RFC §2 逐字一致：`"skills": "array<string> (activated skill names, sorted, never bodies; [] when none)"`；AC-32 要求与当轮实际激活一致。
4. **空集行为**：没有激活时发 **`[]`**（present-but-empty）——core 从实现批次起总是发出该键，不是缺省、不许 null；契约只保证键名与形状，`required` 不含它。
5. **向后兼容**：`required` 保持冻结三键（turn_id/state/usage）→ 旧客户端忽略未知键、语义不变；新客户端重放旧事件时缺键按 `[]` 处理（缺失 = 没有，语义唯一，RFC §3）。

## 2. 契约追加块原文（x-vade.appends 第 8 块）

```json
{
  "contract": "C-06",
  "version": "1.3.0",
  "owner": "C3",
  "doc": "docs/rfc/RFC-0002-turn-completed-skills.md",
  "adds": [],
  "fields": {"turn.completed": ["skills"]},
  "overrides": [
    "turn.completed.skills is an OPTIONAL payload key: skill NAMES only (never bodies, never sha), sorted by the loader (core/vade/skill/loader.py is the normalization point), identical to the turn's PromptParts.activated_skills; [] when none — core always emits the key (never omitted, never null) while the `required` list is unchanged (RFC-0002 §2/§3)",
    "missing and none are the same state: a replayed pre-RFC-0002 event without the key is read as [] by consumers — that is why `skills` is NOT in `required` (RFC-0002 §3)",
    "skills stays on turn.completed only: turn.skill_catalog keeps its own digest/entries/empty payload and gains nothing — one truth for 'which skills ran this turn' (RFC-0002 §5)"
  ],
  "rewrites": "none. The top-level `version` stays 2.0.0 (no state, no enum widening — RFC-0002 appends one optional payload key to one event); every pre-existing entry, `required` list and rule is untouched. This block continues the append-level non-blocking lineage (RFC-0006 1.1.0 → RFC-0013 1.2.0) at 1.3.0."
}
```

同批对 `turn.completed` 条目本身的改动（RFC-0005 先例的正规做法：payload/notes 属于"增量写进 schema 条目"，7 个登记块零字节改动）：

- payload 增行：`"skills": "array<string> (activated skill names, sorted, never bodies; [] when none)"`（§2 逐字）。
- notes 追加：`RFC-0002 append: `skills` carries activated skill NAMES only (never bodies), sorted, identical to the turn's `PromptParts.activated_skills`; [] when none — core always emits the key (never omitted, never null), while the frozen `required` list is unchanged, so a replayed old event without the key is read as [].`
- `required` 数组、顶层 `version`（2.0.0）：逐字节未动；既有 7 块以对象级比对（`appends[:7] == old_blocks`）+ git diff 双重核实为零改动。

**版本号取舍**：块级 append 版本取 **1.3.0**——续 RFC-0006（1.1.0）→ RFC-0013（1.2.0）的 append 级非破坏谱系；顶部 version 保持 2.0.0（RFC-0008 的枚举拓宽拥有那次 bump，`tools/check_contracts.py` 也钉住 2.0.0）。RFC §6.2 写的"1.0.0→1.1.0"是草案时点口径（当时顶部还是 1.0.0），现已过时。

## 3. 实现落点

| 文件 | 改动 |
|---|---|
| `core/vade/loop/engine.py` | `TurnOutcome` 尾部加 `activated_skills: tuple[str, ...] = ()`（尾部默认值，既有构造零改动）；`run_turn` 在 `apply_hooks` 之后 `activated_skills = tuple(parts.activated_skills or ())` 记下（`parts` 随后被重绑为 `_PartCursors`，这是唯一可达点），随最终 `TurnOutcome` 透传。不排序、不去重、不截断——loader 是规范化点（`loader.py:752/772` 已 `tuple(sorted(...))`）。no_capability 早退路径 hooks 未运行，保持 `()` |
| `core/vade/session/queue.py` | `_execute_one` 的 COMPLETED payload 字面量增 `"skills": list(getattr(outcome, "activated_skills", ()) or ())`——恒在、空为 `[]`、不为 null（RFC §2 实现指令原文；getattr 容忍无该字段的旧 outcome） |

**越界改动（单列）**：`core/vade/session/queue.py` 与 `core/tests/session/test_queue.py` 落在任务书的禁改目录 `core/vade/session/**` 内。理由：`turn.completed` 的**唯一生产点**在 `queue.py`（`_finalize` 的 event_type 映射；engine 只发 delta/tool 事件），不改它 `skills` 永远到不了线上 payload，AC-32 无法满足；RFC §2 实现指令第 2/3 条原文就指定 queue.py 与 `core/tests/session/test_queue.py`。改动为纯追加（payload 字典一行 + 4 条新测试，未删任何既有测试/断言），删掉该行即完全回退。审查时请点名这两处。

**落点名勘误**：RFC §1 说 payload 字面量在 `_run_one`（queue.py:170-171）；现代码已演进为 `_run_one` → `_execute_one`（payload 在 `_execute_one`），按现状落点修改。

## 4. 测试（新增 11 条，未删未 skip 任何既有用例）

`core/tests/loop/test_engine.py`（7）：

1. `test_hook_activated_skills_reach_the_outcome` — **RFC §7.2 点名**；hook 回填 → outcome 带出，名字-only
2. `test_outcome_activated_skills_default_empty_without_hooks` — 无 hook → 尾部默认 `()`，既有构造不受影响
3. `test_outcome_activated_skills_pass_through_verbatim` — 多技能顺序逐字透传（loop 不重排/不去重）
4. `test_no_capability_turn_records_no_skills` — no_capability 路径 hooks 未运行 → `()`
5. `test_schema_declares_skills_verbatim_and_required_frozen` — 契约行逐字 + required 冻结 + append 块（contract 键/1.3.0/adds=[]/fields/rewrites none）
6. `test_skills_and_skill_catalog_payloads_do_not_cross_contaminate` — 与 `turn.skill_catalog` 的 digest/entries/empty 互不污染
7. `test_skills_addition_is_additive_for_old_clients` — payload 恰为冻结四键 + skills，旧客户端无感

`core/tests/session/test_queue.py`（4）：

8. `test_turn_completed_payload_carries_activated_skills` — **RFC §7.2 点名**；线上 payload `skills == ["kb-guide","code-style"]`，冻结键齐
9. `test_turn_completed_skills_is_empty_list_when_none` — 空集 `[]`：在、非缺、非 null（旧 outcome 对象也发 `[]`）
10. `test_turn_completed_skills_is_the_only_key_old_clients_miss` — payload 键集恰为冻结四键+skills；skills-blind 读法无 KeyError
11. `test_turn_failed_payload_gains_no_skills` — `turn.failed` 形状不变（skills 不泄漏到失败路径）

**去重说明**：RFC 未要求去重，FR-13.26 要求逐字透传 → loop 不做去重（去重/排序是 loader 侧规范化的事），故无去重专项用例。
**变异自验**：临时拆掉 queue.py 的 skills 行（保留 dict 闭合）→ `3 failed, 1 passed, 15 deselected in 1.57s`（恰为第 8/9/10 条变红，第 11 条不变红）→ 还原后 `git diff --stat` 恢复为正常 5 insertions，无残留。

## 5. 门禁四条（原始输出）

1. `pytest core/tests -q`（基线 1078）→ 尾行：
   `1089 passed, 6 warnings in 65.79s (0:01:05)`
   （+11 = 本批新增；专项：RFC 点名两例 `2 passed in 0.95s`）
2. `tools/check_contracts.py` → exit=0：
   `contracts: OK (C-03/C-04/C-05/C-06/C-18 frozen, 34 error codes, config metadata complete, RFC-0005 append green, RFC-0010 session metadata green, RFC-0012 subagent family green, RFC-0014 session search green, RFC-0003 layers green, event gate green)`
3. `docs/check_docs.py` → exit=0：
   `合计 12 项，失败 0 项。`
4. `tools/check_ledger.py` → exit=0：
   `ledger: OK (14 RFC rows; rpc 1.5.0 47 methods in 3 directions, events 2.0.0 34 entries, 34 error codes, appends 10+8+3; impl paths verified, statuses verified, acceptance tables recounted)`
   （改契约后 LEDGER 已同步：`appends_events` 7→8、三文件合计 20→21、RFC-0002 行与事实块改"已实现 / 1.3.0"、服务端测试行补本次复跑数）

**RFC §7.2 其余验收口径**：

- `git diff --stat 54032ef..HEAD -- schema/` → `schema/events.json | 19 +++++++++++++++++--`（唯一 schema 文件，17 insertions / 2 deletions，两处"删除"均为原行行尾延长）
- 全部改动文件（`git diff --name-only 54032ef..HEAD`）：`schema/events.json`、`core/vade/loop/engine.py`、`core/vade/session/queue.py`、`core/tests/loop/test_engine.py`、`core/tests/session/test_queue.py`、`docs/rfc/LEDGER.md` —— 共 6 个，无越界第 7 文件

## 6. 未做项

1. **批准流程**：RFC 正文状态仍是「草案」（pre-frontmatter RFC；本批只许改 frontmatter `stage`，而它没有 frontmatter，正文不许动）——LEDGER 行记为「草案（实施已落，待批准追认）」。C-06 冻结契约的编排者追认、`docs/agents/01-contracts.md` §八 变更记录登记、§6.1 的消费方通知（UI 忽略未知键 / M4b 审计读取）均属批准后动作，未做。
2. **客户端消费**：UI / M4b 读取 `skills` 的适配不在本仓范围。
3. **push / merge**：未做（按任务书，只在本 worktree 提交）。
4. **顺序保证的边界**：`sorted` 由 loader 保证；若未来某 hook 给乱序名字，loop 透传不纠偏（用例 3 固化该行为）——这是 RFC §2 实现指令的直译，若编排者希望 loop 强制排序，需一行改动（`sorted(...)`）并同步改用例 3。
