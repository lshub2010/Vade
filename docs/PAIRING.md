# 首次运行配对（PAIRING）

浏览器/客户端连真 core 时，**必须先用一个配对码换到会话 cookie**：

1. 客户端 `POST /ui/session`，body 里带 `pair_code`；
2. core 校验并消费这个码，回 `Set-Cookie: __Host-ac_ui=…`；
3. 之后的 WebSocket 升级带上这个 cookie，core 才把它当浏览器端。

没有 cookie 的连接会被 core 当成**设备端**，要求 Noise 握手（`core/vade/transport/server.py`），
纯 JSON 客户端说不通。所以「拿不到配对码」= 首次运行无法完成配对（alpha A1-2）。

> 本文的命令按「原样复制即可执行」维护，并在 Windows 11 + git-bash（MSYS）与 Linux/macOS 上
> 分别给出。两个平台的唯一差别是 `PYTHONPATH` 的分隔符：Windows 用 `;`，Linux/macOS 用 `:`。

## 一、怎么起 core

`python -m vade` 要求**两处**都在 `PYTHONPATH` 上，缺一个都起不来：

| 路径 | 提供什么 | 缺了会怎样 |
| --- | --- | --- |
| `core` | `vade` 包本身（入口 `core/vade/__main__.py`） | `No module named vade` |
| `protocol/core/python` | 原生绑定 `vade_protocol` | `vade.errors.VadeError: INTERNAL: protocol core python binding is not importable (missing libvade_protocol.so)` |

**Windows（git-bash / MSYS）** —— 分隔符 `;`。整块按顺序粘贴：

```bash
cd /d/HermesWorkspace/Vade/Vade
cat > core.toml <<'EOF'
[core]
listen = "127.0.0.1:8443"
data_dir = "./data"
EOF
export PYTHONPATH="core;protocol/core/python"
.venv/Scripts/python.exe -m vade --config core.toml
```

**Linux / macOS** —— 分隔符 `:`：

```bash
cd ~/Vade
cat > core.toml <<'EOF'
[core]
listen = "127.0.0.1:8443"
data_dir = "./data"
EOF
export PYTHONPATH="core:protocol/core/python"
.venv/bin/python -m vade --config core.toml
```

- 两条 `PYTHONPATH` 都是**相对路径**，所以必须先 `cd` 到仓库根；写绝对路径也行。
- `core.toml` 不入库，是本机文件；`./data` 已在 `.gitignore`（规则 `/data/`）。
- `data_dir` 相对**当前工作目录**解析，不是相对 `core.toml` 所在目录。
- ⚠️ `--config` 指向一个**不存在**的文件不会报错：不存在的 toml 被静默忽略，core 拿全默认值
  启动（`listen` 默认就是 `127.0.0.1:8443`）。端口对不上时，先确认 `core.toml` 真的在那儿。
- `[tls].cert` / `[tls].key` 留空时本机监听是明文 HTTP；本机 `127.0.0.1` 被视为安全上下文，
  `Secure` cookie 仍然可用。

## 二、码在哪

监听端口**真正就绪之后**，stdout 会出现三行（注意：是 stdout，不是日志文件）：

```text
{"ts":"2026-11-14T17:27:43.868Z","level":"info","logger":"vade.app","msg":"core identity loaded"}
pair_code=青松-AAAAAA purpose=browser expires_in=600s
vade: first-run pairing — enter this code in the app/browser (it redeems via POST /ui/session). Single-use, expires in 600s; restart the core for a fresh one. Pass --no-pair-banner to hide this.
```

- **配对码在 stdout 第 2 行**：第 1 行是启动时的 JSON 日志（`core.log_level` 默认 `info`），
  第 2 行才是 `pair_code=`，第 3 行是给人看的提示。
- 把 `core.log_level` 调到 `warning` 或更高时，那条 info 日志不再打印，**码会移到第 1 行**。
  所以脚本里别写死行号，认 `pair_code=` 前缀。

要在脚本里取码，先把 §一 的启动命令的 stdout 同时存一份（`tee` 照常显示在终端上）：

```bash
.venv/Scripts/python.exe -m vade --config core.toml 2>&1 | tee core-stdout.txt
```

```bash
# 取码，不回显到终端（别进 shell history）
CODE=$(grep '^pair_code=' core-stdout.txt | head -1 | sed -E 's/^pair_code=([^ ]+) .*/\1/')
```

第 2 行的字段是固定的，按单个空格切分即可：

| 字段 | 含义 |
| --- | --- |
| `pair_code` | 一次性配对码，形如 `2 个汉字-6 位 base32`（示例 `青松-AAAAAA` 是文档占位值，不是真码） |
| `purpose` | 启动横幅铸的码永远是 `browser` |
| `expires_in` | 有效期，单位秒；启动横幅固定 `600s`（10 分钟） |

解析示例：

```python
parts = dict(item.split("=", 1) for item in line.split())
code = parts["pair_code"]          # 一次性码
ttl = int(parts["expires_in"].rstrip("s"))
```

