# Windows 开发环境说明

本仓库目前**没有 CI 配置**，Linux 侧的测试与契约检查由人工在 Linux 验证环境执行；这份文档只记录在 **Windows 开发机** 上跑通开发循环所需的额外信息。两边的行为差异见「平台差异」一节。

## 1. 工具链

下表的五项原生工具链都由 `D:/Hermes/tools` 提供，不装进系统 PATH，避免污染：

| 组件 | 版本 / 位置 |
| --- | --- |
| Rust | 1.100，`x86_64-pc-windows-gnu`（cargo / rustc 在 `D:/Hermes/tools/rust/cargo/bin`，rustup 本体在 `D:/Hermes/tools/rust/rustup`） |
| MinGW-w64 | gcc 16.2，UCRT，`D:/Hermes/tools/mingw/mingw64` |
| JDK | Temurin 21，`D:/Hermes/tools/jdk21` |
| Gradle | 8.10.2，`D:/Hermes/tools/gradle-8.10.2`（`GRADLE_USER_HOME` 也指向 D:，别塞满 C:） |
| Android SDK | 36，`D:/Hermes/tools/android-sdk` |

每个 shell 会话先启用环境（不是必须，但它保证 cargo / gcc / java / gradle / adb 都在 PATH 上）：

```bash
source D:/Hermes/tools/env/vade-env.sh
```

网络：访问 GitHub / crates.io / Maven 不稳定，直连与代理都会时通时断——同一台机器上可能这一次直连就成功，下一次却 443 被 reset、或 20 秒连不上。所以代理是**备选而非前提**：拉依赖或 `git fetch` 失败时先开代理重试；若仍失败，就关掉代理退回直连再试一次。两者互为备选，没有哪一方「必须」。

```bash
vade_proxy on     # 导出 http_proxy/https_proxy = http://127.0.0.1:7897
vade_proxy off    # 清掉，退回直连
```

## 2. 重建协议原生库

Python 侧的 `vade_protocol` 绑定是 Rust cdylib。Windows 产物是 **`protocol/core/python/vade_protocol.dll`**，已被 `.gitignore` 忽略（与 Linux 的 `*.so` 同理），不入库：

```bash
bash D:/Hermes/tools/env/build-protocol-dll.sh
```

脚本会 `cargo build --locked --features uniffi --lib`，把 dll 复制到 `protocol/core/python/`，并做一次 `import vade_protocol` 冒烟。

注意：脚本内部 `cd` 到的是主仓 `D:/HermesWorkspace/Vade/Vade/protocol/core`。在 **worktree** 里开发时，构建完成后需要把 dll 复制到该 worktree 的 `protocol/core/python/`（`WT` 换成你在用的 worktree；下面这条指向当前存在的 worktree，可直接执行）：

```bash
WT=D:/HermesWorkspace/Vade-wt/c0c-miuix
cp -f D:/HermesWorkspace/Vade/Vade/protocol/core/python/vade_protocol.dll \
      "$WT/protocol/core/python/"
```

缺这个 dll 时，涉及原生协议层的用例会成批失败（约 22 个），不是代码问题。

## 3. 常用命令

Python 用仓库自己的 venv（3.12，依赖已装）。`python` 默认解析到 Hermes 自带的解释器，**不要用它**跑测试：

```bash
# 全量测试
D:/HermesWorkspace/Vade/Vade/.venv/Scripts/python.exe -m pytest core/tests -q

# 契约门禁
D:/HermesWorkspace/Vade/Vade/.venv/Scripts/python.exe tools/check_contracts.py

# 文档包一致性
D:/HermesWorkspace/Vade/Vade/.venv/Scripts/python.exe docs/check_docs.py
```

期望结果：全量测试 `1632 passed, 7 failed, 1 skipped`。那 7 个失败**全部是 Windows 平台限制，不是代码问题**（详见 §4）：6 个是 `0600`/`0700` 权限位断言（Windows 无 POSIX 权限位语义），1 个是 `test_python_m_vade_admin_listener_stops_cleanly_on_sigterm`（Windows 没有真正的 SIGTERM，进程被 `TerminateProcess` 硬杀，返回码非 0）。在 Linux 上这些全部通过。

契约检查应为 `contracts: OK`。

