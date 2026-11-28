# 模块仓（独立仓库）

模块源码**不放在主仓**，每个模块一个独立私有仓，避免与 core/协议/schema 混在一起：

| 模块 | 仓库 | 状态 |
|---|---|---|
| echo | https://github.com/lshub2010/vade-module-echo | 参考实现（已迁出） |
| fs | https://github.com/lshub2010/vade-module-fs | 白名单根目录的 list/stat/read/write/delete（65 tests） |
| shell | https://github.com/lshub2010/vade-module-shell | cwd 白名单/超时/输出截断（77 tests） |
| web | https://github.com/lshub2010/vade-module-web | SSRF 防护的 fetch/head（120 tests） |
| model | https://github.com/lshub2010/vade-module-model | OpenAI 兼容 M6（94 tests） |
| memory | https://github.com/lshub2010/Vade-module-memory | 两层记忆/去重/TTL/管理 RPC/M4b 自进化 |
| mcp-bridge | https://github.com/lshub2010/Vade-module-mcp-bridge | stdio/HTTP MCP 上游桥接与动态能力更新 |

约定：
- 每个模块仓遵循 `docs/design.md` §15.1 的目录结构（`manifest.json` 由装饰器生成、`tools.py` 不 import SDK、`server.py` 只转发）
- 模块仓自带 CI 与测试，**不依赖主仓的 venv**
- 主仓只保留 `modules/README.md` 这张索引；`core` 里不得出现任何具体模块名/路径（P1 零硬编码）
