# 客户端已迁出

本目录不再存放客户端源码。Compose Multiplatform 客户端（Android / Desktop / Web）已独立成仓：

- 仓库：https://github.com/lshub2010/vade-client
- 本地：`D:/HermesWorkspace/Vade-client`
- 分支约定：`main`（客户端自己的基线）；任务书 `docs/agents/50-U1-*`～`53-U4-*` 里的 `ui/...` 路径均指该仓内的路径

原因：客户端与 `vade-module-<name>` 同属"独立仓库"约定（主仓只保留 `core/ protocol/ schema/ sdk/ admin-ui/ deploy/ tests/ docs/`）。`admin-ui/` 是 AgentCore 的附属产物，不是模块，仍留在主仓。