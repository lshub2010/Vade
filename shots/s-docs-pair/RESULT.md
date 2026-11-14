# s-docs-pair · 修「文档骗人」

分支 `tmp/s-docs-pair`，基线 `dev_gingshan`（worktree 创建时 `00ada9e`）。
目标：让**第一次用的人按文档能连上**，并把「写了但不存在」的东西删掉。

> 约定：本文所有真实配对码一律打码成 `<码>`（形状 `2 汉字-6 位 base32`）。
> 仓库里出现的 `青松-AAAAAA` 是 `core/tests/test_pair_banner.py:128` 的既有**占位值**，不是真码。

## 环境

| 项 | 值 |
| --- | --- |
| OS | Windows 11 + git-bash（MSYS） |
| Python | 3.12.14，`D:/HermesWorkspace/Vade/Vade/.venv/Scripts/python.exe` |
| uvicorn | 0.54.0 |
| 原生绑定 | `protocol/core/python/vade_protocol.dll`（从主仓拷入 worktree） |

## 一、9 条逐条核实

每一条都自己跑/自己读源码，未照抄 `g-desktop-live` 的结论。

| # | 它的说法 | 我的结论 | 证据 |
| --- | --- | --- | --- |
| 1 | `python -m vade --config core.toml` 报 `No module named vade` | ✅ **成立** | 在仓库根实跑，逐字回 `python.exe: No module named vade`。`vade` 包在 `core/vade/`，不在仓库根。补 `PYTHONPATH` 后**还缺第二段**：只给 `core` 会走到 `identity/store.py:111` 抛 `VadeError: INTERNAL: protocol core python binding is not importable (missing libvade_protocol.so)`。所以必须 `core` **和** `protocol/core/python` 两段 |
| 2 | 码不在第 1 行，第 1 行是 JSON 日志 | ✅ **成立**（默认配置下码在第 2 行）不过**它的表述不完整** | 默认 `core.log_level="info"`：第 1 行 `{"…","msg":"core identity loaded"}`，第 2 行 `pair_code=…`，第 3 行人话提示。**但** `log_level="warning"` 时那条 info 日志不打印，**码落到第 1 行**（实测见 §四.3）。因此正确规则是「认 `pair_code=` 前缀」，不是固定行号 |
| 3 | `PAIRING.md:59` 让用户点「客户端配对输入框」，该输入框不存在 | ✅ **成立**，且是全仓最严重的一条 | 客户端仓 `grep -rni paircode --include=*.kt composeApp/src/` 只命中协议层/环境变量/intent extra/测试，**没有任何 Compose 输入控件**。本仓 `docs/ALPHA.md` A1-2 自己写着「缺：三端都没有**输入配对码的首次运行界面**（`BrowserPairing` 只能由代码注入），双击产物连不上 core」 |
| 4 | 客户端 QUICKSTART 全文无 `VADE_SOURCE` / `VADE_PAIR_CODE`，唯一可用通道无用户文档 | ✅ **成立** | `grep -n "VADE_" docs/QUICKSTART.md` 只有 1 处 `VADE_SMOKE_PORT`（第 150 行，讲冒烟测试环境门控）。四个真实变量名一个都没有 |
| 5 | `QUICKSTART:18`「UI 数据仍是 fixtures」已过期 | ✅ **成立，但要拆开说** | `App()` 的默认参数**确实仍是** `FakeSessionSource()`（`App.kt:42`）—— 这半句没过期。过期的是结论：`Main.kt` 已改为 `source = sessionSourceFromEnvironment()`，「`UiState` 尚未读它（接线在 w-wire 批进行中）」已不成立。另：`UiState.kt` 只在源不是 `FakeSessionSource` 时才建 loader，所以**不带环境变量时仍然渲染 fixtures** —— 这一点文档没写，最容易被误读 |
| 6 | `QUICKSTART:19`「真连接 UI 未接线」已过期 | ✅ **成立** | `Main.kt` 传入 env 派生的 source；`RealCoreUiE2ETest.kt` 是**真 core + 组合后的 App** 的端到端用例（注释与断言：渲染 core 的真标题且不出现任何 fixture 标记）。「没有任何页面调用它」已不成立 |
| 7 | `QUICKSTART` §6「`python -m vade` 不铸码」已过期 | ✅ **成立** | 实测每次启动都铸并打印，`data_dir` 里已有身份也一样（连跑两次，两次都打印）。代码路径 `core/vade/__main__.py` 的 `announce_pair_code`，在 uvicorn `server.started` 之后调用 |
| 8 | `QUICKSTART:201` 期望 `policy_mode=allow_all`，最小配置实测 `default` | ❌ **它错了** | **用 QUICKSTART 自己那份 core.toml（含 `[dev] allow_all = true`）实跑，`/healthz` 返回 `{"ok": true, "version": "0.1.0", "policy_mode": "allow_all"}` —— 文档与它自己的配置自洽。** 它大概是另写了一份不含 `[dev]` 的最小配置才得到 `default`。`default` 只在 `[dev].allow_all` 缺失时出现（`__main__.py`：`policy_mode="allow_all" if app.config.get("dev.allow_all") else "default"`）。**两者都不影响配对** |
| 9 | PLAYBOOK 说「Failed 横幅常驻」，实测 `sessionsError` 无任何消费点，连不上时零反馈 | ✅ **成立** | 客户端仓 `grep -rn "sessionsError\|sessionsPhase\|retrySessions\|messagesError"` 在 `commonMain/desktopMain/androidMain` 里只命中 `SessionsLoader.kt`（定义/写入）与 `UiState.kt`（getter），**没有任何 composable 读它**；`LoadPhase` 同理，只有定义与赋值。其余命中全在 `desktopTest`。结论：连不上时界面不会显示错误横幅 |

