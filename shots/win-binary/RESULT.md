# win-binary：Windows 文本模式 fd 破坏二进制写入的系统性修复

分支：`tmp/win-binary`（基于 `origin/main` @ `123faa8`），修复提交：`ac4b724`。
范围：12 处写入点（core 9 + sdk 2 源文件 + 测试 2 个新文件）；`locks.py` 未动（另一棒负责）。

## 1. 逐处 before/after

每个改动模块顶部新增（内容一致，注释按模块语境微调）：

```python
# Windows fds default to text mode and would translate every LF byte into CRLF,
# corrupting binary payloads. POSIX has no O_BINARY; OR-ing 0 is a no-op there.
_O_BINARY = getattr(os, "O_BINARY", 0)
```

裸 `os.write` 的 6 个模块另加：

```python
def _write_all(fd: int, data: bytes) -> None:
    """`os.write()` until every byte is out; a short write must not truncate."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:  # pragma: no cover - regular files never do this
            raise OSError(f"write on fd {fd} made no progress, {len(view)} byte(s) left")
        view = view[written:]
```

### core/vade/admin/auth.py（sessions.json，裸 os.write）
```diff
-            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
+            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BINARY, 0o600)
             try:
-                os.write(fd, json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"))
+                _write_all(fd, json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"))
                 os.fsync(fd)
```

### core/vade/admin/devices.py（devices.json，裸 os.write）
```diff
-        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
+        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BINARY, 0o600)
         try:
-            os.write(fd, json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"))
+            _write_all(fd, json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"))
```

### core/vade/admin/identity.py（core_identity.key，裸 os.write，O_EXCL）
```diff
-    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
+    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
     try:
-        os.write(fd, raw)
+        _write_all(fd, raw)
```

### core/vade/admin/setup.py（admin.json 等，裸 os.write，静态方法 `_atomic_write`）
```diff
-        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
+        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BINARY, 0o600)
         try:
-            os.write(fd, raw)
+            _write_all(fd, raw)
```

### core/vade/admin/setup_token.py（setup.token，裸 os.write）
```diff
-    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
+    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BINARY, 0o600)
     try:
-        os.write(fd, token.encode("ascii"))
+        _write_all(fd, token.encode("ascii"))
```

### core/vade/identity/pairing.py（pairing.pepper，本次实测出问题的写入点，裸 os.write）
```diff
-            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
+            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
         else:
             try:
-                os.write(fd, secrets.token_bytes(PEPPER_BYTES))
+                _write_all(fd, secrets.token_bytes(PEPPER_BYTES))
```

### core/vade/identity/store.py（core.key，fdopen 文本包装）
```diff
-    fd = os.open(key_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
-    with os.fdopen(fd, "w", encoding="utf-8") as handle:
+    fd = os.open(key_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | _O_BINARY, 0o600)
+    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
```

### core/vade/identity/whitelist.py（whitelist.json，fdopen 文本包装）
```diff
-        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
+        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | _O_BINARY, 0o600)
         try:
-            with os.fdopen(fd, "w", encoding="utf-8") as handle:
+            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
```

### core/vade/skill/loader.py（技能商店安装文件，fdopen 二进制包装，O_EXCL）
```diff
-    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
+    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, mode)
```
`fdopen(fd, "wb")` 本身已是二进制，无需 newline 参数。

### sdk/python/vade_sdk/transport.py（module.key + core_pin.json，fdopen 文本包装 ×2）
```diff
-    fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
-    with os.fdopen(fd, "w", encoding="utf-8") as handle:
+    fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_EXCL | _O_BINARY, 0o600)
+    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
         handle.write(seed + "\n")
...
-    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
-    with os.fdopen(fd, "w", encoding="utf-8") as handle:
+    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | _O_BINARY, 0o600)
+    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
         handle.write(json.dumps(payload, indent=2) + "\n")
```

## 2. `os.write` 返回值方案与理由

**选择：`_write_all` 循环写完（memoryview 切片推进）**，不用 `os.fdopen` 重写。

