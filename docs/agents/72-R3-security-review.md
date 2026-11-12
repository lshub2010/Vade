# R3 · 审查 C：安全专项

> 只用于 C1 / C2 / C5a / C5b / M1–M6 / F1。
> 你的任务不是"看看有没有明显漏洞"，而是**按攻击者视角逐条尝试**。每条结论都要有复现或代码位置。

## 一、先确认威胁模型

| 角色 | 信任级别 | 能做什么 |
|---|---|---|
| core | 唯一可信 | 持有私钥、分配 seq、做审批判定 |
| 模块 | **不可信**（可能被恶意代码替换） | 只能响应 core 的调用；不能反向调用其他模块 |
| 系统浏览器 | **不可信环境**（XSS 可控） | 只有 TLS + 密码；**只读**；不存长期私钥 |
| App 内 WebView | **不可信环境**（XSS 可控）+ **受控宿主** | 页面来自 App 包（不是网络）；私钥在安全区不可导出；签名经原生模板校验 |
| 网络 | 不可信 | 必须 WSS + 应用层加密；抓包 0 明文 |
| 管理员 | 可信但会误操作 | 破坏性操作要二次确认 + 审计 |

任何「为了方便」削弱以上边界的实现，直接 `critical`。

**一句必须写在前面的结论**：WebView 不比浏览器「更安全」——设备被入侵时两者一样被入侵。WebView 的收益只有三条：能调安全区做签名、能限定加载什么/连哪里、有稳定设备身份。**如果实现里出现「因为是我们自己的 App 所以不用管 XSS」，那这条收益立刻变成负的**——加载远程 HTML + JS 桥就是把自己变成 Cordova 的经典漏洞。

## 二、检查清单

### 1. 密钥与凭据
- [ ] 私钥是否只存在于：core 内存 + 一个 `0600` 种子文件？（P5）
- [ ] `ui` 模块里是否**没有**任何 `/admin/*` 转发逻辑？（有 → `critical`：管理面明文过模块进程）
- [ ] 公钥上传是否要 **step-up 重输密码**？只查 CSRF 不够（会话被借用时攻击者不知道密码）。上限是否 5 台？列表是否显示 `hardware_backed` 与 `added_from_ip`？
- [ ] 技能安装是否两步（preview → install）且**服务端**校验 `preview_token`？只靠前端禁用按钮 → `high`
- [ ] zip 解压是否逐 entry 校验（`..` / 绝对路径 / 符号链接）？是否用了 `extractall`？（用了 → `critical`）
- [ ] URL 安装是否禁了回环/私网/`169.254.169.254`？重定向是否每跳重校验？（没做 → `high`，这是 SSRF）
- [ ] 记忆管理响应里是否**不含** `user_text` / `assistant_text`？是否存在 `admin_update` 之类的编辑端点？（有 → `high`）
- [ ] 记忆的 promote/archive/restore 是否写审计？`GET` 是否**不**写审计（否则日志爆炸）？
- [ ] `admin_list` 的 `q` 是否走 `LIKE` 而不是向量检索？（走向量会让翻页结果不稳定，且成本随页数线性上升）
- [ ] 管理面登录是否**只有**密码 +（远程）公钥两因子，没有残留的 token / `Bearer` / 上传公钥入口？
- [ ] 设备登记端点是否都要凭据？匿名可写 = `critical`；`/admin/bind` 有绑定票即为合法入口，不算匿名
- [ ] 密码是否 `Argon2id(m=64MiB,t=3,p=1)`、≥12 字符、拒绝弱密码表？失败是否统一 `AUTH_FAILED`？
- [ ] setup 准入是否为「回环免 token / 非回环必须 token」两条通道？非回环缺 token 是否 `401 SETUP_UNAUTHORIZED`（与错 token 同码）？
- [ ] `setup_token` 是否**只从 POST body** 读？有没有 `?token=`、`X-Setup-Token`、日志、审计、错误响应里出现它的值（含前后缀）？（有 → `critical`）
- [ ] `data/setup.token` 是否 `0600`、父目录 `0700`？setup 成功后是否真的 `unlink`（不是改名/标记）？已初始化后残留文件是否**不被接受**？
- [ ] token 是否**每次启动都重新生成**（未初始化 + 无 env）？验证：`docker restart` 两次，`cat` 到的值不同，旧值 401
- [ ] 设了 `VADE_SETUP_TOKEN` 时是否**不轮换**（否则每重启一次用户手里的值就废一次）
- [ ] 启动时是否 `flock data/core.lock`？起第二个进程验证：非 0 退出，且没有两个进程同时写 `data/`
- [ ] 改密码后是否**吊销其它会话**（服务端版本号失效，不是等 cookie 过期）？`logout-all` 是否连当前会话一起断？
- [ ] 写入顺序是否为「先 `os.replace` 写 `admin.json` → 再 unlink token」？注入失败点验证：写失败时 token 是否仍在（否则只能 reset）？
- [ ] 是否**没有**实现 30min 超时自杀？（实现了 → 云端「先起容器后配置」不可用，属功能性问题）
- [ ] 对外绑定 + 未初始化 + 无可用 token 时，是否**拒绝启动**而不是照常开端口？
- [ ] `VADE_SETUP_TOKEN` 注入后是否写回文件？文档是否标注了 env 的暴露面（`/proc/<pid>/environ`、`docker inspect`）？
- [ ] 非回环 + 空 `devices.json` 时 core 是否**拒绝启动**（而不是照常开端口）？
- [ ] 日志、异常、审计、manifest、`/admin/api/*`、截图里是否出现私钥/PSK/配对码/token？
- [ ] `Debug`/`repr`/`toString` 是否可能打印密钥？
- [ ] 模块密钥是否可能被镜像打包？（Dockerfile 是否 `COPY .` 把 `data/` 带进去）
- [ ] 前端是否把 token 放 `localStorage` / URL query？**setup token 也不能回填进 DOM 或留在 React state 里跨渲染**。
- [ ] 配对码是否只存 `argon2id(normalize(code))`、**180s** 过期、**消费即删**（`DELETE ... rowcount`，不是标记 used）？
- [ ] 配对码归一化（`NFKC` / 分隔符 / 大小写）在 core 与 SDK 两边是否**同一份实现**？两边不一致会导致「看着一样却握手失败」——试 `青松-A7K2M9` / 全角 / 带空格 / 小写四种输入
- [ ] 8 位码（2 汉字 + 6 位 base32）是否真的落到了所有入口：C2 `pairing.py`、C4 `--pair-code`、C5a 邀请位、C5b 二维码、U1 配对页？还有没有地方写死 6 位？

