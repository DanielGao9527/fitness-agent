# 当前架构与数据流

核对：2026-10-18。本文只描述当前实现，计划/质量缺口见[ROADMAP](ROADMAP.md)，验证见[STATUS](STATUS.md)。

Fitness Agent是FastAPI单体应用，原生网页与API同源。私人SQLite为v23/39表；公开知识、FTS索引及向量缓存独立保存。模型Key只在后端。

~~~text
浏览器 static/ → 认证会话 → api/ + schemas.py
                              ↓
                         services/ 业务编排
       ┌──────────────────────┼──────────────────────┐
       ↓                      ↓                      ↓
私人SQLite v23          公共知识/版本包          model/ + prompts/
档案/记录/记忆/反馈      FTS5、向量、RRF           理解/识别/证据判断
~~~

## 模块职责

| 模块 | 当前职责 |
| --- | --- |
| [app.py](../app.py)、[config.py](../config.py) | 生命周期、配置、模型/业务开关和访问模式 |
| [api/](../api)、[schemas.py](../schemas.py)、[security.py](../security.py) | 认证身份、HTTP接口、输入/输出契约；不信任正文user_id |
| [database.py](../database.py) | 私人表、事务和版本迁移 |
| [services/](../services) | 草稿/明确确认、幂等、档案事实、记忆、配餐、训练与知识治理 |
| [model/](../model)、[prompts/](../prompts) | 千问/阿里云网关、JSON任务、错误分类、额度和截止边界 |
| [rag/](../rag) | 审核资料读取、词法/向量召回、融合、来源和缓存校验 |
| [static/](../static) | 页面、核对卡、草稿保护、来源和失败状态 |
| [agent/](../agent) | 早期通用工具入口；当前主助手编排主要在services中 |
| [tests/](../tests)、[evals/](../evals/README.md)、[scripts/](../scripts/README.md) | 工程回归、质量协议/证据、离线/维护工具 |

没有将Hermes或LangGraph接成运行框架，没有多个自治Agent或任意系统命令执行。工作流由现有业务服务控制。

## 输入、规划与写入

文本、主动语音和饮食照片形成可核对草稿。营养估算保留范围、假设和来源，用户确认后才入账；幂等键、版本及上下文指纹阻止重复提交和迟到结果。

[meal_balance.py](../services/meal_balance.py)使用SciPy MILP/HiGHS，在审核候选、既有摄入、营养区间、禁忌、保留项和份量限制下联合配量。发布前独立复算；无解明确失败，不改目标凑结果，不把未知营养算零。只给食材和份量，不给做法。

`plan_foods.py`区分完整的正向偏好表达（如“喜欢米饭”“主食优先米饭”）与排除条件，对话新偏好还须对应本轮原话。可用偏好ID传入候选模型、餐单及MILP的按类别缺失惩罚，并由最终质量评分采用；它不会成为必选项，不放宽禁忌或营养约束，不改变未请求修改的餐次。未知食材或未完整解析的限制仍暂停。偏好解释策略与餐单指纹已版本化。

意图抽取不再发送饮食档案正文，防止模型将旧档案禁忌误抄为本轮新增条件；完整档案仍参与并发指纹，并在餐单/训练生成与发布核验时独立读取。模型偏好可为食材别名或完整正向表达，但必须有本轮正向原话支持；不接受凭档案或否定句推导的新偏好。

[training_recommendations.py](../services/training_recommendations.py)结合三/四/五分化、已确认器械、经验、时间和近七天实际完成，由审核目录/规则编排今天动作。建议不推进实际轮转，也不自动写workouts或计划。可比较的实际组次由[training_progression.py](../services/training_progression.py)提供受限负重提示；没有足够历史不猜首次重量。

轮转依据最近一次实际记录；该次完整覆盖多个方向时优先核对未覆盖方向，必要时比较七天加权负荷。今天/昨天的已识别负荷才进入现有动作排除集合；前天参与轮转，不代表已经恢复。用户指定方向、器械/依据校验及近期未知记录暂停仍生效。训练策略版本变化使旧建议失效。

## E1：中文混合检索

1. 知识条目是审核摘要块；jieba/FTS5负责词项匹配，不负责自动文档分块。
2. [rag_service.py](../rag/rag_service.py)支持jieba搜索分词＋FTS5/BM25；旧双字处理保留作显式基线。
3. [embeddings.py](../model/embeddings.py)区分document/query编码。[vector_store.py](../rag/vector_store.py)按模型身份、维度、模板和完整文本哈希匹配缓存；文档编码只由显式构建发起。
4. [hybrid.py](../rag/hybrid.py)进行精确余弦检索、双路候选及等权RRF；缺失/损坏/不匹配时明确降级，来源过期或撤回不能靠旧缓存命中。
5. [knowledge_runtime.py](../services/knowledge_runtime.py)绑定认证账号调用额度。一次RetrievalSnapshot固定候选；回答前按ID和内容重新验源，不能用另搜一遍替代原来源校验。

