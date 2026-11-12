# 贡献指南

本文适用于 **Vade Agent Runtime**（简称 **Vade**）。

## 一、分支模型

```
debug                 基线分支（默认分支，受保护）
└── dev_gingshan      日常开发分支（受保护）
    └── tmp/<主题>    临时分支（一次 PR 一条，合并后即删）
```

| 分支 | 用途 | 谁能进 | 保护 |
|---|---|---|---|
| `debug` | 唯一基线，对外可用的状态 | 只接受 `dev_gingshan` → `debug` 的阶段合入 PR | 禁直推、禁 force push、禁删除、需 PR |
| `dev_gingshan` | 日常开发，所有变更先落这里 | 只接受 `tmp/*` 的 PR | 禁直推、禁 force push、禁删除、需 PR |
| `tmp/<主题>` | 一次 PR 的工作区 | 直接 push | 无，合并后删除 |

> 不设 required approval：单人账号下 PR 作者无法 approve 自己的 PR，而审查由子代理以 COMMENT 形式提交，没有独立账号可投 approve —— 开了就死锁。审查证据以 PR 模板里的审查章节 + 逐条意见为准，合并前核对阻断项清零。

**不设 `main`。** `debug` 就是基线；将来对外发版时从 `debug` 打 tag，不另起发布分支。

## 二、一次变更的完整流程

```bash
# 1. 同步基线
git switch dev_gingshan && git pull --ff-only

# 2. 拉临时分支（一次 PR 一条，名字带批次号）
git switch -c tmp/c3-session-loop

# 3. 提交（原子提交，一条提交说清一件事）
git commit -m "feat(c3): 会话循环支持中断重入"

# 4. 推送并建 PR（base 是 dev_gingshan）
git push -u origin tmp/c3-session-loop
# 然后用 API / 网页建 PR，正文按 .github/pull_request_template.md 填

# 5. 子代理审查（见第三节），按意见修，推同一个分支

# 6. 合并后删分支
git switch dev_gingshan && git pull --ff-only
git push origin --delete tmp/c3-session-loop
```

阶段合入（`dev_gingshan` → `debug`）用同一个流程，只是 base 换成 `debug`，临时分支可以省略，直接对 `dev_gingshan` 开 PR。

## 三、PR 审核：派子代理，不自审

**审核者不得是被审代码的编写者。** 这条是硬约束，不因为「改动很小」豁免。

| 审查 | 依据 | 回答的问题 |
|---|---|---|
| R1 规格 | `docs/agents/70-R1-spec-review.md` | 做的和 `design.md` / `requirements.md` 说的是不是一回事；验收标准有没有漏 |
| R2 正确性 | `docs/agents/71-R2-correctness-review.md` | 边界、并发、错误路径、资源释放；测试是否真的覆盖了断言 |
| R3 安全 | `docs/agents/72-R3-security-review.md` | 鉴权、密钥、注入、越权读写、日志泄漏 |

派单要求：

1. **给子代理完整上下文**：PR diff（`git diff base...head`）、涉及的契约、对应批次任务书。不要只给「看看这个 PR」。
2. **要求逐条对照原文**，不接受「大致符合」。汇报里每条结论必须附文件路径 + 行号或命令输出。
3. **结论只三档**：`通过` / `有保留（列出必须改的条目）` / `不通过（列出阻断项）`。不允许「建议优化」这种不落地的措辞混进阻断项。
4. **主代理复核**：子代理的每条结论都要回原文核一遍，不成立的驳回并写明理由。审查报告本身也可能是错的。

审查结论以 PR review 提交，格式：

```
R1 规格：通过 / 有保留
- [blocking] design.md §7.1.3 要求 cookie 只读，实现里 POST /admin/api/skills 未校验凭据类型（src/admin/api.py:120）
- [nit] 函数名 suggest_action 与契约里的 propose_action 不一致

R2 正确性：有保留
...
```

## 四、合并门禁

合并前自检，全部满足才点 merge：

- [ ] CI 绿（有 CI 之后）
- [ ] 至少一个子代理给出 `通过`，`有保留` 的阻断项已修完
- [ ] 契约变更已在 `docs/agents/01-contracts.md` 登记
- [ ] 新增/修改的行为有对应测试，且测试在没有实现时确实会失败
- [ ] 文档同步：改了行为就改 `design.md`，改了决策就加 AMEND
- [ ] 没有把 `.env`、密钥、`core.toml`、数据库文件带进提交

完整门禁定义见 `docs/agents/00-orchestration.md`。

## 五、提交信息

```
<type>(<scope>): <subject>

<body 可选：为什么这么改，而不是改了什么>
```

- `type`：`feat` `fix` `docs` `refactor` `test` `chore`
- `scope`：批次号或模块名，如 `c3` `m4b` `u1` `admin` `proto`
- `subject`：中文，动词开头，不超过 50 字，句末不加句号
- 破坏性变更在 body 里写 `BREAKING CHANGE: ...`

## 六、契约变更

`schema/` 下的文件是跨模块契约，改动成本最高：

1. 先提 PR 只改 schema + 在 `01-contracts.md` 登记版本与影响面；
2. 合入后其他模块才能跟；
3. 契约的 owner 见 `01-contracts.md` 的「目录所有权」，非 owner 不得直接改。
