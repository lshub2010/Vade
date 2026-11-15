# s-gitleaks-fixtures — 夹具运行时拼装 + 文档示例占位：工作区归零（ALPHA A6-3 续棒）

- 日期：2026-11-15；工具：gitleaks 8.30.1（`D:/Hermes/tools/gitleaks/gitleaks.exe`）
- 基点：主仓 worktree `D:/HermesWorkspace/Vade/Vade-wt/s-gitleaks-fixtures` @ `0443039`（origin/dev_gingshan，分支 `tmp/s-gitleaks-fixtures`）；客户端仓 `D:/HermesWorkspace/Vade-client` @ `67bfb02`
- 上棒结论（s-gitleaks-final：未通过、命中全为夹具/旧证据自引）正确；本棒按「修夹具」路径收口：**所有 key 形夹具改为运行时拼装（运行时值不变），文档示例换显式占位**，工作区（含提交树）gitleaks 与私钥 grep 双归零；规则零改动，金丝雀反向自证扫描强度未削弱。

## 结论

**工作区 0（主仓与客户端仓的 `dir .` 与私钥 grep 均 0），提交后主仓 HEAD 亦 0；历史存量已逐条登记（主仓 gitleaks 5 / 客户端 8、私钥 grep 全历史 1193——全部为测试夹具、文档示例、旧证据自引的已提交历史行），alpha 口径 = 工作区 0；历史清零需重写历史，另行拍板。测试全绿。**

## 1. 逐命中判断与处置（先判断测什么，再动手；断言一个没弱化）

主仓 4 处 gitleaks 命中（+ 主树 1 处 pycache 编译副本，随源修复自然消失）：

| 命中 | 在测什么 | 处置 |
|---|---|---|
| `core/tests/admin/test_passwords.py:96`（generic-api-key） | 「失败登录不得改写旧哈希」——口令只需是**错值**（断言 401） | `password="definitely-" + "wrong-123"`（运行时值不变） |
| `core/tests/input/test_audit.py:14`（generic-api-key） | RFC-0008 A13「审计行绝不落 VALUE」的金丝雀值；断言依赖该值：`len(SECRET)`、digest≠SECRET、`SECRET not in …`、且 **"TOPSECRET" not in …**（子串断言）→ 值必须逐字保留 | `SECRET = "sk-live-" + "TOP" + "SECRET" + "-4242"` |
| `core/tests/loop/test_args_redaction.py:118`（curl-auth-header） | args 预览对 Bearer 形值的擦除 + 复扫为空；断言含 **"sk-ABC" not in preview** → 值必须保留 sk-ABC 前缀 | 常量拆形 + 名字引用拼装（`_BEARER_FIXTURE`），运行时值不变 |
| `:119`（aws-access-token） | args 预览对 AWS 形值的擦除（AKIA+16 位 base32 域） | `"AWS_ACCESS_KEY_ID=" + _AWS_FIXTURE`，`_AWS_FIXTURE = "AKIA" + _AWS_BODY`——**用名字引用而非相邻字面量**，防 CPython 常量折叠把全值写进 .pyc（目录被扫） |

私钥 grep 工作区 6 处（4 夹具 PEM 头片段 + 2 文档字样）+ 上一棒证据自引 1 处：

| 源行 | 在测什么 | 处置 |
|---|---|---|
| `core/tests/audit/test_redact.py:11` | 敏感字段按名替换（值无关断言） | `"-----BEGIN " + "PRIVATE KEY-----"` |
| `core/tests/loop/test_tool_card_fields.py:179` | 预览复扫：PEM 头形值必须被 step-3 模式擦掉（**运行时值不变才有意义**） | `"-----BEGIN " + "RSA " + "PRIVATE KEY-----"` |
| `core/tests/test_skill/test_audit.py:15` | `scan_secrets` 识别 PEM 头（kind=private_key） | 同上拆形，运行时值不变 |
| `core/tests/test_skill/test_proposal.py:159` | skill 内容私钥被拒（INVALID_ARGUMENT） | 同上拆形 |
| `docs/design.md:411`、`docs/agents/24-C3d-events-skill-proposal.md:139` | 文档描述扫描器行为（非示例值） | 字样改「PEM 私钥头」，语义不变 |
| `shots/s-gitleaks-final/RESULT.md:50` | 上一棒证据自引 | 该行字样改写为「PEM 私钥头字样提及」（本文件不再复引原字样） |

客户端仓 7 处：

