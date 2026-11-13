---
name: needs-mcp
description: 通过 mcp-bridge 查询外部知识库
when_to_use: 问题超出项目文档范围，需要外部知识库时
triggers: ["外部知识库"]
requires_tools: ["mcp-bridge__s_kb_recall"]
---

这个 skill 演示「依赖缺失」：工具不在当轮快照里时，只出现在目录行，正文不注入。

## When to Use

用户明确要求查外部知识库，而 `mcp-bridge` 已注册。

## Procedure

1. 调 `s_kb_recall`。
2. 引用返回条目的 id。

## Pitfalls

工具缺失时不要编造外部知识；直接说明 bridge 未安装。

## Verification

回答引用的条目 id 能在 bridge 的返回里找到。