理由：
- POSIX 规范 `write(2)` 允许短写；`os.write` 的返回值此前全部被丢弃，短写即静默截断——对 pepper（32 字节定长强校验）是启动失败，对 sessions.json/whitelist.json 是半截 JSON，重启后解析失败。
- `os.fdopen(fd, "wb")` + `f.write()` 也能兜住短写（io 层重试），但 `f.write` 在缓冲未满/未 flush 时可能把字节留在用户态缓冲区，调用方随后的 `os.fsync(fd)`（auth.py 就有）会 sync 到一个尚未落全部数据的 fd，需要改动 flush 时序才能保证等价——改动面反而更大。
- `_write_all` 就地放在各模块（auth/devices/identity/setup/setup_token/pairing 各一份，每份 9 行）。刻意**不**新建共享 util 模块：跨 `core/vade/admin`、`core/vade/identity` 的公共 import 会引入新的包间依赖，与任务「不要为这一个常量大规模重构」的约束一致。重复 6 份小函数是代价，换来零依赖图变化。
- `written <= 0` 抛 `OSError`：常规文件不会出现，防御管道/特殊 fd 的死循环。

## 3. 为什么 fdopen 文本包装处也补 `newline="\n"` + `_O_BINARY`（实证）

在 worktree 里直接实测（Windows 11，Python 3.12.14）：

```
no-O_BINARY text fdopen bytes: b'line1\r\nline2\r\n'
with-O_BINARY text fdopen bytes: b'line1\r\nline2\r\n'   ← O_BINARY 无效！
no-O_BINARY raw os.write bytes: b'\r\n' * 32             ← pairing 的 bug 本体
```

结论：`os.O_BINARY` 只关 **fd 层**的 CRT 翻译，管不住 `fdopen(..., "w")` 的 **io.TextIOWrapper** 层翻译——后者由 `newline=` 参数控制。因此：
- 文本包装 4 处（store/whitelist/transport×2）：`newline="\n"` 是有效修复（写入的字节本就是 LF，显式声明「不翻译」）；`_O_BINARY` 保留为纵深防御。
- 裸 `os.write` 6 处：`_O_BINARY` 是有效且必需的修复。
- 二进制包装 1 处（loader）：`"wb"` 本不翻译，`_O_BINARY` 是一致性 + 纵深。

这些位置写入的载荷（64 hex + LF、indent=2 JSON）含真实 LF 字节，修复前在 Windows 上会被加宽（module.key 65→66 字节、JSON 每行 +1 字节）。transport.py 的 key 读取路径 `read_text().strip()` 恰好宽容了这种损坏（测试 `test_widened_legacy_key_file_still_loads_same_seed` 钉住该行为），pairing.pepper 的读取路径则强校验 32 字节、直接启动失败。

## 4. catalog.py:456 —— 为什么没改

`core/vade/llm/catalog.py:456` 是 `dir_fd = os.open(path.parent, os.O_RDONLY)`，唯一用途是 `os.fsync(dir_fd)` 持久化目录项。**没改，理由**：
1. 只读 fd 不存在「写入被翻译」问题；文本/二进制模式差异只在写路径有语义。
2. Windows 上 `os.fsync` 对目录 fd 本来就抛错，代码已用 `except OSError: pass` 兜住（POSIX 才真正生效），行为不变。
3. 加 `_O_BINARY` 虽然无害，但那是在没有 bug 的地方加防御性噪声；该行若未来改为写入再修不迟。任务指示「不确定就不要改」，遵此。

## 5. locks.py（另一棒范围，仅报告不动）

`core/vade/admin/locks.py` 在本分支（origin/main @ 123faa8）上有同样的两个问题，留给该文件的跨平台锁修复一并处理：
- L36：`os.open(lock_path(directory), os.O_RDWR | os.O_CREAT, 0o600)` —— 无 `_O_BINARY`。
- L45：`os.write(fd, f"{os.getpid()}\n".encode("ascii"))` —— 返回值不检查，且 LF 会被文本模式 fd 加宽成 `\r\n`（pid 字符串后跟 CRLF；读取方是否 strip 决定是否成 bug）。

