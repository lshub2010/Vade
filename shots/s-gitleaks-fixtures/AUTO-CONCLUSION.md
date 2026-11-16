# AUTO-CONCLUSION（可粘贴；由 s-gitleaks-fixtures 棒生成，2026-11-15）

```
【gitleaks 工作区归零 — s-gitleaks-fixtures（ALPHA A6-3）】
- 主仓 Vade：gitleaks dir（工作区）= 0；私钥 grep（工作区）= 0 —— alpha 硬口径达成
  提交历史 `git .` = 5（gitleaks 语义=历史 diff；与 --all 相同，全为历史夹具行，本提交树 0 贡献，已登记）
  私钥 grep 全历史 = 1193 条（= 7 种源行[4 夹具 PEM 片段 + 2 文档字样 + 1 旧证据自引] × ~242 个提交树，已登记；1159→1193 增量全为新提交携带同源行，无新增源行）
- 客户端仓：gitleaks dir（工作区）= 0；私钥 grep（工作区）= 0
  提交树/全历史 = 8 / 8（未提交，留父代 commit；全部=上传夹具×4 + 旧证据自引×3 + 文档握手示例×1，已登记）
- 规则未削弱：.gitleaks.toml 零改动（仍=默认规则+4 条不入库树路径排除）；金丝雀 5 类真形假饵
  （AWS 20 位形 / ghp_+36 形 / sk-30 形 / Bearer curl 行 / 完整 RSA PEM 块）在未排除路径 5/5 命中
  （dir 与 git 两道同集），排除树同款饵 0 命中
- 手法：夹具一律运行时拼装且运行时值逐字不变（断言含 "TOPSECRET"、"sk-ABC" 子串与 assertEquals 全保留），
  文档示例换显式占位；未删测试、未 skip、未弱化断言
- 测试：pytest core/tests = 1203 passed（0 failed 0 error）；check_contracts / check_ledger / check_docs 全绿
- 工具/基点：gitleaks 8.30.1；主仓基点 0443039（origin/dev_gingshan）+ 本棒提交；客户端 67bfb02（未提交）
- 明细：shots/s-gitleaks-fixtures/RESULT.md
```