默认hybrid＋jieba，启动不自动付费建库。10月17日来源勘误曾使两块文本与旧向量不匹配，完整缓存检查在查询编码前明确降级词法；随后按用户新增1次授权补齐两块document编码，131条原向量字节不变，133/133精确匹配。工作副本经身份、维度、内容及SQLite完整性校验后原子替换本机公共缓存，目录vector_status=ready；向量按路径读取，原服务不重启。本次没有查询编码、LLM或正式排名，目录就绪不证明新的回答质量。独立克隆不分发运行缓存，缺缓存时账号和手动记录仍可用。公共文档缓存不存私人记忆或查询向量。

来源`scope`逐项区分原网页动作事实与项目先决条件、器械变式和保守执行规则，知识摘答及训练参考依据均展示适用范围。引体向上不再提示未经核对的下拉备选；直杆仍是项目目录选择，设备门槛不变，Mayo公共摘要不再声称转录指定直杆。语料/动作版本及来源版本已更新，原审核有效期不延长。

当前语料的87题AI审计及29题具体修改建议只保存在本地忽略目录，没有替换冻结QA或v1协议。另备的AI辅助验收版本仍是未激活输入建议，应用和质量服务不读取它；角色、原标签暴露、已有用户决定与本次AI判断分别记录，不能仅靠填reviewed/hash绕过现行前置。公开仓库只保存[匿名事实汇总](../evals/results/current-corpus-closeout-2026-10-17.json)。

[check_regression_candidate.py](../scripts/check_regression_candidate.py)是独立离线输入诊断入口，未接入应用/API。默认仅报告原v1状态；显式包目录与清单SHA-256同时提供后，复用load_cases、VectorStore.load与live=False查询缓存检查，验证248活动/12隔离输入、260映射、29项差异、审阅角色和当前来源/运行文件绑定。只在独立临时目录生成FTS，输出valid_inputs_not_evaluated，不计算排名/质量分数，不读Key或私人库，不改变production quality gate。原Recall@6/MRR@10实现和0.80等门槛未改；16道不足题单列未验收，包本体及审阅原材料只留本地。

离线报告另外输出unresolved_label_disputes：第037题单篇/全库解释仍待决定。输入结构合格不会清空语义争议，不推定候选标签正确或把证据不足题计为拒答成功。

## E2：个人记忆与事实生命周期

| 层 | 当前来源 | 用途 |
| --- | --- | --- |
| 会话热状态 | coach_conversations/coach_turns、待核对项、已确认本次条件 | 当前任务上下文和连续修改 |
| 长期档案/温记忆 | profiles、coach_memory_entries | 档案提供权威硬条件；受限用户笔记辅助交流，不覆盖器械、过敏等硬事实 |
| 历史证据 | coach_memory、coach_memory_digests | 本人核对旧原话/摘录按词项找回，携带来源/日期；主要由明确回忆触发 |

温记忆限每账号20条、合计1,200字，理解前读取；不是模型自主维护的Hermes文件。来源对话摘录只接受连续用户原话、本人身份和明确reviewed确认；助手答案不能成为私人事实。原话更正/删除使引用条目停用，手工更正作为新确认内容并清除旧绑定，账号删除级联清理。

器械由[equipment_memory.py](../services/equipment_memory.py)与[coach_reviews.py](../services/coach_reviews.py)校验来源、否定及范围；当前理解策略v19。范围不明由用户选择，不确定/未来/混合范围保留补问。有限表达修复不能吞掉未知要求或伪造来源，不能称为完整语义安全系统。

- 本次条件保存在当前会话，今天条件在coach_day_equipment，长期条件确认后写profiles.equipment。有效本次优先于今天，再用长期档案；临时条件在北京时间跨日后过期。
- [profile_facts.py](../services/profile_facts.py)用事务、递增器械版本及无私人口述的来源事件管理长期更正。更正、撤销或到期使旧理解、建议、替换和绑定计划失效；旧原话不能覆盖新档案。
- coach_memory_revisions保留空集合版本；原话/条目新增、更正、删除后递增。A→B→A或删空再添加都不能复活旧核对结果。
- v23的training_record_revisions仅保存认证用户、日期和递增次数。workouts触发器在实际完成记录新增/删除/相关字段变更时同事务增加版本；日期移走再移回、完成改计划再改回仍保留计数。无变化、幂等重复、纯计划和无关热量字段不增加计数；事务回滚与账号删除同步处理。
- [training_context.py](../services/training_context.py)将近七日实际记录及计数纳入上下文指纹，供理解/建议/绑定计划复用；独立有氧计划绑定当天计数。最后一条记录删除后仍保留日期计数，不复制第二套实际训练事实，也不把计数发给模型作内容。

