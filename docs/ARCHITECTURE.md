# 当前架构与数据流

核对：2026-10-14。本文描述已接通的实现；未完成设计统一见 [ROADMAP](ROADMAP.md)。

Fitness Agent 是 FastAPI 单体应用，原生网页与 API 同源，私人数据保存在 SQLite。后端按需调用千问与阿里云向量服务，浏览器不持有供应商密钥。

~~~text
浏览器 static/ → 认证会话 → api/ + schemas.py
                              ↓
                         services/ 业务编排
       ┌──────────────────────┼─────────────────────┐
       ↓                      ↓                     ↓
私人 SQLite v18         公共资料与检索           model/ + prompts/
档案/记录/草稿/记忆       FTS5 + 向量缓存          理解/估算/证据判断
~~~

## 模块职责

| 入口 | 职责 |
| --- | --- |
| [app.py](../app.py)、[config.py](../config.py) | 生命周期、模型开关、访问模式和配置 |
| [api/](../api)、[schemas.py](../schemas.py)、[security.py](../security.py) | 路由、结构校验、会话认证；不信任正文 user_id |
| [database.py](../database.py) | 私人表、事务和迁移；当前 v18、25 表 |
| [services/](../services) | 对话、确认、事实有效性、记录、配餐和训练规则 |
| [model/](../model)、[prompts/](../prompts) | 供应商调用、结构化输出、错误分类和应用提示词 |
| [rag/](../rag) | 审核公共资料、词法/向量召回、来源校验 |
| [static/](../static) | 页面、核对卡、草稿保护、来源与失败状态 |
| [agent/](../agent) | 早期通用工具接口；当前助手编排主要在 services |
| [evals/](../evals)、[tests/](../tests) | 质量对照与工程回归，使用隔离数据 |

## 业务主流程

**输入与保存：** 文本、主动录音或饮食照片形成待核对草稿，模型估算保留范围和假设。用户明确操作后保存实际记录；幂等键防重复，版本及上下文指纹拦截旧结果。模型返回后再次核验身份、输入版本和相关事实。

**餐次规划：** 模型从审核目录选候选，services/meal_balance.py 用 SciPy MILP/HiGHS 联合求解剩余餐次，发布前独立复算营养区间、禁忌、份量和修改范围。无解明确失败，未知营养不当作零，不更改目标来凑结果。饮食建议不自动入账。

**训练建议：** services/training_recommendations.py 与训练目录/编排服务结合三/四/五分化、器械、经验、时间及本人近七天实际训练，只生成当天动作。实际记录和建议分开，不猜首次重量、不将建议当完成。

## E1：公共知识双路检索

1. 原知识条目仍是人工审核的摘要块；jieba/FTS5 负责词项匹配，不负责文档分块。
2. [rag/rag_service.py](../rag/rag_service.py) 提供 jieba 搜索分词＋FTS5/BM25。旧双字处理只保留作可配置基线。
3. [model/embeddings.py](../model/embeddings.py) 区分 document/query 编码。[rag/vector_store.py](../rag/vector_store.py) 缓存绑定模型、维度、模板与内容哈希，只有显式构建才写文档向量。
4. [rag/hybrid.py](../rag/hybrid.py) 做精确余弦检索及等权 RRF。向量缺失、损坏或不匹配时明确降级；来源撤回/过期不能靠旧缓存继续命中。
5. [services/knowledge_runtime.py](../services/knowledge_runtime.py) 绑定认证用户额度；一次 RetrievalSnapshot 固定候选，回答前按 ID 和内容重新验源，不重复检索替代验源。

默认 hybrid＋jieba；启动不自动付费建库。脚本与配置见 [CONFIGURATION](CONFIGURATION.md) 和 [scripts](../scripts/README.md)。公共文档向量缓存不保存私人记忆或查询向量。

本机日常数据路径旁的公共缓存已于 10 月 14 日接通：复用源码运行目录中已有的 133 块缓存，离线复核模型身份、当前内容和向量一致性，没有新增编码调用。独立克隆不分发运行缓存，需要按脚本显式构建；缓存未准备时走已标明的词法降级。

## E2：个人记忆和事实生命周期

| 层 | 当前内容和入口 | 使用方式 |
| --- | --- | --- |
| 热层 | 当前会话、待核对项、已确认条件，coach/coach_reviews | 对当前任务提供上下文；受版本和有效期约束 |
| 温层 | 档案与 coach_memory_entries | 档案是硬条件的权威来源；用户笔记限每账号 20 条、合计 1,200 字，理解调用前读取 |
| 冷层 | coach_memory、coach_memory_digests | 本人已核对旧原话、经确认的原文摘录按需词项找回，携带日期/来源 |

冷证据主要由“上次”等明确回忆触发；不是任意问题自动搜索全部历史。摘要候选最多拼接三条已核对原话，没有模型自由总结。普通公共知识摘答不读取私人温记忆。

器械是当前实现最完整的事实链路：

