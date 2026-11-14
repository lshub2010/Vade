# s-sdk-ui-pages · Python SDK 能声明 UI 页面了，echo 模块用上它

主仓分支 `tmp/s-sdk-ui-pages`（基于 `dev_gingshan` @ `15312d0`），实现提交 **`bff6811`**。
echo 模块仓 `Vade-module-echo` 分支 `tmp/s-sdk-ui-pages`（基于 `main` @ `7508755`），实现提交 **`4d7d025`**。
本报告单独一提交。**未 push、未合并**。

## 1. 结论

`ModuleApp(ui_pages=...)` 现在存在，manifest 在**非空时**才多出 `"ui": {"pages": [...]}`；空列表的输出与改前逐字节相同。echo 模块用它声明了一页 `echo`，生成的 `manifest.json` 相对上一版是 **33 行纯新增、0 行删除**，`--print-manifest | diff -` 为空。真 core + 真模块 + 浏览器 WS 上 `ui/modules` 回包里出现了这一行页面入口（§7）。

## 2. 落点

| 项 | 内容 |
|---|---|
| SDK 入口 | `sdk/python/vade_sdk/app.py` — `ModuleApp.__init__(..., *, ui_pages: Sequence[Mapping[str, Any]] = ())`，放在 `env_prefix` 之后，与 `instance_id`/`role`/`max_concurrency` 同属 additive 那句 |
| 形状检查 | 同文件 `_check_ui_pages()`：每页须是 mapping 且带 `id`/`title`/`version`/`schema`，`id` 匹配 `PAGE_ID_RE = ^[a-z][a-z0-9_-]{0,31}$`；失败 `raise ValueError`，消息里同时有 `ui_pages[i]` 与 page id。**不重实现 C-03 的 page schema**（icon 枚举、节点树、`uiProps`、8 页/8 KiB 预算、重复 id、`navigate` 目标全留给 core） |
| 输出 | `_manifest_with()`：`if self._ui_pages:` 才写 `payload["ui"] = {"pages": [...]}`；`manifest_json()` 的 `sort_keys=True` 未动，页数组保序 |
| 深拷贝 | 构造时 deepcopy 一次、`manifest()` 再 deepcopy 一次，调用方事后改自己的 dict 或改返回值都不会改写已校验的页 |
| echo 声明 | `src/echo_module/server.py` 的 `build_app()` 里 `ui_pages=[...]`，内容按任务书逐字复制（`id=echo`、`title=回声`、`icon=list`、`version=1`、三段 `text`） |
| 生成物 | `manifest.json` 由 `python -m echo_module --print-manifest` 重新生成，**未手改** |

## 3. 主仓：core 认这一页

```
$ python -c "import sys,json; sys.path.insert(0,'core'); from vade.registry.manifest import validate_manifest; m=validate_manifest(json.load(open(<echo>/manifest.json,encoding='utf-8'))); print(len(m.ui_pages), m.ui_warnings, [t.name for t in m.tools])"
1 () ['echo', 'add', 'sleep', 'boom']
```

页面细节与 before/after 对照（同一个 core 校验器）：

```
page_id='echo' title='回声' icon='list' version=1 entry='auto' requires_session=False refresh_s=0
root type=None children=3
texts= ['这个页面由模块的 manifest 声明（C-03 ui.pages），不是客户端硬编码的。', '模块没有 ui 块时，ui/modules 里一个入口都不会出现（FR-8.79）。', '工具：echo / add / sleep / boom']
display_name='回声模块'

echo manifest BEFORE this batch (no ui block): ui_pages=0 ui_warnings=() tools=['echo', 'add', 'sleep', 'boom']
echo manifest AFTER  this batch           : ui_pages=1 ui_warnings=() tools=['echo', 'add', 'sleep', 'boom']
```

## 4. SDK 测试

新增 10 条（`sdk/python/tests/test_manifest.py`，该文件从 6 条变 16 条）：`ui` 块确实在 `ui.pages` 下、空列表逐字节不变、`--print-manifest` 也带 ui、多页顺序保持、坏 page id 点名报错、缺必填键点名报错、SDK 不越界判 icon、过 frozen jsonschema、过 core `validate_manifest`（并验证 core 才是降级判官）、页被深拷贝所以事后改不动 manifest。

> `bff6811` 的提交消息里写成「新增 9 条」，是笔误；`--collect-only` 实测 16 − 6 = 10。


```
$ pytest sdk/python/tests -q
FAILED sdk\python\tests\test_transport.py::test_key_file_is_created_0600_and_never_overwritten
1 failed, 59 passed, 2 skipped in 4.66s
```

那 1 条失败**不是本批引入**，也不是我改的文件：`test_transport.py` 一行未动（`git diff --stat -- sdk/python/tests/test_transport.py` 为空），断言是 POSIX 权限位，Windows 上做不到：

