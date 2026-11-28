# t3 e2e 在 Windows 上跑通（tmp/t3-win）

分支：`tmp/t3-win`（基线 `origin/dev_gingshan` = `3b7e248`）
Worktree：`D:/HermesWorkspace/Vade-wt/t3-win`
改动文件：**只有** `sdk/python/tests/t3_model_tools_e2e.py`（无其他文件改动，见 §5）
环境：Windows 11 x86_64 / git-bash / `.venv/Scripts/python.exe`（CPython 3.12）
绑定产物：`protocol/core/python/vade_protocol.dll`（32 MB，gitignored，已从主检出拷入 worktree）

---

## 1. 障碍 1：`_binding()` 硬编码 `.so`

### before

`_binding()` 只看 `libvade_protocol.so`；Windows 产物是 `vade_protocol.dll`，于是
worktree 里明明有 dll 也判成「没有绑定」，直接 `SystemExit`：

```
$ unset VADE_PROTOCOL_PY VADE_PROTOCOL_SO VADE_PROTOCOL_LIB
$ VADE_MODEL_DIR=... VADE_ECHO_DIR=... .venv/Scripts/python.exe sdk/python/tests/t3_model_tools_e2e.py
no libvade_protocol.so in this worktree. Build it, or set VADE_PROTOCOL_SO to the exact .so you mean to test with (VADE_PROTOCOL_PY points at a directory holding both files). Refusing to borrow a sibling worktree's binding silently.
EXIT=1
```

错误信息还把用户指向 `VADE_PROTOCOL_SO`（一个 POSIX 名字），在 Windows 上是误导。

### after

* 新增 `_shared_lib_name()` / 模块级 `SHARED_LIB`，按平台给出 uniffi 生成代码真正会加载的名字：
  `darwin → libvade_protocol.dylib`、`os.name == "nt" → vade_protocol.dll`、其余 → `libvade_protocol.so`。
  依据是 `vade_protocol.py::_uniffi_load_indirect()`（498-516 行）本身就按这三个名字从
  `vade_protocol.py` 同目录加载，所以临时目录里放的副本/符号链接必须**叫这个名字**，否则
  import 仍会失败。
* 树内查找、`VADE_PROTOCOL_SO` 分支、临时目录里的符号链接/副本，全部改用 `SHARED_LIB`，
  不再有第二处硬编码。
* 新增 `VADE_PROTOCOL_LIB` 作为跨平台显式指定入口；`VADE_PROTOCOL_SO` 保留为别名（老用法不变）。
* 新增 `_check_binding_dir()`：`VADE_PROTOCOL_PY` 现在会校验目录里同时有 `vade_protocol.py`
  **和** `SHARED_LIB`，缺哪个就报哪个，并说明该建什么。之前 `VADE_PROTOCOL_PY` 是指向不存在
  目录也照单全收，直到后面 import 才炸。
* 三种失败路径的实测输出（都 `EXIT=1`，都是可执行的指引，不是 traceback）：

```
--- 树里没有 dll，也没给 env ---
no vade_protocol.dll in this worktree (D:\HermesWorkspace\Vade-wt\t3-win\protocol\core\python). Build it (cargo build --features uniffi --lib in protocol/core) and copy the cdylib into D:\HermesWorkspace\Vade-wt\t3-win\protocol\core\python, or set VADE_PROTOCOL_LIB to the exact vade_protocol.dll you mean to test (VADE_PROTOCOL_SO is the legacy name for the same thing; VADE_PROTOCOL_PY points at a directory holding both files). Refusing to borrow a sibling worktree's binding silently.

--- VADE_PROTOCOL_LIB 指向不存在的文件 ---
VADE_PROTOCOL_LIB does not exist: D:\nope\vade_protocol.dll

--- VADE_PROTOCOL_PY 指向缺 dll 的目录 ---
VADE_PROTOCOL_PY=D:\HermesWorkspace\Vade-wt\t3-win\protocol\core\python has no vade_protocol.dll; that is the name the generated binding loads on this platform. Build it (cargo build --features uniffi --lib in protocol/core) and copy the cdylib next to vade_protocol.py, or set VADE_PROTOCOL_LIB to the exact file.
```

