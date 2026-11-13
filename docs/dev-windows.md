# Windows 开发环境说明

本仓库在 Linux CI 上是权威验证环境；这份文档只记录在 **Windows 开发机** 上跑通开发循环所需的额外信息。两者行为差异见「平台差异」一节。

## 1. 工具链

三项原生工具链都由 `D:/Hermes/tools` 提供，不装进系统 PATH，避免污染：

| 组件 | 版本 / 位置 |
| --- | --- |
| Rust | 1.100，`x86_64-pc-windows-gnu`（rustup / cargo 在 `D:/Hermes/tools/rust`） |
| MinGW-w64 | gcc 16.2，UCRT，`D:/Hermes/tools/mingw/mingw64` |
| JDK | Temurin 21，`D:/Hermes/tools/jdk21` |
| Gradle | 8.10.2，`D:/Hermes/tools/gradle-8.10.2`（`GRADLE_USER_HOME` 也指向 D:，别塞满 C:） |
| Android SDK | 36，`D:/Hermes/tools/android-sdk` |

每个 shell 会话先启用环境（不是必须，但它保证 cargo / gcc / java / gradle / adb 都在 PATH 上）：

```bash
source D:/Hermes/tools/env/vade-env.sh
```

代理：仓库直连 443 不通，访问 GitHub / crates.io / Maven 前必须开代理。

```bash
vade_proxy on     # 导出 http_proxy/https_proxy = http://127.0.0.1:7897
vade_proxy off    # 清掉
```

## 2. 重建协议原生库

Python 侧的 `vade_protocol` 绑定是 Rust cdylib。Windows 产物是 **`protocol/core/python/vade_protocol.dll`**，已被 `.gitignore` 忽略（与 Linux 的 `*.so` 同理），不入库：

```bash
bash D:/Hermes/tools/env/build-protocol-dll.sh
```

脚本会 `cargo build --locked --features uniffi --lib`，把 dll 复制到 `protocol/core/python/`，并做一次 `import vade_protocol` 冒烟。

注意：脚本内部 `cd` 到的是主仓 `D:/HermesWorkspace/Vade/Vade/protocol/core`。在 **worktree** 里开发时，构建完成后需要把 dll 复制到该 worktree 的 `protocol/core/python/`：

```bash
cp -f D:/HermesWorkspace/Vade/Vade/protocol/core/python/vade_protocol.dll \
      D:/HermesWorkspace/Vade-wt/<worktree>/protocol/core/python/
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

期望结果：全量测试 `378 passed`，契约检查 `contracts: OK`。

## 4. 平台差异

Windows 文件系统没有 POSIX 权限位语义：`chmod` 基本是 no-op，`stat().st_mode` 反映不出 0600 / 0700。因此以下两处断言在 Windows 上被 `os.name != "nt"` 守卫跳过，**Linux CI 上仍然照常校验**，不放宽标准：

- `core/tests/admin/test_passwords.py` —— admin 文档落盘后必须是 `0600`。
- `core/tests/test_skill/test_install.py` —— `pending/` 目录必须是 `0700`。

同一测试里的其他断言（argon2 重哈希、二次登录、`password_rehashed_at`、安装文件不可执行位等）在 Windows 上照常执行，没有整测试 skip。

含义：**Windows 上不强制 0600 / 0700**。安全相关的权限收紧只能在 Linux 上验证，改动这类逻辑时以 CI 为准。

## 5. 其他坑

- PATH 必须是 MSYS 形式（`/d/Hermes/...`）。`vade-env.sh` 已处理；手工追加路径时用 `/d/...`，不要写 `D:\...`，否则 bash 找不到。
- 原生程序（git / cargo / python）的参数不做 MSYS 路径转换：给它们传路径时用 `D:/...` 正斜杠形式，别传 `/d/...`。
- `python` 默认指向 Hermes 自带解释器，请始终显式使用仓库 venv 的可执行文件（见上）。
- 临时文件写在 `$TMPDIR`（Hermes 的 scratch 目录）下，别用系统 temp。
