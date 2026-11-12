# M3 · `web` 网络模块

| 项 | 值 |
|---|---|
| Agent 代号 | `m3-web` |
| 波次 | W5 |
| 依赖 | C4 的 SDK v0.1 |
| 目录所有权 | `modules/web/` |
| 出口 | 五道门 + SSRF 全套用例被拒（AC-16） |

## 一、一句话任务

让模型能读网页、能搜东西，但**不能**借它去访问 `127.0.0.1`、内网、云元数据地址，也不能被一次 302 跳转骗进去。

## 二、必读

- `docs/design.md` §15.2（web 规格）
- `docs/requirements.md` A16（第 3 条）、AC-16
- `sdk/PYTHON_SDK.md`
- `docs/agents/01-contracts.md` §二

## 三、目录

```
modules/web/
├── manifest.json
├── src/web_module/
│   ├── tools.py        # 纯函数，零 SDK import
│   ├── ssrf.py         # 地址校验 + 固定解析
│   ├── fetch.py        # HTTP 客户端
│   ├── search.py       # 搜索后端适配
│   └── server.py
├── tests/{test_tools.py,test_ssrf.py}
├── Dockerfile
└── README.md
```

## 四、工具规格

| 工具 | 参数 | risk | timeout |
|---|---|---|---|
| `fetch` | `url: str, method: str = "GET", headers: dict = {}, body: str = "", max_bytes: int = 2097152` | low | 30s |
| `search` | `query: str, count: int = 10` | low | 20s |

## 五、SSRF 防护（核心）

### 禁止的地址段

```python
BLOCKED_V4 = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4",
)]
BLOCKED_V6 = [ipaddress.ip_network(n) for n in (
    "::1/128", "fc00::/7", "fe80::/10", "ff00::/8", "::/128",
)]
```

额外拒绝：`169.254.169.254`（AWS/GCP/Azure 元数据）、`metadata.google.internal`、`100.100.100.200`（阿里云）、`fd00:ec2::254`。

### 解析与连接必须绑定（防 DNS rebinding）

```python
async def guarded_connect(url):
    host = urlparse(url).hostname
    infos = await loop.getaddrinfo(host, port, type=SOCK_STREAM)
    ips = {info[4][0] for info in infos}
    for ip in ips:
        if is_blocked(ip):
            raise ToolError("PERMISSION_DENIED", f"blocked address: {ip}")
    # 关键：后续连接只用这些 IP，不允许再解析一次
    return ips
```

- **一次解析、一次校验、连接校验过的 IP**。若用 `httpx`，用 `extensions={"sni_hostname": host}` + URL 里填 IP + `Host` 头；若版本不支持，改用 `aiohttp` 的 `TCPConnector(resolver=PinnedResolver(ips))`。
- 单元测试必须模拟 rebinding：resolver 第一次返回公网 IP、第二次返回 `127.0.0.1`，断言实际只解析一次且从未连到回环。
- URL 只允许 `http` / `https`；`file://`、`gopher://`、`ftp://`、`data:` 一律 `INVALID_ARGUMENT`。
- 端口不限，但 0 端口拒绝；URL 长度 ≤ 2048。

### 重定向

- 最多 5 跳；**每一跳都重新走完整校验**（不能只校验第一跳）。
- 跨 scheme 降级（https → http）允许但记 warning 到日志；跳到 `file://` 拒绝。
- 不自动带 cookie（客户端不启用 cookie jar）。

### 请求头

允许：`User-Agent`、`Accept`、`Accept-Language`、`Content-Type`、`Content-Encoding`、`Cache-Control`。
拒绝：`Host`、`Cookie`、`Proxy-*`、`X-Forwarded-*`、`Authorization`（需要鉴权的场景由配置里的固定头注入，不来自模型参数）。
日志里所有头值只记长度与 digest，不记原文。

## 六、响应处理

| 项 | 规则 |
|---|---|
| 大小 | `max_bytes` 默认 2MB（硬上限 8MB）；流式读，超限即停并 `truncated: true` |
| 类型 | `text/*`、`application/json`、`application/xml` → 解码文本（UTF-8 / charset，失败 `replace`）；其余 → 只返回 `{content_type, size, truncated}`，**不返回二进制体** |
| 解压 | 支持 gzip/deflate；解压后仍受 `max_bytes` 限制（防 zip bomb） |
| 返回 | `{status, final_url, headers(白名单), text?, content_type, truncated, bytes}` |
| 错误 | 4xx/5xx **不是**工具错误，`ok: true` + `status`，让模型自己判断 |

## 七、搜索

`search` 需要一个后端，配置项 `WEB_SEARCH_BACKEND`：

| 值 | 行为 |
|---|---|
| `searxng` | `GET {SEARXNG_URL}/search?q=...&format=json`，解析 `results[]` |
| `brave` | `GET https://api.search.brave.com/res/v1/web/search`，`X-Subscription-Token` 从环境读 |
| `none`（默认） | 返回 `UNSUPPORTED("search backend not configured")` |

- 搜索结果统一 `[{title, url, snippet}]`，`count ∈ [1,20]`。
- 结果 URL **也要过 SSRF 校验**（只校验，不抓取）。
- 不得依赖 `model` 模块；不得调用 LLM 做摘要。

## 八、验收（五道门 + 安全）

```bash
cd modules/web

python -m web_module --standalone fetch --args '{"url":"https://example.com"}'

# 必须全部拒绝
for u in "http://127.0.0.1/" "http://localhost/" "http://169.254.169.254/latest/meta-data/" \
         "http://10.0.0.1/" "http://[::1]/" "http://192.168.1.1/" "file:///etc/passwd"; do
  python -m web_module --standalone fetch --args "{\"url\":\"$u\"}" || echo "REJECTED $u"
done

pytest tests/test_ssrf.py -q
```

`test_ssrf.py` 必测：
1. 上述地址全部拒绝；
2. 重定向到 `127.0.0.1` 被拒（起一个本地 302 服务）；
3. DNS rebinding（可注入 resolver）；
4. `http://example.com@127.0.0.1/`（userinfo 欺骗）；
5. 十进制/八进制 IP（`http://2130706433/`、`http://0177.0.0.1/`）；
6. 超 2MB 响应截断且不继续下载；
7. gzip bomb（10MB 压缩成 1KB）截断；
8. 二进制响应不返回 body。

## 九、禁止

- 禁止只校验第一跳重定向。
- 禁止校验域名而不校验解析后的 IP。
- 禁止把解析结果缓存给下一次请求（会掩盖 rebinding）。
- 禁止自动带 cookie / 自动加 `Authorization`。
- 禁止在 `tools.py` import SDK。
- 禁止用第三方"安全 URL 校验"库替代上面的显式网段表（不可审计）。

## 十、交付格式

按 §三，额外附：SSRF 用例逐条输出、rebinding 测试的解析次数日志、重定向链路日志、gzip bomb 的截断证据。
