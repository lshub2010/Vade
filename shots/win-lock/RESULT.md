# Windows flock 修复 — locks.py 平台分派

分支：`tmp/win-fix2` @ `9962745`（基于 origin/main = 123faa8）
Worktree：`D:/HermesWorkspace/Vade-wt/win-fix2`
改动文件（`git diff --stat` 原始输出）：

```
 core/tests/admin/test_admin_hardening.py | 66 ++++++++++++++++++++++++++++++++
 core/vade/admin/locks.py                 | 35 +++++++++++++++--
 2 files changed, 97 insertions(+), 4 deletions(-)
```

## 1. Before / After 代码

### Before（`core/vade/admin/locks.py`，2028 字节，原第 16 行）

```python
from __future__ import annotations

import fcntl
import os
from pathlib import Path

__all__ = ["acquire_instance_lock", "release_instance_lock", "lock_path"]
```

```python
def acquire_instance_lock(data_dir: Path) -> int:
    ...
    fd = os.open(lock_path(directory), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise
    ...

def release_instance_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
```

### After（关键部分）

```python
import os
import sys
from pathlib import Path

__all__ = ["acquire_instance_lock", "release_instance_lock", "lock_path"]

_IS_WINDOWS = os.name == "nt"  # matches the convention in the test suite
```

平台分派（`import fcntl` / `import msvcrt` 都移进各自分支内，顶层不再有 fcntl）：

```python
    fd = os.open(lock_path(directory), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        if _IS_WINDOWS:
            import msvcrt

            # msvcrt.locking needs at least one byte in the file to lock.
            os.lseek(fd, 0, os.SEEK_SET)
            if os.fstat(fd).st_size < 1:
                os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise
```

```python
def release_instance_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        if _IS_WINDOWS:
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
```

注意：最终提交版 `locks.py` 顶部只 import `os` / `pathlib`；平台开关 `_IS_WINDOWS = os.name == "nt"`（仓库测试套件已有 `@pytest.mark.skipif(os.name == "nt", ...)`，`test_password_migration.py:252`，跟这个风格）。

## 2. 四条新测试（core/tests/admin/test_admin_hardening.py）

按现有 flock 用例风格追加在 §5 之后（§5a），全部写成平台无关契约、在执行它的真实 OS 上实测：

1. `test_second_acquire_of_the_same_data_dir_fails_on_this_platform` — 同目录 acquire 两次，第二次必须抛 `OSError`。
2. `test_release_instance_lock_hands_the_directory_back` — release 后能再次 acquire；`release(None)` 是文档化 no-op。
3. `test_distinct_data_dirs_lock_independently` — alpha/beta 两个目录各自 acquire 都成功。
4. `test_platform_branch_takes_a_real_os_lock` — 断言 `locks._IS_WINDOWS == (os.name == "nt")`，并按分支用裸 OS 原语实测锁真的生效：Windows 上第二个句柄 `msvcrt.locking(LK_NBLCK)` 抛 `OSError`，POSIX 上第二个 fd `flock(LOCK_EX|LOCK_NB)` 抛 `OSError`。

**实现方式说明（任务要求里问的「同进程双句柄」）**：实测证明 Windows 上同进程内第二次 `open` 同一文件再 `msvcrt.locking` **确实失败**（`PermissionError [Errno 13]`，见下面 probe 原始输出），与 POSIX flock 的 per-ofd 语义不同但互斥结果一致，所以四条测试全部用同进程写法即可成立，无需 subprocess 版本（原有的跨进程用例 `test_second_instance_cannot_take_the_data_directory` 继续覆盖跨进程路径，也通过）。

## 3. 原始输出

### 3a. Before 行为（主仓旧代码，模块 import 即死）

```
$ python -c "import sys; sys.path.insert(0, 'D:/HermesWorkspace/Vade/Vade/core'); import vade.admin.locks"
Traceback (most recent call last):
  File "<string>", line 1, in <module>
  File "D:\HermesWorkspace\Vade\Vade\core\vade\admin\locks.py", line 16, in <module>
    import fcntl
ModuleNotFoundError: No module named 'fcntl'
```

### 3b. Windows 行为 probe（写测试前先实测关键假设）

```
$ python D:/Hermes/cache/scratch/probe_lock.py
acquire#1 ok fd= 3
RESULT-DOUBLE: OSError [Errno 13] Permission denied
RESULT-2ND-HANDLE: OSError [Errno 13] Permission denied
released ok
re-acquire ok fd= 3
different dirs ok 3 4
release(None) ok
Traceback (most recent call last):
  File "D:\Hermes\cache\scratch\probe_lock.py", line 50, in <module>
    print("core.lock content:", repr((d1 / "core.lock").read_bytes()))
PermissionError: [Errno 13] Permission denied
```

