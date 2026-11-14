# 首次运行配对（PAIRING）

浏览器/客户端连真 core 时，**必须先用一个配对码换到会话 cookie**：

1. 客户端 `POST /ui/session`，body 里带 `pair_code`；
2. core 校验并消费这个码，回 `Set-Cookie: __Host-ac_ui=…`；
3. 之后的 WebSocket 升级带上这个 cookie，core 才把它当浏览器端。

没有 cookie 的连接会被 core 当成**设备端**，要求 Noise 握手（`core/vade/transport/server.py`），
纯 JSON 客户端说不通。所以「看不到配对码」= 首次运行无法完成配对（alpha A1-2）。

## 怎么起 core

```bash
python -m vade --config core.toml
```

`core.toml` 里至少要有：

```toml
[core]
listen = "127.0.0.1:8443"
data_dir = "./data"
```

（`[tls].cert` / `[tls].key` 留空时本机监听是明文 HTTP；本机 `127.0.0.1` 被视为安全上下文，
`Secure` cookie 仍然可用。）

## 码在哪

监听端口真正就绪后，stdout 会多出两行（注意：是 stdout，不是日志文件）：

```text
pair_code=青松-AAAAAA purpose=browser expires_in=600s
vade: first-run pairing — enter this code in the app/browser (it redeems via POST /ui/session). …
```

第一行是给程序读的，字段固定：

| 字段 | 含义 |
| --- | --- |
| `pair_code` | 一次性配对码，形如 `2 个汉字-6 位 base32` |
| `purpose` | 启动横幅铸的码永远是 `browser` |
| `expires_in` | 有效期，单位秒；启动横幅固定 `600s` |

解析示例（第一行按单个空格切分即可）：

```python
parts = dict(item.split("=", 1) for item in line.split())
code = parts["pair_code"]          # 一次性码
ttl = int(parts["expires_in"].rstrip("s"))
```

**每次启动都会铸一个新码**。不需要时加 `--no-pair-banner`：既不打印，也不铸码
（脚本/测试用；此时无法从横幅完成首次配对）。

## 怎么用

在客户端（App / 管理页面）的配对输入框里输入这个码即可；或者直接调接口：

```bash
curl -i -X POST http://127.0.0.1:8443/ui/session \
  -H 'content-type: application/json' \
  -d '{"pair_code":"青松-AAAAAA","client_name":"my-browser"}'
```

成功是 `200` + `Set-Cookie: __Host-ac_ui=…`（同时还有 `__Host-ac_csrf`）；
之后用这个 cookie 升级 `/ws`。

码的规则（都是既有实现，`core/vade/identity/pairing.py`）：

- **一次性**：兑换成功即删除，第二次兑换返回 `CODE_INVALID`；
- **有期限**：`expires_in` 到期后兑换返回 `CODE_EXPIRED`；
- **用途隔离**：`browser` 的码不能当 `module` 用，反之亦然；
- **失败预算**：同一个码错 5 次即销毁；同一 IP 5 分钟内错 5 次会被锁（`ADMIN_LOCKED`）。

## 过期了怎么办

重启 core（`python -m vade …`）会铸一个新码并重新打印。旧码到期后自动失效——
`data/pairing.sqlite3` 里只存 `scrypt(normalize(code))`，没有任何地方留明文，
所以**看不到旧码，只能换新码**。

## 安全须知

- 码只打印在终端 stdout，**不会写进 JSON 日志**，也不会落盘明文；但终端输出本身
  在 10 分钟内等同于凭据，别把启动日志粘到工单里。
- 设备/模块（Noise）配对用的是 `purpose=module` 的码，需要 `module_id`，由管理端的
  邀请/二维码路径铸造，不是这个启动横幅。本横幅只解决「浏览器首次连进来」。