**它说错的：第 8 条。** 另外第 2 条的方向对但结论不完整（行号随 `log_level` 变）。

## 二、它漏掉的两条（我发现的，影响「能不能跑通」）

| # | 问题 | 证据 |
| --- | --- | --- |
| A | 客户端 QUICKSTART §6 的 `PYTHONPATH=core:protocol/core/python` 在 Windows 上**跑不通** | 用 `:` 实跑 → `No module named vade`。Windows 的 `PYTHONPATH` 分隔符是 `;`。这条命令被写成可直接粘贴的样子，但粘了就失败 |
| B | 把含汉字的码直接放进 `curl -d '{"pair_code":"…"}'` 在 git-bash 里**必错**，且报错信息误导 | 实测：ASCII 假码 → `CODE_INVALID`（JSON 解析正常）；汉字假码 → `400 INVALID_ARGUMENT: pair_code is required`（看着像没传字段）；同样的汉字码走 UTF-8 文件 → `CODE_INVALID`。原因：MSYS 按 ANSI 代码页转码命令行参数，body 到 core 已不是合法 UTF-8。旧 `PAIRING.md` 给的正是 `-d` 那条 |

## 三、改了什么

### `docs/PAIRING.md`（重写，+120/−28）

- **命令自包含**：Windows / Linux-macOS 各一整块（`cd` → `cat > core.toml` → `export PYTHONPATH` → 启动），可直接粘贴。写明两段 `PYTHONPATH` 各负责什么、缺了报什么错。
- **码的位置**：明确 stdout **第 2 行**、形状 `2 汉字-6 位 base32`、有效期 `600s`、一次性；并写明 `log_level=warning` 时会移到第 1 行，脚本要认 `pair_code=` 前缀。
- **删掉「点客户端配对输入框」**：改为「**图形化配对入口尚未实现**」，当前**唯一通道**是 `VADE_PAIR_CODE` 环境变量，给出完整启动命令与四个变量的含义/默认值/写错的后果，并注明依据是 `docs/ALPHA.md` A1-2。**没有写成已经能用。**
- **新增 §四 故障排查**：8 项按顺序查（有没有打印码 → 是否被 `--no-pair-banner` 关掉 → `PYTHONPATH` → 端口是否 LISTENING → `/healthz` → 码是否过期/用过 → 客户端四个变量 → **客户端连不上时零反馈**）。
- 另补三个坑：`--config` 指向不存在的文件**被静默忽略**（回默认值）、`data_dir` 相对 CWD 而非 toml 目录、`curl -d` 的汉字转码坑。

### `docs/QUICKSTART.md`（新文件）

本仓原本**没有** QUICKSTART（`docs/` 下无此文件；客户端仓那个在 `Vade-client/docs/QUICKSTART.md`，是另一个仓）。新增一份 core 侧「从零到连上」：起 core → 拿码 → 带四个变量起客户端 → **怎么确认真连上了** → 「四个常见误解」表（把第 5/6/7/8 条的真实口径写清）→ 下一步。并**显式标注**：客户端仓 QUICKSTART 的「UI 数据仍是 fixtures」「真连接 UI 未接线」两行与其 §6 的「不铸码」结论已过期。

### `shots/s-docs-pair/`

`RESULT.md`（本文）、`e2e.sh`（端到端：起 core→取码→`/healthz`→兑换→二次兑换被拒→收进程）、`policy-mode.sh`（`allow_all` 对照实验）。

## 四、按新 PAIRING.md 实跑的真实输出

按 `docs/PAIRING.md` §一 的 **Windows 块原样执行**（只把 `cd` 指向主仓 `D:/HermesWorkspace/Vade/Vade`，因为 `.venv` 在那里；其余逐字）。真实输出（配对码已按规则打码）：

```text
$ cd /d/HermesWorkspace/Vade/Vade
$ cat > core.toml <<'EOF'
[core]
listen = "127.0.0.1:8443"
data_dir = "./data"
EOF
$ export PYTHONPATH="core;protocol/core/python"
$ .venv/Scripts/python.exe -m vade --config core.toml
{"ts":"2026-11-14T18:24:42.240Z","level":"info","logger":"vade.app","msg":"core identity loaded"}
pair_code=<码> purpose=browser expires_in=600s
vade: first-run pairing — enter this code in the app/browser (it redeems via POST /ui/session). Single-use, expires in 600s; restart the core for a fresh one. Pass --no-pair-banner to hide this.
```

→ **码确实在第 2 行**，形状 `2 汉字-6 位 base32`。