**每次启动都会铸一个新码**（`data_dir` 里已有身份也一样）。不需要时加 `--no-pair-banner`：
既不打印，也不铸码（脚本/测试用；此时无法从横幅完成首次配对）。

## 三、怎么用：当前唯一通道是环境变量

**图形化配对入口尚未实现。** 桌面 / Android / wasm 三端都**没有**「输入配对码」的输入框或
首次运行界面，`BrowserPairing` 只能由代码注入 —— `docs/ALPHA.md` 的 A1-2 自己记着这条
（「三端都没有输入配对码的首次运行界面」）。

所以现在把码送到客户端，只有一条路：**启动客户端时用环境变量传进去**。

```bash
cd D:/HermesWorkspace/Vade-client

VADE_SOURCE=real \
VADE_HOST=127.0.0.1 \
VADE_PORT=8443 \
VADE_PAIR_CODE='青松-AAAAAA' \
  gradle :composeApp:run
```

四个变量逐个都对才有用：

| 变量 | 值 | 写错的后果 |
| --- | --- | --- |
| `VADE_SOURCE` | 必须正好是 `real` | 缺它或拼错 → 客户端走假数据，**根本不尝试连接** |
| `VADE_HOST` | core 的地址 | 可省；缺省 `127.0.0.1` |
| `VADE_PORT` | 与 `[core].listen` 的端口一致 | 可省；缺省 `8443`，与 core 默认一致 |
| `VADE_PAIR_CODE` | 上面第 2 行的整串码（含汉字与连字符） | 缺它 → WebSocket 升级被当成设备端，握手失败 |

- 变量名是 `VADE_PAIR_CODE`；`VADE_PAIRED_CODE` / `VADE_PAIRCODE` 都不认。
- 码里的汉字要**原样**带上，别只复制 `-` 后面那 6 位。
- Android 走 intent extras：`vade.source` / `vade.host` / `vade.port` / `vade.pair_code`。
- 客户端侧源码：`composeApp/src/desktopMain/kotlin/vade/ui/data/SourceConfig.desktop.kt`。

⚠️ **不带这四个变量直接 `gradle :composeApp:run`，你看到的是假数据 + 「未连接 core」横幅。**
那是**默认的 fixtures 路径**，不代表 core 有问题；要真连接必须带上面的环境变量。

### 浏览器（wasm）首次配对

浏览器 `fetch` 读不到 `Set-Cookie`（它是 forbidden response header），所以 wasm 版走
**body token 通道**：`KtorTransportFactory` 在 `POST /ui/session` 上带
`x-vade-session-token: 1`，core 在满足下面三条时把**同一个** `__Host-ac_ui` 放进响应体：

1. 请求显式带 `x-vade-session-token: 1`；
2. 请求带 `Origin`（原生客户端不带，它们能读 `Set-Cookie`，不需要这份拷贝）；
3. 该 `Origin` 与本 listener 同源，或在 `[server].ui_origins` 里。

拿到 token 后浏览器仍然用 `__Host-ac_ui` cookie 认证 `/ws`（token 只是让 JS 知道这次
会话存在）；旧 core 不回 token 时，客户端退回「浏览器托管 cookie」模式 —— 只要**页面与
core 同源**，cookie 由浏览器自己保存并发送，`/ws` 升级成功即配对成功。

部署方式：

- **推荐：反向代理**把静态站点与 core 放在同一个 scheme/host/port 下。零额外配置：
  `ui_origins` 留空时，Origin 的 netloc 等于 Host 即放行（与 `/ws` 同一条规则），
  fetch 的默认 `credentials: same-origin` 也会把 `__Host-ac_ui` 存进浏览器并随
  `/ws` 升级发送。**本仓的 wasm 构建今天只验证这一种部署。**
- 页面与 core **同 host 不同端口**（例如 `127.0.0.1:8080` → `127.0.0.1:8443`）属于
  跨源：`SameSite=Strict` 的 cookie 仍在同一个 site 内可用，但 fetch 默认不发送/保存
  跨源 cookie，页面壳必须自己用 `credentials: "include"` 发请求（Ktor WASM 引擎需要
  `engine { configureRequest { credentials = RequestCredentials.INCLUDE } }`，这是
  页面壳的职责，不在本仓的配对链路里）。core 侧已经就绪：把页面 Origin 加进
  `[server].ui_origins`，`OPTIONS /ui/session` 会回答预检。
- **跨站页面（不同 host）不行**：`SameSite=Strict` 的 cookie 不会被带上。那种失败在
  配对卡上归为「浏览器没能保存这次配对」，不会误报成「码错」—— 码其实已经被消费，
  需要重新取一个。
- token 只许留在页面进程内存：`localStorage` / `sessionStorage` 一律不写（等同把 30 天
  有效的 UI 凭据交给同源的任何脚本）。刷新后浏览器仍带着 cookie，但应用会重新出配对卡。

### 脚本/手工调接口

等价的手工路径是先换 cookie 再升级 `/ws`：