```
$ pytest sdk/python/tests/test_transport.py -q      # 把 app.py 换回 HEAD 版本后跑
E       assert 438 == 384                            # 0o666 vs 0o600
FAILED sdk\python\tests\test_transport.py::test_key_file_is_created_0600_and_never_overwritten
1 failed, 7 passed in 1.33s

$ python -c "import os,stat,tempfile,pathlib; p=pathlib.Path(tempfile.mkdtemp())/'k'; p.write_bytes(b'x'); os.chmod(p,0o600); print(oct(stat.S_IMODE(os.stat(p).st_mode)))"
0o666                                                # os.name='nt'：chmod 改不动 mode 位
```

**没有把它改成 `os.name != "nt"` 守卫**，因为任务书写死「不许 skip/弱化断言」，这是要由你拍的口径。正确收口（若你要）是一行平台守卫：Linux 仍校验 0600。

2 条 skipped 是 `jsonschema` 不在 `.venv` 里（`importorskip`）。其中 `test_manifest_passes_the_frozen_json_schema` **在改前就是 skip**，不是我弄红的。用装了 jsonschema 的解释器另跑一遍补齐证据：

```
jsonschema: manifest WITH ui.pages VALID against schema/manifest.schema.json
jsonschema: manifest WITHOUT ui VALID too
ui bytes: 355
```

### 4.1 改前的基线是红的（必须先说）

`dev_gingshan` @ `15312d0` 上 `pytest sdk/python/tests -q` **收集阶段全灭**：

```
ERROR sdk\python\tests\test_app.py - RuntimeError: vade_sdk error table drift...
ERROR sdk\python\tests\test_errors.py - RuntimeError: vade_sdk error table dr...
...(共 7 个模块，均为同一条)
E   RuntimeError: vade_sdk error table drifted from schema/errors.json (missing=['BUDGET_EXCEEDED'], extra=[]); update vade_sdk/errors.py
7 errors in 1.06s
```

根因：RFC-0005（`f108060`）往 `schema/errors.json` 追加了 `BUDGET_EXCEEDED`（现在 34 码），SDK 那份「冻结副本」没跟。见 §8 越界项 1。

## 5. echo 模块测试与自检

```
$ pytest tests -q
19 passed in 1.21s

$ python -m echo_module --check ; echo exit=$?
exit=0
```

## 6. manifest.json 的重生成：diff 只有 ui 块

```
$ git diff --stat manifest.json
 manifest.json | 33 +++++++++++++++++++++++++++++++++
 1 file changed, 33 insertions(+)

$ git diff manifest.json
@@ -94,5 +94,38 @@
   "priority": 0,
   "protocol": "vade/v1",
   "role": "tool",
+  "ui": {
+    "pages": [
+      { "icon": "list", "id": "echo", "schema": { "children": [ ... 三段 text ... ], "type": "column" }, "title": "回声", "version": 1 }
+    ]
+  },
   "version": "1.0.0"
```

0 行删除。行尾归一后再数一遍（`sed 's/\r$//'` 两侧都做，只为消除换行差异，不是改文件）：

```
33      <- 新增行数
0       <- 删除行数
```

README 那条不变量：

```
$ python -m echo_module --print-manifest | diff - manifest.json
(空)
```

### 6.1 CRLF/LF：如实报告，没有抹平

- echo 仓 `core.autocrlf = true`（全局继承），**提交的 blob 是 LF**（`git show HEAD~1:manifest.json | od -c` 首字节 `{ \n`），**工作区是 CRLF**（`od -c manifest.json` 首字节 `{ \r \n`）。所以「改前的 manifest.json」与「任意一次生成器输出」在不做行尾归一的情况下 diff 会**整份不同**——这是本机 checkout 的既有事实，不是本批造成的。
- 生成器本身**在 Windows 上输出 CRLF**：`sys.stdout.write()` 走文本模式做了 `\n → \r\n`。所以 `--print-manifest > manifest.json` 写出的就是 CRLF，正好与工作区一致，不变量成立；提交时由 `autocrlf` 归一成 LF，`git diff` 因此只显示 ui 块那 33 行。
- **我没有用 dos2unix / `newline=` / `.gitattributes` 去让 diff 好看**。上表两个数字（git 归一视图 33/0、手写归一 33/0）是同一事实的两种读法。

## 7. 加分项：真 core + 真模块 + 真 WS，`ui/modules` 回包

跑通了。探针脚本 `D:/Hermes/cache/scratch/ui-pages-proof/e2e_ui_modules.py`（**不在仓库里**，一次性取证）：起 `App`+`CoreApp`(uvicorn, `policy_mode=allow_all`) → 预白名单 echo 模块公钥 → 子进程 `python -m echo_module --core ws://127.0.0.1:18444`（Noise IK 注册）→ `POST /ui/session` 换 `__Host-ac_ui` cookie（返回 `device_id: ui-c272e8fc374009c9`）→ 带 cookie 连 `/ws` → 发一条 `ui/modules`。