本分支的守护测试 `test_no_undecorated_write_open_flags_remain` 显式豁免 `locks.py` 并注释了原因，另一棒修完后可移除豁免。

## 6. 新增测试与原始输出

新文件：
- `core/tests/identity/test_pairing_binary_safety.py`（13 条）
- `sdk/python/tests/test_transport_binary_safety.py`（3 条）

### 6.1 修复后全量（Windows 11, Python 3.12.14, pytest 9.2.0）

```
$ cd D:/HermesWorkspace/Vade-wt/win-binary && D:/HermesWorkspace/Vade/Vade/.venv/Scripts/python.exe -m pytest core/tests/identity/test_pairing_binary_safety.py sdk/python/tests/test_transport_binary_safety.py -v
...
core\tests\identity\test_pairing_binary_safety.py::test_pepper_payload_of_only_line_feeds_roundtrips_byte_exact PASSED [  6%]
core\tests\identity\test_pairing_binary_safety.py::test_pepper_with_three_line_feeds_is_not_inflated PASSED     [ 12%]
core\tests\identity\test_pairing_binary_safety.py::test_pepper_with_mixed_crlf_survives_text_mode_fds PASSED    [ 18%]
core\tests\identity\test_pairing_binary_safety.py::test_pepper_write_completes_a_short_os_write PASSED          [ 25%]
core\tests\identity\test_pairing_binary_safety.py::test_session_store_persist_roundtrips_raw PASSED             [ 31%]
core\tests\identity\test_pairing_binary_safety.py::test_setup_token_write_roundtrips_raw PASSED                 [ 37%]
core\tests\identity\test_pairing_binary_safety.py::test_setup_state_atomic_write_roundtrips_raw PASSED          [ 43%]
core\tests\identity\test_pairing_binary_safety.py::test_core_identity_key_is_stored_byte_exact PASSED           [ 50%]
core\tests\identity\test_pairing_binary_safety.py::test_core_key_store_roundtrips_raw PASSED                    [ 56%]
core\tests\identity\test_pairing_binary_safety.py::test_whitelist_flush_roundtrips_raw PASSED                   [ 62%]
core\tests\identity\test_pairing_binary_safety.py::test_skill_store_atomic_write_roundtrips_raw PASSED          [ 68%]
core\tests\identity\test_pairing_binary_safety.py::test_skill_store_write_completes_a_short_fdopen_write PASSED [ 75%]
core\tests\identity\test_pairing_binary_safety.py::test_no_undecorated_write_open_flags_remain PASSED           [ 81%]
sdk\python\tests\test_transport_binary_safety.py::test_module_key_file_roundtrips_raw PASSED [ 87%]
sdk\python\tests\test_transport_binary_safety.py::test_widened_legacy_key_file_still_loads_same_seed PASSED [ 93%]
sdk\python\tests\test_transport_binary_safety.py::test_core_pin_write_roundtrips_raw PASSED [100%]
============================= 16 passed in 0.43s ==============================
PYTEST_EXIT=0
```

### 6.2 修前（pristine origin/main 代码，`git stash` 源码改动后）同一测试

```
$ git stash push core/vade sdk/python   # 源码改动暂存，测试文件保留
$ python -m pytest core/tests/identity/test_pairing_binary_safety.py -q
6 failed, 7 passed in 0.46s
  FAILED test_pepper_write_completes_a_short_os_write
  FAILED test_session_store_persist_roundtrips_raw
  FAILED test_setup_token_write_roundtrips_raw
  FAILED test_setup_state_atomic_write_roundtrips_raw
  FAILED test_core_key_store_roundtrips_raw
  FAILED test_whitelist_flush_roundtrips_raw
$ git stash pop && 同一命令
13 passed in 0.56s / FIXED_EXIT=0
```

注：flagship pepper 测试在第一轮修前是绿的（当时走真实随机字节，32 字节里大概率无 LF）——已升级为 monkeypatch `secrets.token_bytes` 强制 32 个 LF 走**真实生产写入路径**，使其确定性复现。第二轮针对性冒烟：

