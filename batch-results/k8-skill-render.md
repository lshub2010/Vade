# k8-skill-render 批次结果

## 改了什么

| 文件 | 内容 |
|---|---|
| `core/vade/skill/render.py` | `render_catalog` 新增 `hits`（`Activation.hits`）参数：预算消耗顺序改为文档规定的 `(激活优先 → hit_count 降序 → name 字典序)`；输出仍按 name 排序，保证 prompt 字节稳定。`render_bodies` 的截断提示删掉 `state.path`，改为 `[... truncated, remaining text omitted ...]`，prompt 不再出现宿主绝对路径 |
| `core/vade/skill/loader.py` | ① `render()` 把 `activation.hits` 传给 `render_catalog`；② `_read_directory` 在 `rglob` 前拒绝软链源目录，遍历时跳过软链子目录（不再静默当成空目录），软链文件仍拒绝；③ `materialize(kind="text")` 加上与 zip `SKILL.md` 相同的 1MB 上限，`preview`/`install` 同样生效；④ `truncate_body` 的提示路径改为相对引用 `<name>/SKILL.md`（`_scan_one` 与 `load_body`），正文截断提示不再泄露 data_dir |
| `core/vade/llm/capabilities.py` | `parse_models` 丢弃声明为 `*`（含 `" * "`）的模型名并记 warning：`*` 是「chat 模块未声明 models」时合成的 `<module>/*` 默认条目专用，声明同名会与之撞车并被当成任意模型 |
| `core/tests/test_skill/test_render.py` | 新增：L0 预算按 激活 → hit_count → name 裁剪（三档预算分别验证）；`render_bodies` 截断输出不含绝对路径；loader 级截断提示为 `long/SKILL.md` 且不含 tmp 路径 |
| `core/tests/test_skill/test_install.py` | 新增：text 安装 1MB 上限；目录安装拒绝软链源目录；目录安装跳过软链子目录（`preview.files == ()`） |
| `core/tests/model/test_catalog.py` | 新增：`parse_models` 丢弃 `*` / `" * "`，保留正常名字 |

## 验证

- `python -m pytest core/tests/test_skill core/tests/model -q` → **216 passed**
- `python -m pytest core/tests/integration/test_layered_wiring.py core/tests/session/test_replay_catalog.py core/tests/loop/test_prompt_layers.py core/tests/loop/test_compaction_catalog.py core/tests/loop/test_prompt_legacy.py -q` → **23 passed, 1 failed**
  - 唯一失败：`test_layered_wiring_survives_the_session_before_skill_install_order`，原因是本机缺 `protocol/core/python/libvade_protocol.so`（`OSError: cannot open shared object file`），与本次改动无关（此前批次结果已记录同一环境问题）。
- 未跑全量 pytest（按要求）。

## 设计说明 / 边界

- **排序**：docs/agents/23 §T3 的两句话要一起读——"超了按 `(激活优先, hit_count 降序, name 字典序)` **截断**"与"排序必须稳定：按 name 字典序"（输出）。因此 `render_catalog` 仍按 name 输出，只在**花预算**时用 hit_count 排序；`hit_count` 取 `Activation.hits`（因 `max_activated` 上限被挤掉的触发命中仍保留了计数）。
- **软链**：`rglob` 本身不递归软链目录，但此前软链目录被 `is_dir()` 静默 `continue`，源目录本身是软链时则会直接遍历到外部树。现在源目录是软链 → 拒绝；树内软链目录 → 明确跳过；软链文件 → 仍拒绝（沿用原策略）。
- **1MB 上限**：zip/path 源本就有 `MAX_SKILL_FILE_BYTES` / `max_uncompressed_bytes`，text 源此前无上限，现统一为 `MAX_SKILL_FILE_BYTES`。url 纯文本仍受 `MAX_URL_BYTES`（1,048,576）约束，二者相差 48KB，属既有常量设计，未改。