```
core listening on ws://127.0.0.1:18444  (policy_mode=allow_all)
registered: com.example.echo#main  tools=['echo', 'add', 'sleep', 'boom']  ui_pages=['echo']  ui_warnings=[]
POST /ui/session -> 200 {'device_id': 'ui-c272e8fc374009c9'}
ui/modules reply:
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "revision": 1,
    "modules": [
      {
        "module_id": "com.example.echo",
        "instance_id": "main",
        "display_name": "回声模块",
        "version": "1.0.0",
        "online": true,
        "pages": [
          {
            "id": "echo",
            "title": "回声",
            "icon": "list",
            "entry": "auto",
            "version": 1,
            "requires": [],
            "refresh_s": 0
          }
        ]
      }
    ]
  }
}

modules=1 page_rows=1
```

**这一步只证明了「有页面时出现入口」**；「没有 ui 块时零入口」我只验到 manifest→core 这一层（§3 的 before 行：`ui_pages=0`），没有为它再起第二个模块做 WS 回包对照。

## 8. 越界项（两处，都不是「顺手改」）

1. **`sdk/python/vade_sdk/errors.py` 补 `BUDGET_EXCEEDED`（+4 行，0 删除）** — 不补则 §4.1 那条 RuntimeError 让 `import vade_sdk` 直接死、SDK 全部测试无法收集、echo 的 `--print-manifest` 也跑不了，本批任何一项验收都无法执行。错误消息原文就写着 `update vade_sdk/errors.py`；`retriable=False` 取自 `schema/errors.json` 里该条（`{"retriable": false, "http": 402}`），位置与 schema 键序一致（末位）。**没有动 `schema/**`**。
2. **`sdk/PYTHON_SDK.md` 签名行 + 一段说明、echo `README.md` 加「页面」一节** — 前者列出的是 `ModuleApp(...)` 的完整关键字表，不补 `ui_pages` 就是错的文档；后者是 echo 唯一说明「manifest 是生成物」的入口。

## 9. 变异自验（测试是不是真咬人）

对 `app.py` 逐条施加变异，跑 `pytest sdk/python/tests/test_manifest.py`，每次只让**预期的**用例变红，随后还原并确认无残留（`grep "if False:"` 为空、`git diff --stat` 与变异前一致）：

| 变异 | 变红 |
|---|---|
| M1 `if self._ui_pages:` → `if True:`（恒发空 ui） | `test_ui_pages_are_emitted_under_ui_pages`、`test_ui_block_is_absent_when_no_pages_are_declared` |
| M2 页数组 `reversed(...)` | `test_ui_pages_keep_declaration_order` |
| M3 去掉 page-id 正则判断 | `test_bad_page_id_fails_loudly_and_names_the_page` |
| M4 `missing = []`（不查必填键） | `test_page_missing_a_required_key_is_named_not_dropped` |

## 10. 契约门禁

```
$ python tools/check_contracts.py
contracts: OK (C-03/C-04/C-05/C-06/C-18 frozen, 34 error codes, config metadata complete, RFC-0005 append green, RFC-0010 session metadata green, RFC-0012 subagent family green, RFC-0014 session search green, RFC-0003 layers green, event gate green)
```

`schema/**` 与 `contracts/vade` 一个字节未动：

```
$ git diff --name-only 15312d0 HEAD -- schema contracts | wc -l
0
$ git diff --name-only bff6811^ bff6811
sdk/PYTHON_SDK.md
sdk/python/tests/test_manifest.py
sdk/python/vade_sdk/app.py
sdk/python/vade_sdk/errors.py
```


## 11. 改了哪些文件

主仓（`bff6811`）：

```
 sdk/PYTHON_SDK.md                 |  12 +++-
 sdk/python/tests/test_manifest.py | 139 ++++++++++++++++++++++++++++++++++++++
 sdk/python/vade_sdk/app.py        |  69 ++++++++++++++++++-
 sdk/python/vade_sdk/errors.py     |   4 ++
 4 files changed, 222 insertions(+), 2 deletions(-)
```

`app.py` 那 2 行删除是 docstring 里 `env_prefix.` → `env_prefix, ui_pages.` 与签名行替换，不是删代码。

echo 仓（`4d7d025`）：

```
README.md                 |  7 +++++++
manifest.json             | 33 +++++++++++++++++++++++++++++++++
src/echo_module/server.py | 14 ++++++++++++++
3 files changed, 54 insertions(+)
```

worktree 里有一个未跟踪的 `protocol/core/python/vade_protocol.dll`（32 MB，gitignore 内），是为 §7 的 e2e 从主仓拷的，**没有入库**。

## 12. 没做到 / 需要你决定

- `test_transport.py::test_key_file_is_created_0600_and_never_overwritten` 在 Windows 上恒红（先于本批存在）。要不要按 `os.name != "nt"` 守卫收口，你定。
- `jsonschema` 在 `.venv` 里缺失，SDK 的两条 schema 用例仍是 skip（改前就这样）。
- 「没有 ui 块 → 界面零入口」只验到 core 校验层，没做第二个模块的 WS 对照。