```
pristine（stash 源码后）：
  FAILED test_pepper_payload_of_only_line_feeds_roundtrips_byte_exact
  FAILED test_session_store_persist_roundtrips_raw
  2 failed in 0.32s
修复后（stash pop）：
  2 passed in 0.30s / FIXED_EXIT=0
```

Linux/macOS 上这些「修前红」用例**恒绿**（POSIX 无文本模式 fd，写入本来就字节精确），属预期，见 §7。

### 6.3 既有测试回归

```
$ python -m pytest core/tests/identity core/tests/test_skill sdk/python/tests/test_transport.py \
    core/tests/admin/test_passwords.py core/tests/admin/test_setup_bind_mutex.py \
    core/tests/admin/test_admin_revoke_persistence.py core/tests/admin/test_bind.py -q
267 passed, 2 failed in 27.90s (PYTEST_EXIT=1)
  FAILED sdk\python\test_pairing_hardening.py::test_pepper_file_is_created_0600_with_32_bytes
         AssertionError: assert (33206 & 511) == 384
  FAILED sdk\python\test_pairing_hardening.py::test_a_widened_pepper_mode_is_repaired_on_the_next_start
         AssertionError: assert (33206 & 511) == 384
```

这 2 个失败与本修复无关：断言 `st_mode & 0o777 == 0o600`，而 Windows 的 `os.chmod` 不实现 POSIX 权限位（`st_mode=33206` 是普通文件常量）。在**未改动**的主仓（`D:/HermesWorkspace/Vade/Vade`，`dev_gingshan`，工作树干净）跑同样两条：

```
$ cd D:/HermesWorkspace/Vade/Vade && python -m pytest core/tests/identity/test_pairing_hardening.py::test_pepper_file_is_created_0600_with_32_bytes core/tests/identity/test_pairing_hardening.py::test_a_widened_pepper_mode_is_repaired_on_the_next_start -q
2 failed in 0.14s (BASELINE_EXIT=0 — pytest 自身退出码 1，与 worktree 相同)
```

基线同样失败 ⇒ 预先存在的平台限制，非本次引入。按纪律未删、未跳过、未弱化。

## 7. Linux/macOS 行为不变论证

1. `_O_BINARY = getattr(os, "O_BINARY", 0)`：POSIX 的 `os` 模块没有 `O_BINARY` 属性，`getattr` 返回 `0`；`flags | 0` 是恒等运算，`os.open` 收到的 flags 与原来逐位相同。
2. `os.O_BINARY` 只在 Windows CRT 里定义且有语义；`os.open` 对值为 0 的标志位无操作。不存在「POSIX 上恰好与某个真实标志冲突」的可能（`O_RDONLY=0` 是唯一取 0 的标志，且各调用点都显式包含读/写标志，`| 0` 不改变已有位）。
3. `newline="\n"`：TextIOWrapper 在 POSIX 上默认 `newline=None` 本就不翻译输出 LF（只在输入做 universal newlines），写入字节流不变；显式传 `"\n"` 只是把「默认不翻译」写死。唯一理论差异是 `newline=''` 才有的「不翻译 + 任意换行原样写」语义，此处未用。
4. `_write_all`：POSIX 上对常规文件的 `os.write` 本就全量写出，循环第一轮即 `len(data)`，行为与原 `os.write(fd, data)` 逐字节等价；`written <= 0` 分支 POSIX 常规文件不触发（触发即 EAGAIN/EIO，抛错比静默截断正确）。
5. 测试在 Linux 上全部恒绿（payload 字节精确往返在任何平台都应成立），可在 CI 的 POSIX runner 直接跑，无平台 skip。

## 8. 交付清单

- 分支 `tmp/win-binary`，提交 `ac4b724 fix(win): os.open 写入路径补 O_BINARY，os.write 循环写完短写`（12 文件，+392/−21），未 push、未合并、未动 main/dev_gingshan/debug。
- 未使用 `--amend`/`rebase`/`reset`/`checkout -- .`（过程中用过的 `git stash push/pop` 仅作用于本任务自己的未提交改动，pop 已恢复，最终以 commit 收口）。
- `core/vade/admin/locks.py` 零改动（`git diff --name-only | grep locks` 为空）。
