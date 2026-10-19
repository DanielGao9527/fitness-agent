# 当前架构

核对：2026-10-19。Python/FastAPI单体，原生网页与API同源。私人SQLite为v23/39表，知识源、FTS索引和向量缓存独立保存；模型Key只留服务器。

## 请求与写入

`static/ → api/ → schemas.py → services/ → database.py`

`api`从认证会话取得身份；Pydantic验证结构，业务服务验证归属、约束及版本。AI经`model/`返回候选或草稿，不直接写事实。保存/计划替换须用户明确操作，并核对幂等键、版本和上下文指纹。

## 四项能力

| 能力 | 当前实现与责任 |
| --- | --- |
| 中文检索 | `rag/rag_service.py`校验审核摘要及jieba/FTS5索引；`model/embeddings.py`负责云编码；`rag/vector_store.py`校验模型、维度、模板和内容哈希；`rag/hybrid.py`进行RRF融合。`services/knowledge_runtime.py`绑定生效知识版本。启动不自动付费建库。 |
| 多轮安全写入 | `services/coach.py`编排，`coach_reviews.py`理解/核对，`meal_drafts.py`与`meal_consent.py`管理草稿和确认；文字/ASR/照片经`model/`。请求重试、过时上下文和跨账号结果不得重复入账。 |
| 多餐规划 | `services/meal_plans.py`组织有限候选；`meal_balance.py`调用SciPy MILP/HiGHS求解，`meal_plan_nutrition.py`等独立复算；`meal_plan_actions.py`处理指定餐次修改与失效。不是全量热量数据库。 |
| 记忆/证据 | `equipment_memory.py`处理本次/今天/长期器械；`profile_facts.py`和`coach_memory.py`管理来源、范围和版本；`training_context.py`读取实际历史，`training_recommendations.py`编排当天动作。`knowledge_answers.py`及`knowledge_policy.py`限制重检索；反馈/预审/构建/质量/发布分别由对应knowledge服务管理。 |

## 失效与隔离

更正、撤销、删除、到期或来源变化使受影响旧答案/建议失效；v23记录实际训练更正计数，内容改回原样也不能复活旧建议。权威档案由用户确认，历史模型答案不当事实。冷历史主要在明确回忆请求中按需取回，未实现Hermes运行框架或任意事实自动记忆。

个人反馈先留本人范围；公共候选仅取有效公共摘要的连续摘录，显式提交后才供审核。质量报告由服务端绑定版本，人工批准和切换分开；来源/内容/权限变化使批准失效。现行正式协议仍pending_review，没有实际候选发布。

## 本地运行与诊断

外层`fitness-agent/scripts/start.ps1`复用原虚拟环境/原账号库，指向本增强源码；独立克隆使用本仓库`./scripts/start.ps1`。当前用保留的终端会话运行原入口，不能宣称开机自启或全天候服务。既有脱离启动的进程曾消失，退出原因未知。

知识问答单次协作式预算45秒，HTTP默认20秒/连接最多5秒，0自动重试。当前客户端统一映射超时子类，旧5秒超时阶段无法追溯。续测适配器在隔离评测中保留异常子类、响应头/完整包体、原始字节及usage。实际末题记录ConnectTimeout、无响应头、约5.109秒；不能用它反推原032阶段或更细根因。业务提示词、模型及超时值未改。

本地追加入口验收在隔离账号/数据库中使用133条真实文档向量与173条DEV问题缓存，经实际RRF、认证接口及验源；Qwen HTTP结果用固定MockTransport，连接/读取异常在传输层注入，不能称新增真实模型端到端验证。正式87问题缓存只做身份/内容/值检查，没有运行留出排名。

本地标签诊断与生产质量协议分开：按用户委托复用87题AI审计和已封存248题分组副本；既有缓存诊断的来源、问题/文档向量、标签与参数完全匹配，直接引用旧结果，未新增排名。AI已见标签/输出，3项本人认可仅覆盖展示的证据对应；两者都不能替代正式v1要求的质量凭据或触发实际发布。

验证边界见[STATUS](STATUS.md)，核心阅读路径见[DELIVERY](DELIVERY.md)。