| 命中 | 在测什么 | 处置 |
|---|---|---|
| `BlobUploadCapabilityTest.kt:135/221/286`（generic-api-key） | 上传门 token 提取/透传/回包原文上交；`:144`、`:290` 有 **assertEquals(tok-abcdef) 精确断言** → 值必须逐字保留 | 文件头加 `TOK_FIXTURE_A/B` 两个 val 拆形，5 处使用点（含断言）改为拼装；`:286` 用 `${TOK_FIXTURE_A + TOK_FIXTURE_B}` 内插 JSON |
| `docs/QUICKSTART.md:204` | WebSocket 握手示例（RFC 6455 §4.1 的每连接随机 nonce 样例）——纯示例，文档不依赖其被扫中 | 换显式占位 `<16字节随机数的base64占位>` |
| `shots/g-gitleaks/RESULT.md:52/81/151` | 旧证据文档自引（JDK keyLimits 参数行 / hpke 类型注解，均为当时记录的误报形态） | 改写为占位描述（`<AES套件参数>` / `<PEP484类型联合>…`），取证语义保留；另客户端自有 `shots/s-gitleaks-final/RESULT.md:50` 同款自引一并改写 |

**未做**：删测试、skip、改断言、扩排除路径——都没有。`.gitleaks.toml` **零改动**。

## 2. 扫描数（全口径，报告均 `--redact`）

主仓（本 worktree，提交后复核口径）：

| 口径 | 命中 | 证据 |
|---|---|---|
| `dir .`（工作区，含 `__pycache__`、DLL 稳态树） | **0** | `workdir.json` / `workdir.count` |
| `git .`（分支提交历史，gitleaks 语义=历史 diff 而非 tip） | **5** | `history-after.json`（本提交树 0 贡献，5 条全为历史夹具行，分类见 §3） |
| `git . --log-opts=--all` | **5** | `history-all-after.json`（本提交树不含任何命中，5 条全为历史夹具行，分类见 §3） |
| 私钥 grep 工作区 | **0** | `grep-privkey-workdir.raw.gz` / `.count` |
| 私钥 grep 全历史（rev-list --all，50/批，与 tools/gitleaks.sh 同法） | **1193** | `grep-privkey-history.raw.gz` / `.count`（上一棒 1159；+34 = 其后新提交各自携带同一批源行，见 §3） |

客户端仓（改动**未 commit**，留父代）：

| 口径 | 命中 | 证据 |
|---|---|---|
| `dir .`（工作区） | **0** | `shots/client-workdir-after.json`（客户端仓内） |
| `git .`（HEAD，仍为旧树） | **8** | `shots/client-head.json` |
| `git . --log-opts=--all` | **8** | `shots/client-history-all.json` |
| 私钥 grep 工作区 | **0** | `shots/client-grep-privkey-workdir.raw.gz` / `.count` |

主树（`D:/HermesWorkspace/Vade/Vade`）未动、仍为旧态：合并本分支并刷新 `__pycache__` 后其 `dir .` 才归零（主树 stale pyc 为上棒已登记项）。

## 3. 历史存量登记（改不了，要重写历史才能清零）

主仓 gitleaks 历史 5 条（`--all`，全部=测试夹具的历史提交行）：

| 历史命中 | 提交 | 规则 | 类别 |
|---|---|---|---|
| `core/tests/input/test_audit.py:14` | 1300ce65 | generic-api-key | 测试夹具（SECRET 金丝雀值） |
| `core/tests/loop/test_args_redaction.py:63` | 4e8c02b3 | curl-auth-header | 测试夹具（Bearer 值） |
| `core/tests/loop/test_args_redaction.py:64` | 4e8c02b3 | aws-access-token | 测试夹具（AWS 值） |
| `core/tests/admin/test_passwords.py:94` | c75a1d43 | generic-api-key | 测试夹具（错口令） |
| `core/tests/admin/test_passwords.py:94` | e31d4f7f | generic-api-key | 测试夹具（错口令） |

私钥 grep 全历史 1193 条 = **7 种源行 × ~242 个提交树**（凡包含这些行的提交各计一次）：4 条夹具 PEM 头片段（§1 表）+ 2 条文档字样（design.md / 24-C3d）+ 1 条上一棒证据自引（s-gitleaks-final/RESULT.md:50）。按文件聚合：design.md 242、24-C3d 242、test_skill/test_audit 212、test_skill/test_proposal 202、audit/test_redact 199、tool_card_fields 92、s-gitleaks-final/RESULT.md 7（新近提交才包含）。1159→1193 的增量全部来自上一棒之后的新提交（0443039 / ce7bd0c / 19039f91 / cfc67828 等）携带同一批源行，**没有新增源行**。本提交之后 tip 树不再含任何源行；存量止于历史。

