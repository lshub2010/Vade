# 模块仓（独立仓库）

模块源码**不放在主仓**，每个模块一个独立私有仓，避免与 core/协议/schema 混在一起：

| 模块 | 仓库 | 状态 |
|---|---|---|
| echo | https://github.com/lshub2010/vade-module-echo | 参考实现（已迁出） |
| fs | https://github.com/lshub2010/vade-module-fs | 白名单根目录的 list/stat/read/write/delete（65 tests） |
| shell / web / memory / mcp-bridge / model | `vade-module-<name>` | W5 待建 |

约定：
- 每个模块仓遵循 `docs/design.md` §15.1 的目录结构（`manifest.json` 由装饰器生成、`tools.py` 不 import SDK、`server.py` 只转发）
- 模块仓自带 CI 与测试，**不依赖主仓的 venv**
- 主仓只保留 `modules/README.md` 这张索引；`core` 里不得出现任何具体模块名/路径（P1 零硬编码）
