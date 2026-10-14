# Fitness Agent · 项目总览

更新：2026-10-14。这里是当前增强工作的唯一进度入口；阶段交接已归档。GitHub：[已交付增强版](https://github.com/DanielGao9527/fitness-agent/tree/codex/fitness-hybrid-retrieval)，分支 codex/fitness-hybrid-retrieval。

面向中文饮食记录、餐次规划与当天训练建议，将自然语言和多模态识别结果变为可核对草稿，结合个人条件与公共知识证据给出建议。

## 先看这三份

| 想了解什么 | 文档 | 内容 |
| --- | --- | --- |
| 现在做到哪了 | [当前基线与验证结果](STATUS.md) | 已实现、未完成、真实结果和已知问题 |
| 最终要交什么 | [最终交付与验收清单](DELIVERY.md) | 产品、代码、评测、演示和简历材料 |
| 下一步怎么做 | [后续开发路线](ROADMAP.md) | 顺序、具体任务、通过条件和代码入口 |

**当前结论：E1 已实现；E2 工程链路已接通，真实质量验收未收尾；E3b 零命中恢复、时间预算和固定响应回放已实装，真实质量对照待办；E4 治理流程待开发。** 本机 localhost:8765 已运行 e3-bounded-retrieval-v18，原账号和记录在备份核验后保留。

## 项目主线

| 已有业务底座 | 本轮增强主线 | 最终目标 |
| --- | --- | --- |
| 饮食/训练记录、草稿确认、受约束配餐、当天训练建议 | 中文混合检索 → 个人记忆 → 有界检索与回放 → 受控知识更新 | 一个能演示、能解释、能复现验证的个人项目 |

技术栈：Python、FastAPI、Pydantic、SQLite/FTS5、jieba、阿里云 Embedding/千问 API、RRF、SciPy MILP/HiGHS。Agent 工作流和 Memory System 是设计能力；没有接入 Hermes 或 LangGraph 作为运行框架。

## 开发入口与阅读顺序

日常启动统一使用 fitness-agent/scripts/start.ps1，固定使用 localhost:8765；原 app:app 入口也转到增强代码，用户无需选择版本。当前源码仓库仍在 fitness-agent/data/runtime/remote-auth，外层保留的历史业务文件不再用于应用启动。

开发前依次阅读 [PRODUCT](PRODUCT.md) → [ARCHITECTURE](ARCHITECTURE.md) → [EXECUTION](EXECUTION.md) → [DEVELOPMENT_PROMPT](DEVELOPMENT_PROMPT.md) → [STATUS](STATUS.md) → [ROADMAP](ROADMAP.md)。这些文件分别负责业务范围、当前实现、接续点、开发约束、已核实事实和未来计划。

## 按需查阅

- 运行：[安装与启动](../README.md#本地启动)、[配置](CONFIGURATION.md)、[账号](ACCOUNTS.md)、[部署](DEPLOYMENT.md)、[备份恢复](OPERATIONS.md)。
- 数据：[知识来源](KNOWLEDGE_SOURCES.md)、[隐私](PRIVACY.md)、[应用提示词](../prompts/README.md)。
- 验证：[测试说明](../tests/README.md)、[评测数据](../evals/README.md)、[脚本说明](../scripts/README.md)。
- 历史：[归档索引](archive/README.md)。旧方案和日期记录用于追溯，当前结论以 STATUS 为准。

最新接续点为 E4a 反馈与知识候选；E2/E3 真实质量验收仍按前置条件推进，结果以 STATUS 为准。

E3b 本机已实装，本地代码提交 a24c534；当前 GitHub 同步遇到 TLS 网络错误，尚未推送。远端仍不能视为含本批代码，详见 EXECUTION。