测试入口为[test_memory_training_chain.py](../tests/test_memory_training_chain.py)，完整UI操作见[DEMOS](DEMOS.md)。私人数据按认证用户隔离，不进入公共知识库。

## E3：有界纠正与离线回放

[knowledge_answers.py](../services/knowledge_answers.py)复用同一混合检索器；[knowledge_policy.py](../services/knowledge_policy.py)限制最多两轮检索、一次受控改写，始终用原问题判断证据和引用。

主聊天、理解核对与知识入口共用[knowledge_scope.py](../services/knowledge_scope.py)的有限频率问句检查，“安排/推荐”不单独决定个人意图。询问前缀只用于分类，原问题和个人条件不改写；一般损伤预防不凭单字“伤”拒绝，有限近期伤情及原医疗边界继续生效。

零候选或证据不足时允许有限恢复；相同证据、越界/不完整改写、额度不足、来源撤回或到达预算后停止。数字/单位、时间、否定词只是有限规则校验，不是普遍语义等价证明。

[deadlines.py](../model/deadlines.py)提供45秒协作式截止，网络按剩余时间收紧、读取分块检查并拒绝迟到结果；同步调用不能被描述为可强制抢占。每次供应商调用仍按认证账号记账。

[replay_knowledge_loop.py](../scripts/replay_knowledge_loop.py)回放冻结候选、响应和虚拟时钟；[evaluate_knowledge_loop.py](../scripts/evaluate_knowledge_loop.py)区分准备、标签复核、显式预算真实执行和实际回答复核。单请求重检索不等于跨请求自学习。

两套既有E3 v1工具读取`evals/corpora/`内原字节语料快照，并在输出中明确标记历史语料。清单哈希、题目与标签保持冻结；当前更正语料不能冒用旧清单。应用与正式发布质量服务继续读取当前运行目录，不读取历史快照。

## E4：受控知识更新

[knowledge_feedback.py](../services/knowledge_feedback.py)把本人已完成回答/证据不足结果关联到私有反馈。候选第一版只接受当前有效public_web摘要的连续摘录，内容去重、版本绑定；不把私人口述、模型答案、任意网址或自由改写导入公共库。

1. 本人明确共享公共摘录及出处；审核角色由本机管理命令授予既有正式账号，每次接口重查。审核队列按白名单返回公共字段，不包含私人问题、回答或意见。
2. [knowledge_review.py](../services/knowledge_review.py)绑定来源、语料、候选版本和角色作工程检查与人工预审，预审通过仍不参与召回。
3. [knowledge_builds.py](../services/knowledge_builds.py)构建独立公共来源、FTS、精确内容匹配的向量和冻结dev回归。重复完整摘要去重；短摘录缺编码停止，不套用长摘要向量；失败保留诊断和既有版本。
4. [knowledge_releases.py](../services/knowledge_releases.py)登记不可变包哈希、提交/角色版本；[knowledge_quality.py](../services/knowledge_quality.py)依据固定协议和公开问题缓存生成服务端报告，不接收客户端上传的“通过”。
5. 正式质量要求标签独立复核、匹配缓存、两侧混合无降级、逐题Recall@6/MRR@10不退化及候选平均Recall@6≥0.80。这是门槛，不是已测成绩；当前正常协议仍pending_review。
6. [knowledge_publications.py](../services/knowledge_publications.py)绑定有效报告/批准，明确原子切换并递增序号；回滚仅选曾上线且仍有效版本或原审核库。来源、权限、协议、关联私人资料删除或包变化会失效受影响登记/批准/引用；同版本再启用不能复活旧答案。

在线检索、生成结束、保存和历史展示重查发布状态；固定配餐/训练依据仍用原审核目录。个人内容不进入公共版本，不自动提取Q&A或发布；当前实际在线指针是原审核库。合成演示协议只存在于独立测试进程，不是正常运行配置中的质量绕过开关。

## 运行和交付

[backups.py](../services/backups.py)负责一致性备份、清单与恢复校验；显式迁移入口为[migrate_memory.py](../scripts/migrate_memory.py)。本机工作区从外层原入口启动同一源码/数据库，不要求用户选旧版或增强版。

应用开发规则在docs，运行时模型约定在prompts，凭据/记录/上传图片不入Git或常规日志。访问、备份/迁移与部署见[OPERATIONS](OPERATIONS.md)、[ACCOUNTS](ACCOUNTS.md)、[DEPLOYMENT](DEPLOYMENT.md)；本轮演示文件不属于运行服务功能。