（最后这个 traceback 是 probe 脚本自己用第三方句柄读**被锁文件**被 Windows 拒绝——这是 byte-range 锁的正确行为：锁着的字节连读都拒绝。生产路径无任何代码读这个文件（`cli.py:66` 用 `acquire_instance_lock` 探测，不用读），只有 `cat data/core.lock` 诊断在 core 运行时不可用，POSIX 上可用。锁内 `ftruncate(0)+write(pid)` 诊断块已单独 probe：在 NTFS 上工作正常，锁不失效：）

```
$ python D:/Hermes/cache/scratch/probe_ftruncate.py
locked byte 0 ok
ftruncate+write while locked: OK; size = 6
unlocked+closed ok, size = 6 content = b'12345\n'
```

### 3c. 测试全文件（验收命令原样）

第一次全文件运行（commit 后的最终代码，复跑验证）：

```
$ cd D:/HermesWorkspace/Vade-wt/win-fix2
$ export PYTHONPATH="D:/HermesWorkspace/Vade-wt/win-fix2/core;D:/HermesWorkspace/Vade-wt/win-fix2/protocol/core/python"
$ D:/HermesWorkspace/Vade/Vade/.venv/Scripts/python.exe -m pytest core/tests/admin/test_admin_hardening.py -q
.F..F..F.........                                                        [100%]
=========================== short test summary info ===========================
FAILED core/tests/admin/test_admin_hardening.py::test_sessions_file_stores_only_the_digest_and_is_0600
FAILED core/tests/admin/test_admin_hardening.py::test_browser_cookie_without_csrf_is_refused_on_every_non_get
FAILED core/tests/admin/test_admin_hardening.py::test_remote_setup_unlock_failure_leaves_an_audit_row
3 failed, 14 passed in 3.08s
```

第二次全文件运行（去掉未使用的 `import sys` 之后的最终代码）：

```
core\tests\admin\test_admin_hardening.py:117: AssertionError
=========================== short test summary info ===========================
FAILED core/tests/admin/test_admin_hardening.py::test_refresh_survives_a_core_restart_and_access_does_not
FAILED core/tests/admin/test_admin_hardening.py::test_sessions_file_stores_only_the_digest_and_is_0600
2 failed, 15 passed in 2.65s
```

两次运行的失败集合**不同**且都是存量 flaky，根因是 O_BINARY bug（见 §5）：`pairing.pepper` 少了 O_BINARY 时，随机 pepper 中只要含 `\n`/`\r` 字节文件就短于 32 字节（概率 1−(254/256)³²≈22.3%），受影响测试逐次独立 flaky。第二次运行的 `test_refresh_survives_a_core_restart_and_access_does_not` 失败同样是 pepper bug（其 restart 路径重读 pepper）——单独运行它则通过：

```
$ ... -m pytest "core/tests/admin/test_admin_hardening.py::test_refresh_survives_a_core_restart_and_access_does_not" -q
.                                                                        [100%]
1 passed in 0.33s
```

**与锁相关的用例两次全文件运行都是 7/7 通过**。点名复跑（原样输出）：

```
$ ... -m pytest core/tests/admin/test_admin_hardening.py -q -k "lock or instance"
.......                                                                  [100%]
7 passed, 10 deselected in 0.45s
```

命中：`test_second_instance_cannot_take_the_data_directory`、`test_second_acquire_of_the_same_data_dir_fails_on_this_platform`、`test_release_instance_lock_hands_the_directory_back`、`test_distinct_data_dirs_lock_independently`、`test_platform_branch_takes_a_real_os_lock`、`test_cli_write_commands_refuse_while_the_lock_is_held`、`test_serve_refuses_to_start_a_second_core_on_the_same_data_dir`。

### 3d. core 真实启动（临时 toml，127.0.0.1:18443）

第一次（commit 前的代码，行为与最终版相同）：

```
$ timeout 20 python -m vade --config D:/Hermes/cache/scratch/vade-win-test.toml --no-pair-banner
{"ts":"2026-11-28T08:05:03.716Z","level":"info","logger":"vade.app","msg":"core identity loaded"}
INFO:     Started server process [20504]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:18443 (Press CTRL+C to quit)
INFO:     Uvicorn running on http://127.0.0.1:59032 (Press CTRL+C to quit)
vade: listeners up — data ws://127.0.0.1:18443 admin http://127.0.0.1:59032
setup_token=acs_gb-q… setup_path=D:\Hermes\cache\scratch\vade-core-data\setup.token
vade: first-run admin setup — open http://127.0.0.1:59032/admin/setup. ...
EXIT=124
```

第二次（去掉 `import sys` 之后的最终提交版代码）：