```text
$ curl -s http://127.0.0.1:8443/healthz
{"ok": true, "version": "0.1.0", "policy_mode": "default"}
```

§三 的「脚本/手工调接口」两条命令（先验证 plumbing，用的是占位码 `青松-AAAAAA`，不是真码）：

```text
$ printf '{"pair_code":"%s","client_name":"my-browser"}' '青松-AAAAAA' > body.json
$ od -c body.json | head -2
0000000   {   "   p   a   i   r   _   c   o   d   e   "   :   " 351 235
0000020 222 346 235 276   -   A   A   A   A   A   A   "   ,   "   c   l
$ curl -s -i -X POST http://127.0.0.1:8443/ui/session -H 'content-type: application/json' --data-binary @body.json
{"code": "CODE_INVALID", "message": "pair code is not valid", "retriable": false}
```

→ 字节是合法 UTF-8（`351 235 222` = 青），JSON 解析正常，所以**不是**那个误导人的 `pair_code is required`。

用**真码**兑换（body 走 stdin，全程不落盘）：

```text
HTTP/1.1 200 OK
Cache-Control: no-store
Content-Type: application/json; charset=utf-8
Server: uvicorn
Set-Cookie: <REDACTED>
Set-Cookie: <REDACTED>

{"device_id": "ui-516e1f94b419559b"}
```

同一个码再兑换一次：

```text
{"code": "CODE_INVALID", "message": "pair code is not valid", "retriable": false}
```

→ **一次性**成立。

§二 的取码命令（在**合成**的 banner 文件上验证，避免把真码写盘）：

```text
$ CODE=$(grep '^pair_code=' core-stdout.txt | head -1 | sed -E 's/^pair_code=([^ ]+) .*/\1/')
extracted CODE matches expected shape: YES
extracted CODE equals the dummy value: YES
warning-variant extraction works (码在第 1 行时): YES
```

§二 的 `tee` 变体（`| tee /dev/null`，不落盘）不改变顺序，仍是「JSON / 码 / 人话」三行。

`log_level="warning"` 的对照（第 1 行那条 info 消失 → 码在第 1 行）：

```text
$ .venv/Scripts/python.exe -m vade --config core.toml   # 该配置 log_level="warning"
pair_code=<码> purpose=browser expires_in=600s
vade: first-run pairing — enter this code in the app/browser (it redeems via POST /ui/session). Single-use, expires in 600s; restart the core for a fresh one. Pass --no-pair-banner to hide this.
```

## 五、四道门禁

| 门禁 | 结果 |
| --- | --- |
| `pytest core/tests -q` | **1133 passed, 6 warnings in 384.82s**（跑了两遍，都是 1133/0 failed） |
| `tools/check_contracts.py` | `contracts: OK (…)` |
| `docs/check_docs.py` | **合计 12 项，失败 0 项** |
| `tools/check_ledger.py` | `ledger: OK (15 RFC rows; …)` |

**关于基线 1132**：本 worktree 基于 `dev_gingshan` 的 `00ada9e`，该提交合并了 `s-flake-progress`（36% 抖动的定性修复），**比 1132 多 1 条**，所以这里的基线是 **1133**，不是 1132。已知抖动用例 `test_three_rapid_updates_become_at_most_two_events` 两轮全绿（修复已落地），我没有改动它。

**另记（与本次改动无关，预先存在）**：`pytest` 不带路径跑全仓会在 `sdk/python/tests/` 收集失败 —— `vade_sdk` 的错误表与 `schema/errors.json` 漂移（`missing=['BUDGET_EXCEEDED']`）。这属 `sdk/**`，不在本棒的改动范围，**未改**。规范命令是 `pytest core/tests -q`（见 `PROGRESS.md`）。

## 六、进程清理

用完的 core 全部收掉，端口无残留：

```text
$ netstat -ano | grep -E ':(8443|8010|18755)\s.*LISTENING'
none
```

最后一次收尾把主仓的临时 `core.toml` / `data/` / `body.json` 也删了，主仓 `git status --short` 为空。
杀 lingering 进程用的是 `MSYS2_ARG_CONV_EXCL='*' taskkill /PID <pid> /T /F`（普通 `taskkill /PID` 会被 MSYS 当路径转换）。

## 七、没动的东西 / 受限

- **客户端仓 `Vade-client` 一行未改**（硬约束）。因此 `Vade-client/docs/QUICKSTART.md` 的 4 处过期陈述、以及 `Vade-client/shots/g-live/PLAYBOOK.md` 的「Failed 横幅常驻」**仍在原地**，只能由客户端仓的 owner 修。本仓 `docs/QUICKSTART.md` §6 已显式标注哪些结论以本文为准。
- `docs/ALPHA.md` 未改（不在可改清单内）。它的 A1-2 仍然写着「桌面默认 `FakeSessionSource`，连接根本不会发生」——这半句与 `Main.kt` 的现状不符，建议后续由 ALPHA owner 更新。
- `core/**`、`schema/**`、`tools/check_*.py`、`docs/check_docs.py`、`docs/rfc/**`、`docs/status.md`、`PROGRESS.md` 均未改动。
