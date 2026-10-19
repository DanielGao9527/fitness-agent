# 本地交付、验证与学习入口

核对：2026-10-19。当前四项能力已有本地工程实现；完整知识覆盖、真实媒体、正式公共知识发布及公网/实体设备并未全部验收。不接微调、不增加功能/框架、不扩库。事实和范围见[STATUS](STATUS.md)、[PRODUCT](PRODUCT.md)。

## 四项能力对照

| 用户目标 | 已实现 | 实际验证与证据 | 仍缺验证/限制 |
| --- | --- | --- | --- |
| 中文混合检索 | jieba＋FTS5/BM25、阿里云Embedding、RRF、内容/身份匹配缓存、来源复核及词法降级 | T2固定100题/1000文档，Recall@6由72.11%至84.16%（＋12.05个百分点），纯向量88.51%仍更高。[全方案](../evals/results/t2-pilot-2026-09-25.json)；当前文档缓存133/133匹配 | 这是相关文档召回，不是QA正确率、官方全库成绩或领域泛化保证。最新87题只完成前三题，032超时；83未测。 |
| 多轮理解与安全写入 | 文字/ASR/照片→结构化草稿→核对→明确保存；Pydantic、幂等键、版本和上下文指纹；连续纠正/过期拒绝/隔离 | 三类输入以固定识别结果贯穿纠正、最终份量、过时确认拒绝、重启与幂等保存，页面及相关回归已有；[综合演示](../evals/results/final-demos-2026-10-16.json) | 固定ASR/图片替身不等于真实供应商或实体设备。最少真实ASR1＋文字解析1＋照片分析1还缺实际输入和新预算。 |
| 受约束多餐规划 | LLM有限候选＋SciPy MILP/HiGHS联合配量；已吃、营养、禁忌、保留项和份量上限；独立复算/无解失败，修改指定餐次使旧方案失效 | 本地配餐/轮转相关43项、独立复算/无解/不入账回归；小林米饭偏好真实单场景及用户手动两餐通过，原应用拒绝/连接失败保留。[观测](../evals/results/preference-training-fix-2026-10-17.json) | 有限审核食材/约束与模型估算，不是完整食物成分库或医疗饮食系统；没有新的大规模配餐质量分数。 |
| 个人记忆与证据治理 | 会话/档案/笔记/历史的来源、范围、更正、删除/版本；实际训练影响当天动作；有界重检索；公共反馈→预审→构建/质量/批准/切换/回滚；私人/公共隔离 | 记忆同题条件采用17/33→28/33，但2次建议退步、确认17/18→13/18下降保留。[同题报告](../evals/results/memory-pair-2026-10-16.json)；跨会话/实际训练/更正/重启/隔离13项＋2次真实理解[小验收](../evals/results/memory-training-closeout-2026-10-16.json)；循环dev两侧11/12、无净收益；治理隔离演示已有 | 非任意事实自动记忆、非Hermes框架、非跨请求自优化。正式v1独立审核仍pending；没有实际公共候选发布，不能把模拟批准/回滚当正式发布验收。 |

## 最后依赖与续测

原87题批次实际4/85次、12400字符：013/014/031经planner AI复核正确，032为超时非语义拒答；83未测，15不足均未测，无整体通过分数。旧结果封存，不重考前4题。

新续测准备为83题/最多81判断/260508字符、单题至多一次/输出2048tokens、固定first6、0向量/理解/媒体/现场检索。83题预演与16项守卫通过，新授权文件不存在且未启动。须明确授权才能采用“单次连接/读取超时记失败跳过、连续3次才停”，只有完整HTTP响应重置，零调用题不影响计数；鉴权/额度/其他供应商/来源/预算/隔离异常立即停。保留原63/72及15不足标准。[最新实际汇总](../evals/results/knowledge-coverage-2026-10-19.json)

真实媒体最小三次缺输入/预算。正式知识独立审核/实际发布、公网托管和实体手机另案，不伪勾完成，也不阻塞本地工程交付。

## 学习顺序：每条只读核心链路

| 顺序 | 核心文件阅读顺序 | 最少对应测试/证据 |
| --- | --- | --- |
| 1 中文检索 | `rag/rag_service.py → model/embeddings.py → rag/vector_store.py → rag/hybrid.py → services/knowledge_answers.py`：先区分分词、向量化、融合和回答验源 | `tests/test_knowledge_answers.py`，T2[方法](archive/2026-09/T2_RETRIEVAL_EVALUATION.md)与全方案结果；理解召回不等于答案正确 |
| 2 安全写入 | `api/coach.py → services/coach.py → services/coach_reviews.py → schemas.py → services/meal_drafts.py / meal_consent.py → services/records.py`：追踪草稿、确认与版本指纹 | `tests/test_coach_reviews.py`、`test_meal_consent.py`、`test_meal_text.py`、`test_meal_photos.py`，结合[五类演示](DEMOS.md)理解真实/替身区别 |
| 3 多餐约束 | `services/meal_plans.py → meal_balance.py → meal_plan_nutrition.py → meal_plan_actions.py`：区分LLM候选、MILP配量和独立复算 | `tests/test_meal_balance.py`、`test_meal_plan_nutrition.py`、`test_meal_plan_actions.py`，看无解、禁忌优先与指定修改反例 |
| 4 记忆/证据 | `services/equipment_memory.py → profile_facts.py → coach_memory.py → training_context.py → training_recommendations.py`；再读`knowledge_answers.py / knowledge_policy.py → knowledge_feedback.py → knowledge_review.py → knowledge_quality.py → knowledge_publications.py` | `tests/test_memory_training_chain.py`、`test_profile_fact_versions.py`、`test_knowledge_publications.py`，对照记忆报告、[循环评分](../evals/results/e3-dev-scored-2026-10-16.json)与正式[质量协议](../evals/release_quality/v1/README.md) |

先从[ARCHITECTURE](ARCHITECTURE.md)看总入口，再按表追请求和测试，不另起新教程。`docs`控制开发边界，`prompts`控制模型任务，数据库保存私人事实，RAG保存公共资料。

## 运行与交付边界

本机外层fitness-agent目录执行`./scripts/start.ps1`，使用原账号库和原虚拟环境，端口8765；当前由保留终端会话持有，关闭所有者会停止服务，不宣称开机常驻。缺Key仍能手动记录。备份、迁移及恢复见[OPERATIONS](OPERATIONS.md)。

代码/测试/必要技术文档和匿名汇总可入Git；原请求、输出、授权、DB、日志、录像/照片及个人简历不入Git。原报告与失败不覆盖；本轮改写前七份全文另存本地私密快照。推送后须核对新SHA的GitHub检查，不用历史绿色替代；不合main、不部署。