成功路径会自己打印找到的库：

```
[info] binding lib (in tree): D:\HermesWorkspace\Vade-wt\t3-win\protocol\core\python\vade_protocol.dll
```

---

## 2. 障碍 2：临时 `core.toml` 里路径含反斜杠

### before

原代码 `f'data_dir = "{data}"'` 直接插 `str(Path)`。Windows 上得到
`data_dir = "C:\Users\LENSHLD\AppData\Local\Temp\vade-t3-xxxx\core-data"`，
TOML 基本字符串里 `\U` / `\H` 是转义引导符，`\U` 要求 8 位十六进制：

```
$ VADE_PROTOCOL_PY=D:/HermesWorkspace/Vade/Vade/protocol/core/python \
  .venv/Scripts/python.exe sdk/python/tests/t3_model_tools_e2e.py
...
  File ".../tomllib/_parser.py", line 516, in parse_hex_char
    raise suffixed_err(src, pos, "Invalid hex value")
tomllib.TOMLDecodeError: Invalid hex value (at line 3, column 17)
EXIT=1
```

### after

新增 `_toml_path(path)`，返回 TOML 基本字符串的**内容**（不含引号）：

```python
return path.as_posix().replace("\\", "\\\\").replace('"', '\\"')
```

* `as_posix()` 在 Windows 上给 `D:/Hermes/...`，Python 与 TOML 都直接接受（不需要 `\\` 转义）。
* 再兜一层 `\` 与 `"` 转义，即使目录名里真出现反斜杠或引号也不会破坏字符串。
* **故意不加 `.resolve()`**：POSIX 上 `as_posix()` 与 `str(path)` 逐字节相同，加了 `resolve()`
  反而可能在 macOS 上改写 `/tmp` 符号链接、把 `/tmp/...` 变成 `/private/tmp/...`，那才是行为变化。
  实测（`PurePosixPath`，与运行时 `PosixPath` 的 `as_posix()`/`__str__` 一致）：
  `/tmp/vade-t3-abc/core-data` → 渲染结果与 `str(path)` 完全相同 `same=True`。

**范围检查**：脚本里写进 TOML 的路径只有 `data_dir` 一处（`grep toml|write_text` 确认：
`core.toml` 是唯一被生成的 TOML，其余路径都走环境变量 `VADE_DATA_DIR` / `MODULE_KEY_PATH`，
不经过 TOML 转义）。`listen`、`log_level`、模块 id 都是 ASCII 字面量，无需处理。

隔离验证（不经 e2e，直接测函数）：

```
SHARED_LIB = vade_protocol.dll
toml body  = D:/HermesWorkspace/Vade/Vade-module-model/src
toml doc   = [core]
data_dir = "D:/HermesWorkspace/Vade/Vade-module-model/src"
roundtrip  = D:/HermesWorkspace/Vade/Vade-module-model/src
matches    = True
quoted ok  = D:/a b/"quoted" x          # 目录名含空格+引号也能 roundtrip
```

---

## 3. 最终完整跑通输出（不设 VADE_PROTOCOL_PY / _SO / _LIB）

