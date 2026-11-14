# s-blob-token 交付报告：RFC-0016 提案（`ui/blob.token`）

分支 `tmp/s-blob-token`（基于 `origin/dev_gingshan` @ `15312d0`）。**纯文档**：`schema/**`、`core/**`、`sdk/**`、`tools/check_*.py`、客户端一行未动。未 push、未合并、无 `--amend`/`--rebase`/`--reset`。

## 1. 交付物

| 文件 | 变更 |
|---|---|
| `docs/rfc/RFC-0016-blob-token.md` | **新增，537 行**（`status: proposed`；14 节；A1–A12 验收；Q1–Q8 待拍板） |
| `docs/rfc/DRAFT-blob-token.md` | **删除**（-212 行；处置理由见 RFC §14，可从 git 历史取回） |
| `docs/rfc/README.md` | 索引 +2 行（补 RFC-0015 的漏登记 + RFC-0016） |
| `docs/rfc/LEDGER.md` | +1 行（`impl_state=未实施`）+ facts 块一条 + 「16 份 RFC」计数修正；aggregate **未动**（1.6.0 / 48 / appends 11+8+3） |
| `docs/status.md` | 附录 +1 行（`proposal（proposed，未实施）`） |

提交：`e3e74d5`（docs），本报告单独一次提交。

## 2. 三条断点证据：逐条复核（不是转抄草稿）

| 断言 | 我实跑的命令 / 读的符号 | 结果 |
|---|---|---|
| 契约里没有这个方法 | `grep -c "ui/blob.token" schema/rpc.json` | **0**；`grep -n "ui/blob"` 只有 `:29` 的 append 说明与 `:1241/1248/1255/1262/1269/1276` 六条路径，`directions` 里没有条目 |
| 铸造入口零调用方 | `grep -rn "issue_token" core/ --include=*.py` | 1 处定义（`blob/service.py:173`）+ 7 处测试（`conftest.py:149,263`、`test_gc.py:46,90`、`test_mount.py:66`、`test_review_fixes.py:257,281`），**生产零调用方** |
| 身份永不匹配 | 读 `server.py:222`（`f"ui-{secrets.token_hex(8)}"`）、`server.py:354-378`（Noise 静态键→白名单→`module#instance`）、`parts.py:519-522`（归属比对） | `ui-<16hex>` 与 `module_id#instance` 不同源、不同形态；`ui/submit` 的 `device_id` 走 `session/rpc.py:149-155` → `manager.py:882` → `parts.py`，故「cookie 传、原生提交」必 `PERMISSION_DENIED` |
| 契约文案确实声称有路 | `schema/rpc.json:1217` | 逐字 `"Authorization: Bearer *** minted over the already-authenticated device channel>"`——没点名方法，`<` 也掉了 |

额外核实两条契约事实：

- `schema/rpc.json` 顶部 `1.6.0`，`x-vade.appends` **11 块**，`ui_to_core` **35**、合计 **48**（`check_ledger` 同口径）。
- 我在 RFC §4.1 里写的六条 `endpoints` 与 `schema/rpc.json` 的 `http_endpoints.endpoints[].path` **逐字相等**（程序比对六个键，全部一致），不是手抄。

## 3. 一处必须上报的事实订正：本仓 core 不回 `-32601`

任务书与 RFC-0015/RFC-0006/schema notes 都说「未注册方法 → JSON-RPC `-32601`」。**在本仓这是错的**：

```
$ python -c "…SessionRPC(log=…).dispatch(conn, 'ui/blob.token', {})"
type: RpcError | code: NOT_FOUND | message: unknown method 'ui/blob.token'
                 frame: {'code': 'NOT_FOUND', 'message': "unknown method 'ui/blob.token'"}
```

链路上的三处：`session/rpc.py:52-55`（`RpcError("NOT_FOUND", …)`）、`app.py:237-241`（同）、`transport/connection.py:59-63 / :234-235`（`error` 直接放 `to_dict()`，`code` 是**字符串**）。所以客户端降级判定必须写成析取：

```
methodAbsent(err) = err.code == -32601 || (err.code == "NOT_FOUND" && err.message.startsWith("unknown method"))
```

只判 `-32601` 等于在真机上永远认不出「core 太旧」。RFC §7.1 把这个订正写成表 + 实测输出，并要求落地批次顺手改 `rpc.json` 五处 note（`:62,107,154,168,1209`）与 `rpc.py:82` 注释的措辞（不改行为）。

## 4. 对草稿的四处偏离（都在 RFC 里写明理由，评审需逐条点头）

| # | 草稿 | RFC-0016 | 理由 |
|---|---|---|---|
| 1 | 决定 7：浏览器会话**也允许**调 `ui/blob.token` | **只给原生连接**；cookie → `PERMISSION_DENIED / cookie_session` | cookie 已有能用的四道闸；铸 bearer 等于把「环境凭据」升级成「可搬运凭据」，且默认 `read_scope="core"` 下持有者知道 id 就能读。客户端分支成本只有一行 |
| 2 | 附件关闭 → `UNAVAILABLE` | **`UNSUPPORTED` + `reason="attachments_disabled"`** | 草稿错：`ui/submit` 对同一条件用的就是 `UNSUPPORTED`（`parts.py:488-493`）。同一条件两个码 = 客户端两个分支 |
| 3 | 「不是 `-32601`」的说明引旧行号、结论也需重写 | §7.1 用实测 + 三处符号重写，并给出可粘贴的判定式 | 见 §3 |
| 4 | 未说明参数是否容忍未知键 | **严格**：未知键 → `INVALID_ARGUMENT`，`device_id` 一律拒 | 铸凭据的方法静默忽略 `device_id`，客户端会误以为自己拿到了别的东西；严格把「身份不出自参数」变成可测断言（A8） |

