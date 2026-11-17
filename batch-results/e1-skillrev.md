# e1-skillrev 批次结果（P1-1：pending revision 不标识字节）

## 结论

`skills/pending/<name>` 的 `revision` 以前复制**活动版本号**：同一 name 连续两次 install 后，
两份提案的 revision 相同，`core/vade/admin/skills.py` 里 `revision != pending.revision` 永不触发，
`approve` 会上线**后到的、没人审过的正文**。

现在 pending 由 loader 自己盖一个**严格递增的独立 revision**，并且「比较 revision」与
「批准/驳回」在 loader 的同一把 `RLock` 里完成。HTTP 全链路（admin app + 真实 loader）复现
脚本已固化为测试：`active rev1 → install B → install C → approve(revision=1)` 现在是
`409 TOOL_CONFLICT`，只有带 C 的 revision（3）才能让 C 上线。

## 改了什么

| 文件 | 内容 |
|---|---|
| `core/vade/skill/loader.py` | `install()` 用新私有方法 `_stamp_pending_revision()` 给 pending 盖章；`approve()` / `reject()` 新增 keyword-only `expect_revision`，检查与移动在同一把锁内；`reload()`/`uninstall()`/`set_status()`/`rollback()`/`preview()` 的共享可变状态也纳入同一把 `RLock`。 |
| `core/vade/admin/skills.py` | approve/reject 不再自己读 `pending()` 再比对（那是 TOCTOU），改为把请求里的 `revision` 作为 CAS token 传给 `loader.approve/reject(expect_revision=...)`；`NOT_FOUND` / `TOOL_CONFLICT` 语义不变。 |
| `core/tests/admin/test_skills.py` | 新增 2 个 HTTP 级回归测试（approve / reject 各一）；两处既有断言按新语义调整（`pending_revision` 1 而不是 0；第二次 install 后用 revision 2 批准）。 |
| `core/tests/test_skill/test_install.py` | 新增 4 个 loader 级测试：盖章独立且重启后保持、approve/reject 的 CAS、reject 拒绝的是被驳回的那份、6 线程并发 approve 只成功一次。 |

## 选型：独立递增 revision，而不是 sha256 CAS

两个方案都能堵住复现路径，选 revision 的理由：

1. **已经是公开契约里的字段**。`SkillSummary.revision` 与详情页的 `pending_revision` 就是这个值，
   UI 已经在「看详情 → 返回 revision → 点批准」的链路里传递它。选它不需要改
   `schema/admin-api.yaml`，也不需要让 UI 认识一个新字段。
2. **它是人可读的、每次提案唯一的身份**。sha256 CAS 需要把摘要摊到 API/UI 上，而摘要对审核者
   没有语义；revision 同时还是 approve 将要写入 `.history/` 的编号，审核者能直接对上
   「我批的是第 3 版」。
3. **跨进程/重启仍然单调**。revision 落在 `pending/<name>/meta.json`，`reload()` 读回来，
   所以重启后 `_pending` 里的身份不变（有测试）。
4. sha256 并没有消失：`_verify_pending_digest()` 仍然拒绝「intake 之后被改过的 pending 文件」，
   它管的是**磁盘字节**与 intake 记录一致，和 revision 管**审的是哪一份提案**是两层不同的闸门。
   我们不加第二套 CAS token，避免同一个问题有两个真相来源。

## pending revision 的规则

```
pending.revision = max(active.revision, 上一份 pending.revision, 磁盘 meta.json 的 revision) + 1
```

- 没有任何 active / pending 时从 **0** 开始（保持首次 install 的既有行为）。
- 每次 install 都 +1：同一 name 的 B、C 两份提案不会共号；C 被批准后 active 正好等于 C 的戳。
- approve 写回 `max(pending.revision, 旧 active.revision + 1)`：新名字第一次批准仍然是
  rev0 → rev1（既有测试语义），而旧格式 pending（戳等于 active 的那批）也不会写出重复版本号。
- `_stamp_pending_revision` 会读磁盘上的 `pending/<name>/meta.json`，所以别的进程（C3d proposal
  服务）刚写进去、本进程还没 reload 的提案也不会被重新盖成同一个号。

## 锁与 TOCTOU

- `SkillLoader._lock = threading.RLock()`：approve/rollback 会在持锁时调 `reload()`，必须可重入。
- `approve(name, expect_revision=N)`：`_require_name` → 取 `_pending[name]` → 比对 N →
  校验磁盘摘要 → 移目录 → 写 meta/history → reload，全在一个临界区。调用方先读后比再调用
  的写法已被删除（那正是复审复现的窗口）。
- `reject(name, reason, expect_revision=N)`：同样在锁内比对再移动；不匹配时**不移动任何目录**。
- 并发 approve 同一 revision：测试里 6 个线程同时打，恰好 1 个成功、5 个 `NOT_FOUND`
  （赢家已经把 pending 摘走），`.history/` 只有一份 `1.md`。

## 验证

- `python -m pytest core/tests/test_skill core/tests/admin -q` → **223 passed**
- 其中新增：
  - `tests/admin/test_skills.py::test_a_superseded_proposal_cannot_be_approved_with_the_old_revision`
  - `tests/admin/test_skills.py::test_reject_with_a_superseded_revision_is_a_conflict`
  - `tests/test_skill/test_install.py::test_each_install_stamps_its_own_pending_revision`
  - `tests/test_skill/test_install.py::test_approve_and_reject_require_the_revision_they_were_shown`
  - `tests/test_skill/test_install.py::test_reject_with_the_shown_revision_moves_that_proposal`
  - `tests/test_skill/test_install.py::test_concurrent_approvals_with_one_revision_promote_exactly_once`
- 并发用例单独重跑 3 次均通过。

## 已知边界（不在本批次范围）

- **C3d proposal 入口仍写 `revision = base_revision`**（`vade/skill/proposal.py::_write`，本次按要求未改）。
  同一 name 连续两次 `submit` 而中间没有 reload/approve 时，两份提案的 revision 仍会相同；admin
  approve 走的是详情页返回的 `pending_revision`，在那条路径上 CAS 只能挡住「审后又被 install 覆盖」，
  挡不住「审后又被 proposal 覆盖」。彻底修需要 proposal intake 也调用同一个盖章函数（或让
  `ProposalReview.approve` 传 `expect_revision`）。已作为后续项记录。
- 锁是**进程内**的：两个进程同时写同一个 `skills/` 目录仍可能互相覆盖（跨进程需要文件锁）。
  当前 admin 是单进程装配（`ctx.skills` 一个实例），不在 P1-1 范围内。
- 未跑全量 pytest（按要求）；除 `test_skill`、`admin` 外的套件未执行。