```
{"ts":"2026-11-28T08:10:14.995Z","level":"info","logger":"vade.app","msg":"core identity loaded"}
INFO:     Started server process [2008]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:18443 (Press CTRL+C to quit)
INFO:     Uvicorn running on http://127.0.0.1:49278 (Press CTRL+C to quit)
vade: listeners up — data ws://127.0.0.1:18443 admin http://127.0.0.1:49278
setup_token=acs_gb-q… setup_path=D:\Hermes\cache\scratch\vade-core-data\setup.token
vade: first-run admin setup — open http://127.0.0.1:49278/admin/setup. ...
BOOT_EXIT=124
```

EXIT=124 是 `timeout 20` 到点杀掉正常运行的 server；`ModuleNotFoundError: No module named 'fcntl'` **不再出现**。两次启动都没出现 `pairing pepper must be 32 bytes`：pepper 的 O_BINARY bug 是**概率性**的（随机 pepper 含 `\n`/`\r` 才被破坏，约 22.3% 概率），这两次碰巧抽到干净的 pepper；flaky 的测试证据见 §3c——该 bug 属于另一个子代理的 O_BINARY 修复范围，与本修复无关。

## 4. 「POSIX 行为未变」论证

POSIX 分支的锁逻辑**逐字符未改**：`os.open(...)` 的 flags 只多了 `getattr(os, "O_BINARY", 0)`，在 POSIX 上该常量不存在、`getattr` 返回 `0`，`os.open(path, flags | 0, mode)` 与原来完全等价；`fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)` 与 `fcntl.flock(fd, fcntl.LOCK_UN)` 一字未动，诊断块（`ftruncate`/`write(pid)`/`fsync`）一字未动，异常路径（`except OSError: os.close(fd); raise`）一字未动。唯一语义差别：`import fcntl` 从模块顶层移到分支内——它在 POSIX 分支里**必然成功**，所以失败模式不变（POSIX 上不可能走到 `import msvcrt` 分支）。

不光是论证，还在真 Linux 上执行了该分支验证。用 WSL2 Ubuntu-26.04，直接按文件路径加载 worktree 里的 locks.py（文件本身无包内依赖，绕开 WSL python 没有 starlette 的问题）：

```
$ wsl python3 /mnt/d/Hermes/cache/scratch/posix_lock_check.py
branch: POSIX fcntl.flock, _IS_WINDOWS = False
acquire#1 ok fd = 3
DOUBLE-ACQUIRE: OSError -> [Errno 11] Resource temporarily unavailable
CROSS-PROCESS second acquire: rc = 7 (7 = refused, expected)
released ok
re-acquire after release ok
distinct dirs ok: 3 4 paths differ: True
release(None) ok
POSIX-CHECK-DONE
WSL_EXIT=0
```

`[Errno 11] EAGAIN` 正是 flock 非阻塞冲突的原始 errno，与改前行为一致（改前代码与改后 POSIX 分支在这条路径上字节相同）。

## 5. 未由本修复负责的存量失败（如实区分）

1. `test_sessions_file_stores_only_the_digest_and_is_0600` — 两次全文件运行都失败：`assert stat.S_IMODE(...) == 0o600`，Windows 返回 0o666(438)。任务说明明确 0600/0700 chmod 批次不属本次范围。
2. `test_browser_cookie_without_csrf_is_refused_on_every_non_get`、`test_remote_setup_unlock_failure_leaves_an_audit_row`（第一次运行）、`test_refresh_survives_a_core_restart_and_access_does_not`（第二次运行，单独跑通过）— `vade.errors.VadeError: INTERNAL: pairing pepper pairing.pepper must be 32 bytes`，出处在 `core/vade/identity/pairing.py:195`。根因是 O_BINARY 缺失：pepper 写入时 `\n`(0x0A)→`\r\n`(0x0D 0x0A)，文件变 33 字节或含 `\r` 被当作 32 字节里的坏字节，**只有随机 pepper 恰好含 `\n` 时才触发**（≈22.3%），所以逐次运行 flaky、失败集合漂移。这正是另一个子代理正在修的 O_BINARY bug，与本修复无关（本修复的锁路径不碰 pepper）。
3. Pyright（Windows 解析）报 `fcntl.flock/LOCK_EX/LOCK_UN` 不是模块属性——Windows pyright 对 POSIX stub 的反向误报（分支在 Windows 上不可达）；CI（`.github/workflows/ci.yml`）不含 pyright/ruff/mypy，不阻塞。

## 6. 与并行子代理的冲突标记

我改了 `locks.py` 的 `os.open` 行，**加了 `getattr(os, "O_BINARY", 0)`**：

```python
fd = os.open(lock_path(directory), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
```

如果全仓 O_BINARY 修复的子代理也要动这行，以本提交（`9962745`）为准，请它跳过这一行。