**验证**：`gitleaks detect`；对全部 API 响应跑正则 `private|secret|psk|password|token|credential` 与长 base64；`docker run --rm <image> find / -name "*.key"`。

**管理面隔离的复现方法**（必须真的做一遍，不要只看代码）：

```bash
# 1) 在 webui 页面控制台执行，应被 CORS 拦
fetch('https://127.0.0.1:8444/admin/api/nodes',{credentials:'include'}).then(r=>r.text()).then(console.log)

# 2) 伪造 Origin，应 403 ORIGIN_MISMATCH
curl -k -b '__Host-ac_admin=<valid>' -H 'Origin: http://127.0.0.1:8080' \
  https://127.0.0.1:8444/admin/api/nodes

# 3) 数据面不应有 admin 路由，应 404
curl -k https://127.0.0.1:8443/admin -o /dev/null -w '%{http_code}\n'
```

### 2. 密码学
- [ ] 是否自研了任何密码学原语/状态机？（有 → `critical`）
- [ ] 是否 `verify=False` / `InsecureSkipVerify` / `--insecure` 能关掉 TLS？（有 → `critical`）
- [ ] 版本不匹配是否可能降级协商？（FR-2.2 禁止）
- [ ] 重放：重发同一 counter 是否被拒？回退是否被拒？反射帧是否失败？
- [ ] rekey：`generation` 是否参与单调性判定？rekey 后旧 key 的帧是否被拒？rekey 失败是否**断连**而非回退旧 key？
- [ ] 对称密钥是否只在内存（`zeroize`、`PR_SET_DUMPABLE=0`），日志/dump 里是否可能出现？
- [ ] 密钥是否按方向分离？nonce 是否可能复用？
- [ ] 随机数是否用系统 CSPRNG？有没有用时间戳/`random` 播种？
- [ ] 指纹比较是否常量时间？（低风险，但记 `advisory`）
- [ ] 配对是否可能被中间人（XX 未校验 PSK / 未显示指纹）？

