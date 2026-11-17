# d1-migrate 批次结果：scrypt → Argon2id 批量迁移

任务 #7。此前只有在线重哈希（登录成功后 `SetupService.rehash_password()`），停机的部署永远不会触发它；`password_needs_rehash` 也就失去了「健康信号」的意义。本棒补的就是离线批量这一块。

## 改了什么

| 文件 | 内容 |
|---|---|
| `tools/migrate_password_hashes.py`（新，470 行） | 批量迁移 CLI：扫描一个或多个 data dir（也接受 `admin/admin.json` 路径），把 `scrypt$n$r$p$salt$digest` 换成当前参数的 Argon2id，并把参数写进 `admin.json`。`--dry-run` / `--credentials` / `--password-stdin` / `--json`。 |
| `core/vade/admin/setup.py` | 新增 `SetupService.replace_password_hash(new_hash, *, expected_current, params, rehashed_from)`：`admin.json` 的第二个写入口，仍是唯一写者。CAS 语义 + 「只接受当前参数的 Argon2id」护栏。 |
| `core/tests/admin/test_password_migration.py`（新，281 行） | 14 个用例，见下。 |
| `batch-results/d1-migrate.md` | 本文件。 |

`auth.py` 没有改动：格式判别用 `argon2.extract_parameters()`（公共 API）+ scrypt 六段解析，不需要把私有解析逻辑再复制一份到 `auth.py` 里。

## 关键行为（逐条对上验收）

1. **`--dry-run` 只报告**：不写文件、不写审计、不产生 `password_params`。测试比对 `admin.json` 的**字节**（不是断言之一个字段），并断言 dry-run 后仍是 `scrypt$…`。带 `--credentials` 时 dry-run 会额外校验口令能对上，但不写盘。
2. **实跑后全部 Argon2id，参数随哈希记录**：`password` 是 `$argon2id$v=19$m=65536,t=3,p=1$…`；`password_params = {alg, version:19, m:65536, t:3, p:1, hash_len:32, salt_len:16}` 是**从实际写下去的那条 PHC 串里读回来的**（`extract_parameters`），不是照常量抄的——两边不可能漂移。另记 `password_rehashed_at` / `password_rehashed_from: "scrypt"`。文件仍是 tmp + `os.replace` + 0600（走 `SetupService._write_admin`）。
3. **未知/损坏格式非 0 退出并列清单**：退出码 `2`，stdout 末尾单列一节
   ```
   unknown or corrupt hash formats (left untouched):
     /srv/x/admin/admin.json: unknown hash format (neither scrypt$… nor $argon2…)
   ```
   逐条带路径和原因。分类覆盖：缺 `password` 字段、非 JSON 的 `admin.json`、`scrypt$` 段数/整数/base64/digest 长度不对、`n` 不是 2 的幂或越界（`2..2**20`，防 `hashlib.scrypt` 被恶意 `n` 打到 OOM）、`$argon2` PHC 解析失败。这些目标一律**不写**。
4. **迁移后旧密码可登录**：这是整件事的验收点，所以测试不是只 `assert verify_password(...)`，而是走真实 `POST /admin/api/login`（`conftest.login_browser`）拿 200 + csrf，并断言登录路径**没有**再重哈希（`password_hash()` 与迁移后一致）。工具自身在写盘前也用同一口令验一遍新哈希，写不下去的值不可能是登不进的值。
5. **写 `admin.password_rehashed` 审计**：每个成功迁移的目标写一条到该 data dir 的 `audit/audit.db`（`kind="admin.password_rehashed"`、`status="ok"`、`actor="cli"`，`extra={to, from, source:"offline-migration", path, m, t, p, version}`），与在线路径的 kind 一致，运维按 kind 查就能同时看到两种来源（`source` 区分）。

另外两条安全性质（验收没写，但不做就是错的）：

- **口令对不上绝不写**：`scrypt` 摘要不可逆，凭据文件里一个错字就会毁掉一个今天还能用的凭据。所以 `verify_password(password, stored)` 为假时该目标只报 `skipped`、退出码 3、文件字节不变（有测试）。
- **CAS 而非盲写**：`replace_password_hash` 要求 `admin.json` 里还是刚读到的那条哈希才写。中间有登录完成在线重哈希，就以登录写的为准，本目标报 `skipped`（退出码 3），不覆盖。
- 空口令/缺失口令的真跑同样是 `skipped` + 退出码 3（不猜、不跳过静默）。
- **运行中的 core 会被挡住**：`data/core.lock` 被 `flock` 住时该目标 `skipped` + 退出码 3、文件不动（与 `vade admin` 的 `_require_not_running` 同口径）。真在跑着的 core 上写 `admin.json`，进程内存里的旧认知会在下次登录时把它盖回去，而且没有告警。