- 一次理解可返回带原话和版本的候选。[equipment_memory.py](../services/equipment_memory.py) 校验来源、否定与时间范围；没有范围依据为 unspecified，须用户选择。
- 不确定、未来或混合范围通过 EquipmentNeedsInput 进入补问；[coach_reviews.py](../services/coach_reviews.py) 的当前策略 v16 保留原话/来源与人工范围校验。器械解析允许“其他器械都没有”等完整的末尾否定子句，不增加任何器械，也不吞掉后续未知限制。伪造来源仍拒绝，不把有限规则称为完整语义安全。
- 用户确认后，本次保存在会话、今天保存在 coach_day_equipment、长期写入既有 profiles.equipment。有效本次条件优先于当天，再使用长期档案；本次/当天最晚北京时间当日结束。
- [profile_facts.py](../services/profile_facts.py) 统一长期事实事务与递增版本，profile_fact_events 保存来源元数据；当天条件有独立版本。A→B→A 不使旧结果复活。
- [training_context.py](../services/training_context.py) 与建议/理解指纹核对版本，档案更正、到期或撤销会使相关旧理解、建议和动作替换失效。
- 删除当天来源对话撤销当天值并保留版本计数；单独确认的长期档案需另行撤销。临时器械不进入跨会话冷证据或摘要。

私人记忆、更正、删除均按认证用户隔离。自动模型写记忆、私人向量回忆、自动晋升/降级和普遍偏好语义提取尚未实现。

### N3：偏好记忆的来源与撤销

- 沿用 coach_memory_entries；v18 增加用途和原话来源，旧笔记保留为 general_note，不自动推断类型。明确分类的交流偏好限一个，个人术语可多条，共享 20 条/1,200 字上限。
- from-turn 接口只接受本人对话中的连续用户原话，核对对话、消息及记忆集合版本和 reviewed=true。不接受助手回答、改写或正文 user_id，不调用模型。
- coach_memory_revisions 在新增、更正、删除后递增，空列表也保留计数；理解指纹包含该版本，防止空集 A→B→A 后旧核对或生成结果复活。
- SQLite 触发器在原话更正或对话删除时撤销引用条目，并递增条目及集合版本；原话改回去也不自动恢复。warm_memory 只发送有效条目，失效条目留在本人列表供核对或删除。
- 修改来源条目是用户手工确认的新内容，清除原话绑定。删除原对话会停用独立保存的记忆，清除摘录需删除记忆条目；数据库删除账号时，条目和计数按外键级联清理。

记忆只辅助理解交流和术语，不作为硬条件来源，也不保证固定摘答或规则训练建议会改变文字风格。独立标注复核及新真实模型质量仍待验收。

## E3a：有限检索纠正

[services/knowledge_answers.py](../services/knowledge_answers.py) 先检索，再让模型从候选中受约束选证据。首轮有候选但不足且给出合法改写时，最多追加一轮；第二轮仍判断原问题。相同候选、越界改写、额度不足或达到上限就停止。

零候选目前直接返回 insufficient，恢复及独立策略回放仍待 E3b。响应只保存允许公开的候选 ID、阶段和停止原因，不把私人改写原文写入公共日志。每次编码与证据判断计入额度，这不等于金额预算。

## 主聊天分流与一致性

api/coach.py → services/coach_knowledge.py → services/knowledge_answers.py 复用公共摘答。request_kind/action 表示当前消息动作，intent 保留原会话任务；一般知识与配餐/训练分流，混合或不明确请求先澄清。

知识结果保存在 coach_turns.response；version 检查所有消息并发变化，workflow_version 排除只读知识轮次，避免无故修改原计划。未核对条件仍保留，知识轮次不能作为新计划请求。

重复 HTTP 请求复用生成中、成功或失败状态，进程中断不假定外部调用未发生。返回和历史展示均重新检查条件与引用；知识摘答上传问题和公共摘要，不上传完整私人档案。超范围、配置缺失、限额、证据不足、服务异常和来源变化有明确状态。

## 部署与数据边界

当前代码支持 v18 备份/迁移。工作区启动脚本转接到增强仓库，显式绑定原虚拟环境和原数据库；外层 app.py 也转接到同一实现。端口占用时失败，不悄悄另开一个服务。已有模型密钥时本机启动入口默认启用 AI，ManualOnly 会从子进程环境移除模型密钥并关闭模型能力，正常环境在启动结束后恢复。

9 月 29 日，本机原库在独立备份及副本演练后从 v13 升到 v17；18 张原有表在迁移和启动后指纹不变。10 月 14 日在副本演练和独立备份后再升级为 v18、25 表，原 24 表的所有原有字段指纹保持一致；localhost:8765 已运行新增功能。deploy/check_release.py 比较实际库和候选代码结构，不匹配时阻止自动升级；Git 同步不代表公网部署，详见 [OPERATIONS](OPERATIONS.md)。

共享模式要求 HTTPS、Host 白名单、安全 Cookie、单 worker 和私密数据/密钥目录。账号/游客隔离与清理详见 [ACCOUNTS](ACCOUNTS.md)，来源和隐私分别见 [KNOWLEDGE_SOURCES](KNOWLEDGE_SOURCES.md)、[PRIVACY](PRIVACY.md)。

E4 的反馈、候选评测、人工审批和知识版本发布尚未接通；本架构图不把未来模块画成现有实现。