### 3. 认证与授权
- [ ] 白名单是否**默认拒绝**？未登记公钥的模块能否连上？未登记设备能否拿到写权限？
- [ ] 吊销是否立即断开已连接？下次握手是否失败？
- [ ] `browser_session` 是否真的没有私钥参与？cookie 是否 `__Host-` 前缀 + `Secure + HttpOnly + SameSite=Strict + Path=/` 且**无 `Domain`**？
- [ ] **管理面是否与模块 app 同源**？（同源 = `critical`：`/app` 里是模块代码，XSS 可直接读 `/admin/api/*`，`HttpOnly` 挡不住）
- [ ] 管理面是否返回了任何 `Access-Control-Allow-*` 头？（有 → `critical`）
- [ ] `/admin/api/*` 是否逐请求校验 `Origin` 与 `Sec-Fetch-Site`？伪造 Origin 能否拿到数据？
- [ ] cookie 是**按 host 隔离**还是只靠「不同端口」？（cookie 不含 port，后者不构成隔离）
- [ ] 数据面 `:8443` 是否残留 `/admin` 路由？（应为 404）
- [ ] 审批：无人在线是否 fail-closed？超时是否拒绝？`session` scope 是否随会话结束失效？
- [ ] 模块能否通过任何 RPC 反向调用 core/其他模块的工具？（FR-2.7）
- [ ] `pooled` 实例故障是否被正确摘除？恶意实例能否冒充同 `module_id` 的另一实例？（需要私钥，确认无绕过）
- [ ] Admin：失败 5 次是否锁定（IP + 全局双计数）？CSRF 是否校验（**只对 browser 模式**）？是否独立 listener `:8444`（不挂数据面）？
- [ ] **浏览器会话是否真的只读**？拿 `mode=browser` 的会话发 `POST /admin/api/skills/install`，必须 `403 READ_ONLY_SESSION`。只在前后端都拦才有效，只在前端隐藏按钮 = `high`
- [ ] **`/admin/bind` 的五道校验是否都在**：票存在且未消费（同事务 `DELETE ... RETURNING`）、AAD 匹配、`ts` 偏差 ≤120s、设备签名有效、设备数 <5。少一道 = `critical`
- [ ] **P-256 是否拒 high-S**？（不拒 → 签名可延展，一次性 challenge 失效）
- [ ] **绑定票是否只存 hash**？日志/审计/响应体里能否搜到票明文？
- [ ] **设备表为空 + 非回环监听是否拒绝启动**？（不拒 = 半开状态）
- [ ] 管理面 `Host` 是否校验白名单（防 DNS rebinding）？HSTS 是否设置？
- [ ] 管理面 CSP 是否含 `frame-ancestors 'none'`？能否被 iframe 嵌入做点击劫持？

### 4. 输入与注入
- [ ] `fs`：`..`、绝对路径、软链、软链目录、根为软链、前缀欺骗（`/root-evil`）、null 字节、UNC 路径
- [ ] `shell`：env 名注入、env 值超长、cwd 逃逸、超时杀进程组、`sudo` 绕过（`/usr/bin/sudo`、`env sudo`）
- [ ] `web`：私网/回环/链路本地/云元数据、IPv6、十进制/八进制 IP、userinfo 欺骗、DNS rebinding、逐跳重定向、gzip bomb、`file://`
- [ ] `mcp-bridge`：上游工具名注入（`__`、超长、控制字符）、上游返回超大/畸形结果、上游凭据泄漏
- [ ] `memory`：scope 注入、超长文本、embedding 返回 NaN/维度不符
- [ ] **记忆注入**（AMEND-19）：把「忽略之前的指令」写进记忆后，下一轮是否原样注入且未标为数据？`lesson` 的 `when/do/pitfall/verify` 是否也可能包含指令式文本？注入位置是否在 system prompt 的**数据区**而非指令区？
- [ ] **skill 四段绕过**：`Pitfalls` 里塞大段指令、`Verification` 里塞 `curl | sh`，schema 是否只校验“节存在”而没校验内容？（内容审查不是 schema 的事，但必须确认没有把它当成“已审核”的依据）
- [ ] manifest：未知字段、超限工具数、`input_schema` 非 object、risk 未知、重名
- [ ] Admin：路径参数注入、SSE `Last-Event-ID` 注入、布局 `rev` 并发绕过

### 5. 资源与拒绝服务
- [ ] 单帧 1MB 上限是否在**分配内存前**校验？
- [ ] 慢消费者/不读的 UI 是否会导致 core 内存增长？（背压）
- [ ] 队列是否可被无限灌入？`client_msg_id` 是否能绕过幂等？
- [ ] 工具调用是否可无限循环？（max_steps）
- [ ] 审计/日志/blob 是否可被打满磁盘？有无上限与轮转？
- [ ] **自进化的放大器**（AMEND-19）：把 `nudge_interval` 调到下限 + 超长会话，`daily_token_budget` 是否真的硬停？还是只是记账？耗尽后是否真的降级为纯规则（而不是继续调、只记日志）？
- [ ] **consolidate 的 O(n²)**：staging 灌到 10 万条时，聚类是否仍然在限定时间内完成？是否会把 CPU 打满导致 core 心跳超时？
- [ ] **rollback 竞态**：`enabled=false` 与在途 `turn_review` 并发时，是否还有一次写入落库？（关闭不是即时生效，是“下一次触发不开始”）
- [ ] 50 连接 + 大 payload 下是否稳定（NFR-1）？