```bash
# 1) 把码写进 UTF-8 文件（不要直接放进 curl -d，原因见下）
printf '{"pair_code":"%s","client_name":"my-browser"}' "$CODE" > body.json
# 2) 兑换
curl -s -i -X POST http://127.0.0.1:8443/ui/session \
  -H 'content-type: application/json' --data-binary @body.json
```

成功是 `200` + `Set-Cookie: __Host-ac_ui=…`（同时还有 `__Host-ac_csrf`）；
响应体是 `{"device_id":"ui-…"}`；之后用这个 cookie 升级 `/ws`。

换 core / 退出前自吊销这个会话（同一个 cookie，无 body）：

```bash
curl -s -i -X DELETE http://127.0.0.1:8443/ui/session \
  -H 'Origin: http://127.0.0.1:8080' \
  -H 'Cookie: __Host-ac_ui=<token>; __Host-ac_csrf=<csrf>'
```

`204` = 已吊销（响应同时下发两个 cookie 的 `Max-Age=0`，旧 cookie 立即失效，
它认证的 `/ws` 连接被关闭）；重复调用 / 无 cookie / cookie 已失效 → `401 AUTH_FAILED`，
且**只**影响出示的那一个会话。浏览器会话不绑定设备记录（`ui-…` 是每次登录现铸的标签），
所以没有设备需要一并吊销。契约：`schema/ui-api.yaml`；审计：`ui.unpaired`（`ok`/`denied`）。

> ⚠️ **Windows git-bash 的坑**：把含汉字的码直接放进 `curl -d '{"pair_code":"青松-AAAAAA"}'`
> 时，MSYS 会把命令行参数按 ANSI 代码页转码，到 core 那边已经不是合法 UTF-8，JSON 解析失败，
> 接口回一个**会误导人**的 `400 INVALID_ARGUMENT: pair_code is required`（看起来像你没传字段）。
> 用上面的「写 UTF-8 文件 + `--data-binary @body.json`」就不会踩。ASCII 码不受影响。

码的规则（都是既有实现，`core/vade/identity/pairing.py`）：

- **一次性**：兑换成功即删除，第二次兑换返回 `CODE_INVALID`；
- **有期限**：`expires_in` 到期后兑换返回 `CODE_EXPIRED`；
- **用途隔离**：`browser` 的码不能当 `module` 用，反之亦然；
- **失败预算**：同一个码错 5 次即销毁；同一 IP 5 分钟内错 5 次会被锁（`ADMIN_LOCKED`）。

## 四、故障排查（连不上时按这个顺序查）

| # | 查什么 | 怎么查 | 正常结果 / 判定 |
| --- | --- | --- | --- |
| 1 | core 起来了吗、铸码了吗 | 看 core 的 stdout 有没有 `pair_code=` 行 | 有 → 看第 2 项；没有 → 第 3、4 项 |
| 2 | 是不是被 `--no-pair-banner` 关了 | 检查启动命令里有没有这个参数 | 有就去掉；被关时**不铸码**，无码可用 |
| 3 | `PYTHONPATH` 对吗 | 报 `No module named vade` → 缺 `core`；报 `protocol binding is not importable` → 缺 `protocol/core/python`；Windows 用了 `:` → 也报 `No module named vade` | 两个路径 + 正确分隔符 |
| 4 | 端口真的在听吗 | `netstat -ano \| grep ':8443.*LISTENING'` | 有一行 → 在听；空 → 没起来或换了端口（`--config` 指错文件会静默回默认值） |
| 5 | HTTP 面通吗 | `curl -s http://127.0.0.1:8443/healthz` | `{"ok": true, "version": "0.1.0", "policy_mode": "default"}`；`policy_mode` 是 `allow_all` 还是 `default` 取决于 `[dev].allow_all`，**不影响配对** |
| 6 | 码过期/用过了吗 | 看启动时刻；`expires_in` 是 600s | 过期 → 重启 core 拿新码；第二次兑换必然 `CODE_INVALID`（一次性） |
| 7 | 客户端那四个变量对吗 | 逐个对名字与拼写 | `VADE_SOURCE=real` 必须存在，否则走假数据、不连 |
| 8 | 客户端连不上时有报错吗 | —— | **没有。** `sessionsError` / `sessionsPhase` 目前没有 UI 消费点，连不上时界面**零反馈**（只有一排空列表）。要判断连没连上，看 core 侧有没有 `/ui/session`、`/ws` 的请求，或直接查第 5 项 |

## 五、过期了怎么办

重启 core 会铸一个新码并重新打印。旧码到期后自动失效 —— `data/pairing.sqlite3` 里只存
`scrypt(normalize(code))`，没有任何地方留明文，所以**看不到旧码，只能换新码**。

## 六、安全须知

- 码只打印在终端 stdout，**不会写进 JSON 日志**，也不会落盘明文；但终端输出本身在 10 分钟内
  等同于凭据，别把启动日志粘到工单里。
- 设备/模块（Noise）配对用的是 `purpose=module` 的码，需要 `module_id`，由管理端的
  邀请/二维码路径铸造，不是这个启动横幅。本横幅只解决「浏览器首次连进来」。