**退出码**（写进模块 docstring 和 `--help`）：`0` 全部已是当前参数 Argon2id；`1` 致命（路径不存在、所有目标都没有 `admin.json`、凭据文件不可读、写失败）；`2` 存在未知/损坏格式；`3` 未完成（缺口令 / 口令对不上 / 输了 CAS / core 还在跑）。优先级 1 > 2 > 3。`2` 不会阻断健康目标（有测试：坏的一个 + 好的一个，好的照样迁移完，退出码仍是 2）。

## 验证

- `python -m pytest core/tests/admin/test_password_migration.py -q` → **14 passed**（14.7s；Argon2 参数是设计值，慢是正常的）
- `python -m pytest core/tests/admin -q` → **69 passed**（94.7s，含既有 `test_passwords.py` 5 个与 bind/revoke/readonly 全套，`setup.py` 改动没打坏任何一条）
- 真 CLI 手跑（scratch 目录，4 个目标：legacy / 已 Argon2id / `md5$deadbeef` / legacy 无口令）：
  ```
  $ python tools/migrate_password_hashes.py <a> <b> <c> <d> --dry-run
  dry run: nothing is written
  targets: 4  planned: 2  already-current: 1  bad-format: 1
    planned         …/data-a  scrypt (n=16384,r=8,p=1) → argon2id (m=65536,t=3,p=1)  — no plaintext supplied; …
    already-current …/data-b  argon2id (m=65536,t=3,p=1)
    bad-format      …/data-c  — unknown hash format (neither scrypt$… nor $argon2…)
  exit=2
  ```
  实跑（带凭据）→ `migrated: 1  already-current: 1  skipped: 1  bad-format: 1`、`exit=2`，写下的 `admin.json` 为
  `"password": "$argon2id$v=19$m=65536,t=3,p=1$…"` + `password_params{…}` + `password_rehashed_from: "scrypt"`；同命令第二遍 → `already-current: 2`、`exit=0`，文件字节不变。
- 审计行用 `AuditReader(path).query(kind="admin.password_rehashed")` 真读到：
  `{"kind":"admin.password_rehashed","status":"ok","actor":"cli"}` + `extra={"to":"argon2id","from":"scrypt","source":"offline-migration","path":…,"m":65536,"t":3,"p":1,"version":19,"audit_id":"01M7KV6…"}`。
- `--password-stdin`（单目标、`printf … |`）与「目标直接给 `admin.json` 路径 / 凭据文件用对象形式」两条入口都手跑过，`exit=0`。
- 未跑全量 pytest（按要求）。

## 需要注意 / 后续

- **Argon2id 不能从 scrypt 摘要推导**，所以批量迁移必然需要明文。工具的选择是「缺口令就报错退出，不猜、不静默跳过」——想一把跑完就必须提供覆盖全部 legacy 目标的凭据文件。批量场景下这意味着要临时把明文落到一台机器上（`--credentials` 文件或 `--password-stdin`），凭据文件用完即删；这是这个任务的固有代价，不是实现选择。
- `--password-stdin` 不读 TTY 提示、不回显（`sys.stdin.readline()`），适合管道；但没有 `getpass` 版本，交互式单机迁移还得手打或写临时文件——如有需要再加 `--password-prompt`。
- 工具对 `data/core.lock` 的检查在 **Windows 上退化为「视为未运行」**（没有 `flock`），和 `vade admin` 的 `_require_not_running` 一致；Linux/macOS 上是真的挡住了（有测试，Windows 跳过）。
- `hashlib.scrypt` 的 `maxmem` 是 OpenSSL 默认 32 MiB，所以历史哈希若 `n≥2**15, r=8` 本来就无法在线验证（登录也失败），本工具会如实报 `supplied plaintext does not verify`。Vade 只写过 `n=2**14,r=8,p=1`，不受影响；判别器允许到 `n=2**20`，只为把「结构损坏」和「参数离谱」分开，不代表能验证。
- `PROGRESS.md` 四之 5「scrypt 旧哈希迁移（仍在）」现在可以划掉——本棒没有改 `PROGRESS.md`（避免和其它批次抢同一文件），留给汇总时统一改。
- 未 push（按要求）。