### 6. 日志与审计
- [ ] 高危工具参数在审计里是否脱敏？UI 展示完整参数是**允许**的（用户要看），但日志不行
- [ ] LLM prompt / 用户文本是否进了日志？（只允许 digest）
- [ ] 审计是否不可改（UPDATE/DELETE 触发器）？保留 30 天？
- [ ] 错误信息是否泄漏宿主路径、用户名、版本、内部 IP？

### 7. 供应链与部署
- [ ] 依赖是否锁定版本？`pip-audit` / `cargo audit` / `npm audit` 有无 high/critical？
- [ ] Docker 是否非 root 运行？是否 `COPY .` 带入密钥/`.env`？
- [ ] 镜像是否含编译工具链与源码（可减小攻击面）？
- [ ] core 端口是否只开 8443（数据面）+ 8444（管理面）？8444 对外时是否已配设备表 + 强密码（否则应拒绝启动）？
- [ ] 容器是否有 `read_only` / `no-new-privileges`（建议项）？

### 8. UI 专项
- [ ] 浏览器端是否真的没有长期私钥？
- [ ] iOS 是否用 Keychain 且关闭 iCloud 同步？
- [ ] **WebView 是否只加载内置资源**？release 包里搜 `loadUrl("http` 必须 0 处。加载远程 HTML + JS 桥 = `critical`（XSS 直接升级为本地签名能力）
- [ ] **桥接方法是否恰好三个**（`getDeviceInfo` / `sign` / `secureStore`）？有无反射入口（`getClass()`、`addJavascriptInterface` 传任意对象）？
- [ ] **`sign()` 是否校验 payload 模板**？从 JS 传 `"hello"` 能否被签出来？能 = `critical`
- [ ] **`onReceivedSslError` 是否只有 `cancel()`**？搜 `proceed()` 必须 0 处
- [ ] **设备私钥是否不可导出**？`hardware_backed=true` 的设备上，私钥能否被 `exportKey` 一类接口取出？
- [ ] **`hardware_backed=false` 是否在列表里可见标注**？（悄悄混进硬件设备里 = `medium`）
- [ ] **指纹核对是否不可跳过**？有无「记住上次选择」导致第二次自动确认？
- [ ] **`__AC_HOST__.mode` 是否由原生注入且 JS 不可改写**？`Object.freeze` 或 `Object.defineProperty(writable:false)`
- [ ] **页面里是否有 `innerHTML` 插值 API 数据**？（Bearer 模式下 XSS 能读 token，这条是承重墙）
- [ ] 指纹变更是否阻断而非「忽略一次」？
- [ ] 审批弹窗是否显示完整参数（不能只显示摘要，否则用户批准了没看清的东西）？
- [ ] 截图/录屏里是否出现真实地址、token、指纹以外的凭据？

## 三、判定与严重度

| 级别 | 含义 | 处理 |
|---|---|---|
| `critical` | 可导致私钥泄漏、远程命令执行、越权访问、明文上网 | 立即阻断，修完重审 |
| `blocking` | 可导致拒绝服务、审计缺失、审批绕过 | 阻断 |
| `advisory` | 加固建议，不改变安全边界 | 登记 |

**任何 `critical` 直接 `reject`，不看其他项。**

## 四、输出格式

```json
{
  "batch": "<BATCH>",
  "reviewer": "R3",
  "verdict": "pass|reject",
  "critical": [],
  "blocking": [
    {"file": "modules/web/src/web_module/ssrf.py", "line": 31,
     "issue": "只校验首跳 URL，302 到 127.0.0.1 可绕过",
     "attack": "起本地 302 服务 → fetch http://evil/redirect → 返回内网内容",
     "fix": "每一跳重跑 validate_url()"}
  ],
  "advisory": [],
  "checks_run": ["gitleaks", "pytest test_ssrf", "tcpdump+grep", "pip-audit"]
}
```

## 五、禁止

- 禁止用「内网环境，不会被攻击」作为豁免理由。
- 禁止只做静态阅读——每个 `critical`/`blocking` 必须有复现步骤或明确的代码路径。
- 禁止把「已用 TLS」当作应用层加密的替代。
- 禁止修改代码；你只出结论与复现。