不变的：绑 `connection.device_id`、TTL 60..900 默认 600 拒而不夹、`max_bytes` 来自 `[attach].max_bytes`、六条 `endpoints`、零新增错误码、零新增事件/配置键/表。

## 5. 门禁（原文）

```
$ python docs/check_docs.py
[PASS] C1 … [PASS] C10
合计 12 项，失败 0 项。                                   EXIT=0

$ python tools/check_ledger.py
ledger: OK (16 RFC rows; rpc 1.6.0 48 methods in 3 directions, events 2.0.0 34 entries,
34 error codes, appends 11+8+3; impl paths verified, statuses verified,
acceptance tables recounted)                            EXIT=0

$ python tools/check_contracts.py
contracts: OK (C-03/C-04/C-05/C-06/C-18 frozen, 34 error codes, … event gate green)  EXIT=0
```

`check_contracts` 绿是因为**schema 一行未动**，不是放水；`tools/check_*.py` 与 `docs/check_docs.py` 一个字都没改。

## 6. 给用户拍板的问题清单（RFC §11 的 Q1–Q8）

1. **token 绑谁？** 设备身份（现提案）／cookie 会话／两者皆可（草稿的写法）。若选「两者皆可」，需要论证它如何不把 cookie 四道闸绕过。
2. **有效期多长？** 600 s（60..900，沿用 `TOKEN_TTL_*`）是否够；改短要客户端实现「401 → 重铸 → 重试当前分片」，改长要动既有常量。
3. **一次性还是 TTL 内可重放？** 现提案可重放（一次上传要 1 begin + N PUT + 1 complete）；一次性要改六条路由的鉴权模型。
4. **解绑设备是否立刻清 token？** 现提案要求补 `blob.revoke_device` 接线（今天 `on_revoke` 只带指纹、不碰 blob，缺口窗口 = TTL 上限 900 s）。补的代价是要动 `whitelist.on_revoke` 的回调签名或调用侧。
5. **跨设备/跨主机复用允许吗？** 现提案按 bearer 本义「持有即用」；要同源证明就是 RFC-0004 §11-B 的逐请求签名，另一个 RFC 的量级。
6. **参数严格还是忽略未知键？** 现提案严格（未知键 `INVALID_ARGUMENT`）。
7. **要不要把读面收紧成「token 只能读自己设备的 blob」？** 现提案**不**（维持 `read_scope="core"`）：默认配置下「持有有效 token + 知道 file_id」就能读，这是 RFC-0004 的既有设计，为的是多端历史图片；收紧会让桌面看不到手机传的图。
8. **要不要给只读连接发 `read_only=true` 的 token？**（`blob_tokens.read_only` 列已存在）现提案不发，只读直接 `READ_ONLY_SESSION`；发的话同一方法会有两种语义。

其中 **1 / 2 / 3 / 5** 是任务书点名要问的；**4 / 6 / 7 / 8** 是我在设计时撞出来的，不拍板也能按「现提案」落地，但会影响验收口径。

## 7. 发现但**没有**动的既有漂移（不属本棒范围）

- `docs/WHERE-ARE-WE.md:15` 仍写「RFC-0015 契约登记 pending / 顶部 1.5.0」，而 `15312d0` 已把它转正为 1.6.0。这行是过期的。
- `RFC-0004:128` 与 `:308` 的 `Authorization: Bearer ***` 与 `Bearer <ui/bl…` 两处被表格竖线截断；`rpc.json:1217` 的同一句话也是残句。RFC-0016 §9.1 第 4 行要求落地时一起改写。
- `rpc.json` 五处 note 与 `rpc.py:82` 注释仍宣称 `-32601`（§3）。
- `docs/rfc/README.md` 原先漏登记 RFC-0015——本棒补上了，所以索引现在是 0001…0016 全量（0011 在 0013 之前是既有顺序，未改）。

## 8. 没做的事（明确）

- 没有实现：`ui/blob.token` 不在 `schema/rpc.json`，core 也没有 handler。RFC 的 §4.1 JSON 片段已用 `json.loads` 验证可解析，六条 `endpoints` 与 `rpc.json` 逐字比对通过，落地时可直接粘。
- 没有跑 `pytest`（纯文档，且本棒不许改 core）。
- 没有动 `tools/check_contracts.py` 的三处版本钉（`1096/1356/1480`）——RFC §13.2/§13.4 写明了落地时**只追加** 1.7.0，并给了「改成 1.8.0 仍须 FAIL」的咬合验证要求。

## 9. 基线与远端（交接提示）

- 本分支基于 `15312d0`（`origin/dev_gingshan` 当时的 tip）。
- 本棒进行期间 `origin/dev_gingshan` 前进到 `8e79529`（`docs(alpha): 校正三处过期口径…`，只动 `docs/ALPHA.md`）。因此 `git diff origin/dev_gingshan..HEAD` 会把 `docs/ALPHA.md` 显示成「被我改回旧版」——**不是我改的**（`git show --stat e3e74d5 / 5996218` 可证：我的两个提交只碰 5 个 docs 文件 + 本报告）。合并时按普通 merge 取两侧即可，两边的文件集不相交。
- 按红线**没有** rebase / amend / reset，也没有 push。
