# Fitness Agent · 文档入口

更新：2026-10-16。[GitHub增强分支](https://github.com/DanielGao9527/fitness-agent/tree/codex/fitness-hybrid-retrieval)。既有工程、记忆链路小验收、五类演示与文档收尾已交付；正式质量缺口和公网/手机验收保留，不宣布全部模型质量达标。

| 想看什么 | 入口 |
| --- | --- |
| 看五个演示、按步骤复现 | [DEMOS](DEMOS.md) |
| 已完成、真实成绩和已知失败 | [STATUS](STATUS.md) |
| 交付物与尚未勾选的验收项 | [DELIVERY](DELIVERY.md) |
| 后续还有什么、哪些不能自动启动 | [ROADMAP](ROADMAP.md) |
| 下一次开发从哪里接 | [EXECUTION](EXECUTION.md) |

日常仍打开localhost:8765，使用原账号/记录和v23增强实现。业务源码位于fitness-agent/data/runtime/remote-auth；外层启动入口已经统一，不需要选择旧版。五类演示包和9段录像仅保存在本地artifacts，Git保留技术复现说明与匿名汇总。

开发前阅读：[PRODUCT](PRODUCT.md) → [ARCHITECTURE](ARCHITECTURE.md) → [EXECUTION](EXECUTION.md) → [DEVELOPMENT_PROMPT](DEVELOPMENT_PROMPT.md) → [STATUS](STATUS.md) → [ROADMAP](ROADMAP.md)。

按需查阅：

- 运行：[安装与启动](../README.md#本地启动)、[配置](CONFIGURATION.md)、[账号](ACCOUNTS.md)、[运维/备份](OPERATIONS.md)、[部署](DEPLOYMENT.md)。
- 数据：[知识来源](KNOWLEDGE_SOURCES.md)、[隐私](PRIVACY.md)、[模型提示词](../prompts/README.md)。
- 验证：[测试说明](../tests/README.md)、[评测与原始结果](../evals/README.md)、[脚本说明](../scripts/README.md)。
- 历史：[归档](archive/README.md)；旧阶段状态不能代替当前结论。详细结果和失败仍留在原报告，不因本次精简被覆盖。

技术栈为Python、FastAPI、Pydantic、SQLite/FTS5、jieba、阿里云Embedding/千问API、RRF及SciPy MILP/HiGHS。Agent/Memory System描述工程职责，没有接入Hermes/LangGraph运行框架；固定响应回放、合成发布演示与真实效果分别标记。