```
$ cd D:/HermesWorkspace/Vade-wt/t3-win
$ unset VADE_PROTOCOL_PY VADE_PROTOCOL_SO VADE_PROTOCOL_LIB
$ export VADE_MODEL_DIR="D:/HermesWorkspace/Vade/Vade-module-model"
$ export VADE_ECHO_DIR="D:/HermesWorkspace/Vade/Vade-module-echo"
$ D:/HermesWorkspace/Vade/Vade/.venv/Scripts/python.exe sdk/python/tests/t3_model_tools_e2e.py
[info] binding lib (in tree): D:\HermesWorkspace\Vade-wt\t3-win\protocol\core\python\vade_protocol.dll
[info] fake OpenAI upstream: http://127.0.0.1:64904/v1

=== modules register ===
[PASS] both modules registered online=['com.example.echo', 'com.example.model']
[PASS] model module serves chat tools=['chat']
[PASS] echo module serves echo/add/sleep/boom tools=['echo', 'add', 'sleep', 'boom']

=== turn: model calls echo/add/sleep/boom in one step ===
[info] event timeline: [(5, 'turn.tool_call', 'call_t3_echo_1'), (6, 'turn.tool_result', 'call_t3_echo_1'), (7, 'turn.tool_call', 'call_t3_echo_2'), (8, 'turn.tool_result', 'call_t3_echo_2'), (9, 'turn.tool_call', 'call_t3_echo_3'), (10, 'turn.tool_result', 'call_t3_echo_3'), (11, 'turn.tool_call', 'call_t3_echo_4'), (12, 'turn.tool_result', 'call_t3_echo_4'), (14, 'turn.completed', None)]
[PASS] four turn.tool_call events count=4 types=[...4x tool_call + 4x tool_result + turn.completed]
[PASS] four turn.tool_result events count=4
[PASS] exactly one turn.completed count=1
[PASS] tool_call ids are call_t3_echo_1..4 call_ids=['call_t3_echo_1', 'call_t3_echo_2', 'call_t3_echo_3', 'call_t3_echo_4']
[PASS] every tool_call precedes its own tool_result calls={'call_t3_echo_1': 5, ...} results={'call_t3_echo_1': 6, ...}
[PASS] all four tool_results precede turn.completed results=[6, 8, 10, 12] completed=14
[PASS] upstream saw exactly two requests count=2
[PASS] upstream resolved all four advertised tools by suffix tools={'echo': 'echo__echo', 'add': 'echo__add', 'sleep': 'echo__sleep', 'boom': 'echo__boom'} problem=None
[PASS] four tool_calls name the advertised echo tools tools=['echo__echo', 'echo__add', 'echo__sleep', 'echo__boom']
[PASS] echo result is ok and carries the echo payload ok=True summary='{"text": "hello-from-t3", "len": 13}'
[PASS] add result is ok and sums 2 + 3 ok=True summary='{"sum": 5.0}'
[PASS] sleep result is ok and reports the slept duration ok=True summary='{"slept_ms": 1}'
[PASS] boom is a structured INTERNAL failure, not a turn failure ok=False code='INTERNAL' summary='INTERNAL: t3-boom'
[PASS] turn completed state=completed
[PASS] first request advertised the four echo tools and hid the provider channel tools=['echo__add', 'echo__boom', 'echo__echo', 'echo__sleep', 'ask_user']
[PASS] second request carries exactly four tool messages tool_messages=[4 条，tool_call_id 与 name 齐全]
[PASS] tool messages cover all four tool_call ids ids=['call_t3_echo_1', 'call_t3_echo_2', 'call_t3_echo_3', 'call_t3_echo_4']
[PASS] echo result reached the model intact content='{"text": "hello-from-t3", "len": 13}'
[PASS] add result reached the model intact content='{"sum": 5.0}'
[PASS] sleep result reached the model intact content='{"slept_ms": 1}'
[PASS] boom failure reached the model as a structured error content='{"ok": false, "error": {"code": "INTERNAL", "message": "t3-boom"}}'
[PASS] second request carries all four assistant tool_calls assistant_calls=[4 条，id 顺序 call_t3_echo_1..4]
[PASS] transcript is user/assistant/tool x4/assistant roles=['user', 'assistant', 'tool', 'tool', 'tool', 'tool', 'assistant']
[PASS] transcript tool messages keep the four tool_call ids ids=['call_t3_echo_1', 'call_t3_echo_2', 'call_t3_echo_3', 'call_t3_echo_4']
[PASS] final assistant answer is the upstream's second reply content='T3-FINAL: echo, add and sleep ran; boom failed with INTERNAL as designed'
[PASS] live sink saw all four tool_calls and tool_results tool_call=4 tool_result=4

[info] work dir removed (VADE_KEEP_WORK=1 keeps it)

=== result ===
all checks passed
EXIT=0
```

计数：`grep -c '^\[PASS\]'` = **29**，`grep -c '^\[FAIL\]'` = **0**，退出码 **0**。
连跑 2 次结果一致（29/0、EXIT=0 各两次，另加最终确认跑一次）。

