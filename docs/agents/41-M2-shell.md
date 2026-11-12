# M2 · `shell` 命令模块

| 项 | 值 |
|---|---|
| Agent 代号 | `m2-shell` |
| 波次 | W5 |
| 依赖 | C4 的 SDK v0.1 |
| 目录所有权 | `modules/shell/` |
| 出口 | 五道门 + high risk 审批联动 + 超时杀进程组 |

## 一、一句话任务

给模型执行命令的能力，但每一次都必须有人批准；超时要连子进程一起杀掉，环境变量不能把宿主的密钥带出去。

## 二、必读

- `docs/design.md` §15.2（shell 规格）、§6（审批）
- `docs/requirements.md` FR-7、A16、AC-5/AC-16
- `sdk/PYTHON_SDK.md`
- `docs/agents/01-contracts.md` §二

## 三、目录

```
modules/shell/
├── manifest.json
├── src/shell_module/
│   ├── tools.py        # 纯函数，零 SDK import
│   ├── config.py       # cwd 白名单、env 白名单、上限
│   └── server.py
├── tests/{test_tools.py,test_security.py}
├── Dockerfile
└── README.md
```

## 四、工具规格

| 工具 | 参数 | risk | timeout |
|---|---|---|---|
| `exec` | `cmd: str, cwd: str = "", timeout_ms: int = 30000, env: dict[str,str] \| None = None` | **high** | 受参数约束，上限 300s |

- `risk=high` 写进 manifest；**审批由 core 的 policy 做**，模块不自己弹窗、不自己放行。
- manifest 里 `idempotent: false`（同一命令跑两次结果可能不同）。

## 五、安全实现

### 执行

```python
proc = await asyncio.create_subprocess_shell(
    cmd,
    cwd=str(cwd_path),
    env=build_env(extra_env),
    stdin=asyncio.subprocess.DEVNULL,
    stdout=PIPE, stderr=PIPE,
    start_new_session=True,          # 关键：独立进程组，便于整组杀
)
```

超时：

```python
try:
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_ms/1000)
except TimeoutError:
    os.killpg(proc.pid, signal.SIGKILL)      # 杀整组，不是只杀 sh
    out, err = await proc.communicate()      # 回收，避免僵尸
    return ToolError("TIMEOUT", f"killed after {timeout_ms}ms", retriable=False)
```

**必须验证**：`sh -c 'sleep 100 & wait'` 超时后，`sleep 100` 也被杀（用 `pgrep -f "sleep 100"` 断言 0）。

### cwd

- 默认 `cwd` = 配置的第一条根；给了就校验（复用与 fs 相同的 `resolve + parents` 逻辑，但**在本模块内复制一份**，不 import fs）。
- 越界 → `PERMISSION_DENIED`。

### 环境变量

```python
BASE = {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ", "TMPDIR"}
DENY_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|COOKIE|SESSION)", re.I)

def build_env(extra):
    env = {k: os.environ[k] for k in BASE if k in os.environ}
    for k, v in (extra or {}).items():
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", k):
            raise ToolError("INVALID_ARGUMENT", f"bad env name: {k}")
        if DENY_RE.search(k):
            raise ToolError("PERMISSION_DENIED", f"env var not allowed: {k}")
        env[k] = str(v)[:4096]
    return env
```

`os.environ` 里的其他变量（AWS_*、OPENAI_API_KEY、SSH_AUTH_SOCK…）**一个都不继承**。

### 命令白名单（主防线）

`SHELL_ALLOWLIST`：逗号分隔的可执行名列表，**默认空 = 全部拒绝**（不是全部允许）。匹配规则：

- 只匹配 `argv[0]` 的 basename，不做路径前缀匹配（`/tmp/ls` 与 `ls` 同等对待）。
- 支持通配：`git*` 匹配 `git` / `git-lfs`，`*` 仅此一处语义，不做正则。
- 拒绝 shell 元字符（`;` `|` `&` `$(` `` ` ``）——**除非**显式用 `sh -c`，而 `sh` 本身必须进白名单且 `risk=high`。
- 不解析复合命令；看到元字符就拒，不尝试"理解"。

没有白名单、只有黑名单的话，默认是放行——这与「默认拒」的整体安全姿态相反（F1 的部署契约按 allowlist 写，两边必须一致）。

### 命令黑名单（辅助，不是主防线）

拒绝首 token 为 `sudo` / `su` / `doas` / `pkexec` 的命令；拒绝包含 `>/dev/sd`、`mkfs`、`dd of=/dev/` 的模式。这些只是减少误操作，**真正的门是审批**——README 里必须写清这一点。

### 输出

- stdout / stderr 各上限 **64KB**；超限截断并 `truncated: true`，同时**继续读空管道**，防止子进程因管道满而卡死。
- 返回 `{exit_code, stdout, stderr, truncated, duration_ms}`；`exit_code != 0` 不算工具错误（`ok: true`），让模型自己判断。
- 命令长度上限 8KB。

## 六、验收（五道门 + 安全）

```bash
cd modules/shell

python -m shell_module --standalone exec --args '{"cmd":"echo hi"}'

# 超时杀组（必须 1.5s 内返回，且无残留）
python -m shell_module --standalone exec --args '{"cmd":"sh -c \"sleep 100 & wait\"","timeout_ms":800}'
pgrep -f "sleep 100" && echo "LEAK" || echo "OK"

# 环境不泄漏
FAKE_SECRET=abc python -m shell_module --standalone exec --args '{"cmd":"env"}'
# 输出中不得出现 FAKE_SECRET

# 拒绝 sudo
python -m shell_module --standalone exec --args '{"cmd":"sudo id"}'

# 越界 cwd
python -m shell_module --standalone exec --args '{"cmd":"pwd","cwd":"/etc"}'

# 大输出不卡死
python -m shell_module --standalone exec --args '{"cmd":"yes | head -c 10000000"}'
```

`test_security.py` 必测：超时杀组（含孙子进程）、子进程自己 daemon 化后仍被杀（尽力而为，记录已知边界）、env 白名单、env 名注入（`"A;rm -rf /"`）、cwd 逃逸、命令超长、10MB 输出截断、审批联动（用 core e2e：high risk 必挂起）。

## 七、禁止

- 禁止 `shell=False` 拆分参数（工具语义就是 shell）。
- 禁止只 `proc.kill()` 不杀进程组。
- 禁止继承全量 `os.environ`。
- 禁止模块自己实现审批/放行。
- 禁止把 stdout/stderr 全量写日志（只记长度与 digest）。
- 禁止在 `tools.py` import SDK。

## 八、交付格式

按 §三，额外附：超时杀组的 `ps` 前后对比、env 泄漏测试原始输出、一次 core 端到端审批截图、Docker 内运行日志。
