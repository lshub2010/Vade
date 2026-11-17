# c3-audit 批次结果

## 改了什么

| 文件 | 内容 |
|---|---|
| `core/vade/audit/__init__.py` | 配置改读 C-18 的 `audit.retention_days` / `audit.path`（此前读 `audit.keep_days`——schema 里不存在这个键，`load_config()` 的 `_defaults()` 也不会给未知键补值，所以配置永远拿默认 30，`audit.retention_days` 被静默忽略）。`install()` 实例化 `RetentionJob` 并 `start()`；`0/-1` 在落任何 port 之前抛 `ConfigError(INVALID_ARGUMENT)` 拒绝启动。`build_writer()` 改成收完整路径 + `retention_days`。 |
| `core/vade/app.py` | `App.shutdown()` 第一步 `audit_retention.stop()`（join 2s）。放在最前是因为它是唯一不经调用方就动审计库的线程；停机后让 sweep 继续跑，重启时可能和下一个进程的 DROP 抢同一个文件。 |
| `core/vade/admin/app.py` | `CONFIG_ITEMS` 的 `audit.keep_days`（min 7/max 365）换成 `audit.retention_days`（min 1/max 3650，对齐 C-18）。旧条目让配置页写一个没人读的键，且 min 7 会拒掉 schema 允许的 1–6。 |
| `core/tests/audit/test_retention_wiring.py`（新） | 5 个用例 / 6 个 case，见下。 |

## 关键行为（可直接对上验收）

1. **保留天数真生效**：`writer.keep_days` 与 `RetentionJob.keep_days` 都来自 `audit.retention_days`（默认仍是 30）。`run_once()` 用真实时钟算 cutoff，`< cutoff` 的整张天表被 `DROP TABLE`，同时 `_tables` 缓存同步 discard（否则同进程下一次写还会往已删的表 INSERT）。
2. **删除动作落审计**：`AuditWriter.prune()` 的既有行为——`kind="audit.pruned"`、`status="ok"`、`extra.tables=[被删表名]`，写进当天表。测试按 reader 查这条行做断言，不是只看返回列表。
3. **启动/停止接线**：`install()` 起 `vade-audit-retention` 守护线程；`install()` 幂等（`app.audit` 已存在直接返回，不会起第二个 sweeper）；`App.shutdown()` 里 `_stop` 置位、`_thread` join 后清空。生产路径 `__main__._serve` 的 `finally: asyncio.run(app.shutdown())` 就是那个停机点。
4. **0/-1 双层拒绝**：`load_config()` 由 schema `minimum: 1` 先拒；组件层再拒一次（手搓 `Config` 的场景），报错带 `audit.retention_days`，且拒绝发生在 `app.audit`/`audit_writer`/`audit_retention` 被赋值之前——启动失败不会留下半个 port。
5. **`audit.path` 生效且默认不挪库**：`load_config()` 会合并 schema 默认值，所以 `cfg.get("audit.path")` 恒为 `"data/audit.sqlite3"`，无法用「有没有这个键」判断是否配置过。取法：**等于 C-18 默认值就按未配置处理**，仍落在 `<core.data_dir>/audit/audit.db`（既保证状态在 data_dir 下，不随进程 cwd 漂移，也不把升级前写下的审计落在旧位置无人读）；任何其他值原样使用，相对值按进程 cwd 解析（与 `core.data_dir` 同约定）。
6. **保留线程不创建空库**：原契约是「无管理流量不落盘」。`prune()` 会 `sqlite3.connect()` 建文件再去发现没有天表，所以 `_StoreAwareRetention.run_once()` 在库文件不存在时直接返回 `[]`。库存在时行为和 `RetentionJob` 完全一致。

## 验证

- `python -m pytest core/tests/audit -q` → **13 passed**（新增 6 case + 既有 immutable 3 / redact 4）
- `python -m pytest core/tests/admin core/tests/config -q` → **39 passed**（配置白名单换键没打坏管理面）
- `python -m pytest core/tests/policy core/tests/input/test_audit.py -q` → **18 passed**（writer/reader 未改，回归确认）
- 反向验证：`git checkout d682448~1 -- <3 个源文件>` 后跑新测试 → **5 failed, 1 passed**，恢复后全绿。唯一通过的是「默认路径留在 data_dir」——旧代码硬编码该路径，本来就该绿（它是回归护栏，不是复现 bug 的用例）。
- 未跑全量 pytest（按要求）。
- **集成测试在本机跑不了**：`tests/integration/test_admin_assembly.py`、`test_core_wiring.py`、`test_shutdown_restart.py` 全部因 `protocol/core/python/libvade_protocol.so` 缺失而失败（`App.startup()` 里 transport 加载 Rust 扩展）。用 `git stash -u` 在干净树上复跑同一条用例，同样失败 → 环境缺产物，不是本棒引入。因此「真实 `App.startup()` + `install_components()`」这条链路在本机只验证到 `install_components(app, strict=False)` 为止：手工脚本确认 `app.audit_retention` 在装完后存活、`App.shutdown()` 后 `_thread is None` 且 `_stop` 已置位。

## 需要注意 / 后续

- `audit.path` 的「等于默认值即视为未配置」是一条有意的兼容取舍：真正想用 `data/audit.sqlite3` 的部署写不出这个意图（会落到 legacy 路径）。若以后要统一到 C-18 默认位置，应当做一次性迁移（旧文件存在且新文件不存在时 `os.replace`），而不是直接改解读规则。
- `RetentionJob.stop()` 之后实例不可复用（`_stop` 不会 clear，`retention.py` 未在本次授权文件内）。当前无影响：`install()` 幂等、重启是新的 `App`/新的 job；但若以后要在同进程内 restart 同一个 `App`，需要在 `start()` 里 `self._stop.clear()`。
- 保留窗口只在 `install()` 时读一次（`x-vade.apply = restart`），符合契约；改配置需要重启才生效。
- 管理面 `CONFIG_ITEMS` 没有暴露 `audit.path`：路径来自浏览器可写的配置页等于给了一个任意路径写 SQLite 的入口，且它是 restart 键、改完要重启才有意义。需要改路径请改 `core.toml`。
- `App.shutdown()` 仍不 `close()` `audit_writer`（本次未授权扩张范围）。停机时队列里未落盘的审计行靠进程退出时的 SQLite/OS 兜底，若要「优雅停机不丢审计行」，下一步应在 `shutdown()` 里 `audit_writer.flush()/close()`。