客户端历史 8 条（全部 tracked，登记）：

| 历史命中 | 提交 | 类别 |
|---|---|---|
| `BlobUploadCapabilityTest.kt:132/215/276` | d2820ea1 | 测试夹具（上传 token）×3 |
| `BlobUploadCapabilityTest.kt:135` | dca58578 | 测试夹具（上传 token）×1 |
| `shots/g-gitleaks/RESULT.md:52/81/151` | 0ed27b59 | 旧证据文档自引 ×3 |
| `docs/QUICKSTART.md:202` | 75f6c6f9 | 文档示例（握手 nonce 样例）×1 |

**历史存量已登记，alpha 口径 = 工作区 0。**

## 4. 金丝雀反向自证（归零不是把规则拆了）

scratch 金丝雀 git 仓（扫后即删，`canary/canary-summary.txt` + 掩码报告在案）：根路径 5 份**真形状假饵**（AWS 20 位形、ghp_+36 形、sk-+30 形、Bearer curl 行、完整 RSA PEM 块）——

- **断言 1 PASS**：`dir` 与 `git --log-opts=--all` 两道各命中 **5/5**：aws-access-token、github-pat、generic-api-key、curl-auth-header、private-key 全部活着（注意 curl-auth-header 命中的正是与夹具同形的 Bearer 行——规则没瞎）；
- **断言 2 PASS**：`build/`、`.venv/` 排除树里的同款 AWS 饵 **0 命中**——路径排除按路径生效、未扩大化。

## 5. 测试与门禁

- `pytest core/tests -q`：**1203 passed, 0 failed, 0 error**（`pytest-full.log`；PYTEST-EXIT:0）。
- `tools/check_contracts.py`：OK（C-03/04/05/06/18 frozen、34 错误码、各 RFC append green）。
- `tools/check_ledger.py`：OK（16 RFC 行、rpc 1.7.0 49 方法、events 2.0.0 34 条…）。
- `docs/check_docs.py`：12 项 PASS，失败 0。
- 环境注记：worktree 需 `protocol/core/python/vade_protocol.dll`（gitignored 构建产物，已自主树拷入）才能跑 transport/integration/e2e 标记测试；缺它时 12 failed + 18 errors——**非本棒改动所致**，补齐后全绿。

## 6. 边界与交接注记

- 允许清单内改动：`core/tests/**`（7 文件）、`docs/**` 示例（2 文件）、`shots/**`（上一棒证据自引改写 + 本棒证据目录）；`.gitleaks.toml` 零改动；未动 `core/vade/**`、`schema/**`、`sdk/**`；未 push、未合并。
- 客户端仓改动**未 commit**（任务书要求）：3 个源/文档文件 + 2 处证据自引改写 + 未跟踪证据 JSON；`contracts/vade` 的既有脏状态非本棒所为，未触碰。
- 客户端 `docs/QUICKSTART.md` 在任务书「7 处」清单内（allow-list 明示 desktopTest/** 与 shots/**，此处按「同样处理那 7 处」执行），留父代复核。
- 客户端仓系直接在 `Vade-client` 工作树上修改（`vade-client` 目录为同仓另一检出，未动）。

## 7. 复跑方式

```bash
# 主仓（任一：本 worktree 或合并后的主树）
gitleaks dir . -c .gitleaks.toml --no-banner --redact --report-format json --report-path out.json --exit-code 0
gitleaks git . --log-opts=--all -c .gitleaks.toml --no-banner --redact --report-format json --report-path out.json --exit-code 0
git grep -nE 'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY'          # 工作区 0；全历史加 rev-list 分批
# 客户端：合并/提交本棒客户端改动后同上三道
```

## 8. 证据清单（本目录）

- `workdir.json` / `workdir.count` — 工作区扫描（0）
- `grep-privkey-workdir.raw.gz` / `.count` — 私钥 grep 工作区（0）
- `grep-privkey-history.raw.gz` / `.count` — 私钥 grep 全历史（1193）
- `baseline-workdir-prefix.json` — 改动前基线（4 命中，对照）
- `pytest-full.log` — 全量测试 1203 绿
- `canary/` — 金丝雀自证（掩码报告 + summary）
- 客户端仓侧证据：`Vade-client/shots/client-{workdir-after,head,history-all}.json` 等