---

## 4. POSIX 行为是否变化

**共享库解析**：不变。`SHARED_LIB` 在 Linux 上就是 `libvade_protocol.so`（模拟
`sys.platform='linux'; os.name='posix'` 实测输出 `libvade_protocol.so`，macOS 模拟输出
`libvade_protocol.dylib`），树内查找路径、临时目录里的文件名、符号链接名都与改动前逐字相同。
`VADE_PROTOCOL_SO` 仍被识别（现为 `VADE_PROTOCOL_LIB` 的别名，二者都读）。

**TOML 路径**：不变。`PosixPath.as_posix() == str(path)`，且不带 `resolve()` 就与原
`f'data_dir = "{data}"'` 的输出完全一致；额外加的 `\` 与 `"` 转义对 POSIX 常见路径是恒等变换
（实测 `/tmp/...`、`/home/...` 两种路径 `same=True`）。唯一会变的情形是目录名里真的含 `\` 或 `"`
—— 那是从「生成非法/错误 TOML」变成「生成正确 TOML」，是修 bug 不是改行为。

**`VADE_PROTOCOL_PY` 变严**：现在要求目录里同时有 `.py` 和当前平台的库。POSIX 上按老用法
（指向 `protocol/core/python`，里面有 `.so`）仍然通过；只有在指向缺 `.so` 的目录时才会提前
报错，而那种情况原本也会在 import 阶段失败，只是错误更难读。

**测试语义**：6 条断言全部保留，无 skip、无超时调大、无删除。唯一改动的断言语义见 §5。

---

## 5. 唯一一处断言改动（与平台无关，需单列理由）

`[FAIL] first request advertised the four echo tools + model chat` 这条在**两个障碍都修好之后**
仍然失败，且与平台无关：

```
[FAIL] first request advertised the four echo tools + model chat
       tools=['echo__add', 'echo__boom', 'echo__echo', 'echo__sleep', 'ask_user']
```

原因：本脚本写于 `bbf8b1a`（2026-11-27），它假设 `model__chat` 会出现在工具表里。**之后**
合入的 `7c2b3e6`「fix(model): 模型通道不进工具表，仅模型模块在线时仍可对话」明确把 provider
通道从工具表剔除（`core/vade/loop/tools.py: PROVIDER_TOOLS = frozenset({"chat"})`，理由是
§FR-2.7 禁止 model 反向调用自己的 provider，且多模型模块在线时会暴露多个可调用项），
而本脚本在 `7c2b3e6..HEAD` 区间**从未被同步更新**（`git log 7c2b3e6..HEAD -- <script>` 为空）。

也就是说：旧断言与已合入的契约相反，在 Linux 上同样会失败。合并契约测试自证：

```
$ PYTHONPATH=core:protocol/core/python .venv/Scripts/python.exe -m pytest \
    core/tests/model/test_multiple_modules.py -q -k provider_channel
1 passed, 5 deselected in 0.32s        # test_the_provider_channel_is_not_a_tool_the_model_can_call
```

因此把该条改为断言真实契约（**不是弱化**：从「必须出现 model__chat」变成「必须不出现
model__chat」，仍要求四个 echo 工具全部出现，并新增了「provider 通道不得泄漏进工具表」这一条
约束）：

```python
check(
    "first request advertised the four echo tools and hid the provider channel",
    all(name in first_tools for name in expected_names if name)
    and "model__chat" not in first_tools,
    f"tools={first_tools}",
)
```

未改动 core 代码：`7c2b3e6` 的契约是对的（有测试与文档依据），错的是这个 e2e 脚本的期望。
若评审认为应保持脚本原样、另开一棒修，本改动可单独回退（只涉及这一处 `check`）。

---

## 6. 提交

只 `git add` + `git commit`，未 `--amend` / `rebase` / `reset`，未 push、未合并、未动远端。
改动文件：`sdk/python/tests/t3_model_tools_e2e.py`（+ `shots/t3-win/RESULT.md`）。