## 4. 平台差异

Windows 文件系统没有 POSIX 权限位语义：`chmod` 基本是 no-op，`stat().st_mode` 反映不出 0600 / 0700。因此以下两处断言在 Windows 上被 `os.name != "nt"` 守卫跳过；在 Linux 上跑同一套测试时照常校验（目前由人工在 Linux 验证环境执行，仓库尚无 CI 配置），标准没有放宽：

- `core/tests/admin/test_passwords.py` —— admin 文档落盘后必须是 `0600`。
- `core/tests/test_skill/test_install.py` —— `pending/` 目录必须是 `0700`。

这两条之外的其他断言（argon2 重哈希、二次登录、`password_rehashed_at` 等）在 Windows 上照常执行，没有整测试 skip。但有一个例外要说清：`core/tests/test_skill/test_install.py` 里那个按 `& 0o111 == 0` 判断「安装文件不可执行」的循环，在 Windows 上**照样会跑，却恒真（vacuous）**——`st_mode` 不带权限位，循环体一次都不会失败。也就是说它在 Windows 上是空校验，只在 POSIX 系统上才有判别力，别把「Windows 上绿灯」当成这项检查也通过了。

含义：**Windows 上不强制 0600 / 0700，也验不出可执行位**。安全相关的权限收紧只能在 Linux 上验证；改动这类逻辑时，需在 Linux 环境手动复跑 `core/tests/admin/test_passwords.py` 与 `core/tests/test_skill/test_install.py` 确认。

## 5. 其他坑

- PATH 必须是 MSYS 形式（`/d/Hermes/...`）。`vade-env.sh` 已处理；手工追加路径时用 `/d/...`，不要写 `D:\...`，否则 bash 找不到。
- 原生程序（git / cargo / python）的参数不做 MSYS 路径转换：给它们传路径时用 `D:/...` 正斜杠形式，别传 `/d/...`。
- **`--config /d/...` 会被静默忽略**（这条最容易误判成 bug）。`python -m vade --config /d/Hermes/.../c.toml` 里，`/d/...` 会被 Python 当成 `D:\d\Hermes\...` 这个不存在的路径，`load_config` 发现文件不存在就**静默回落到默认值**（8443/8444、`./data`），不报错。症状是「我改了配置但端口/data_dir 没变」。用 `D:/Hermes/...` 就正常。判断方法：`setup_path` 那行会显示实际用的 data 目录。
- `python` 默认指向 Hermes 自带解释器，请始终显式使用仓库 venv 的可执行文件（见上）。
- 临时文件写到 `D:/Hermes/cache/scratch`（Hermes 的 scratch 目录）。注意 `$TMPDIR` 在这里是 `/tmp`，实际映射到系统 temp（`C:/Users/<用户名>/AppData/Local/Temp`），并不是 Hermes 的 scratch 目录。

## 6. Windows 上写二进制文件必须显式二进制模式

`os.open()` 在 Windows 上**默认是文本模式**，会把写入数据里的每个 LF 字节（0x0A）自动改写成 CR+LF 两个字节（0x0D 0x0A）。POSIX 没有这个概念，所以这个坑只在 Windows 上出现，且**概率性发作**——只有当随机字节里恰好含 LF 时才坏。

实测（写 32 字节随机 pepper，500 次）：**51 次被写成 33/34/35 字节**，规律是「payload 里有几个 LF 就多几个字节」。后果是 `pairing pepper must be 32 bytes` 报错，core 约 17% 概率启动失败，而且每次失败的用例都不同，看起来像 flaky 测试。

两条规则：

1. **任何写入型 `os.open` 都要带二进制标志**。`os.O_BINARY` 在 POSIX 上不存在，所以用 `getattr(os, "O_BINARY", 0)`（POSIX 上为 0，按位或不改变任何行为）。
2. **`O_BINARY` 管不住 `os.fdopen(fd, "w")`**。这是第二层：`TextIOWrapper` 有自己的换行翻译，即使 fd 是二进制的，`fdopen(fd, "w")` 仍会把 LF 写成 CR+LF。文本包装必须显式 `newline="\n"`。实测四种组合确认过。

另外，`os.write()` 可能短写（返回值小于 `len(data)`），写入路径应循环写完再返回，不能假设一次写完。
