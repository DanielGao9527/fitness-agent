# Fitness Retrieval Pipeline 核查与开发交接

日期：2026-09-25。交接对象：开发任务「增强Fitness项目并辅导学习」。

## 1. 核查范围与结论

实际开发仓库是本文件所在的 `fitness-agent/data/runtime/remote-auth/`，当前分支为 `codex/fitness-hybrid-retrieval`。外层 `fitness-agent/` 是另一份较旧代码，不要在外层另改一套。核查时开发分支存在未提交改动；接手前先检查工作树，保留已有工作，不覆盖其他正在进行的修改。

**结论：**当前开发代码具备可选的 `jieba + embedding + RRF` 公共知识混合检索，但默认配置仍是 `lexical + bigram`。公共知识当前有效的 133 个 section 已在本机 FTS 和默认向量缓存中各覆盖 133/133；私人聊天、训练和饮食历史没有进入这两套索引。不能由本机缓存推断运行中服务或线上服务已启用 hybrid。此次核查没有改代码、部署或调用真实模型；相关现有测试 81 项通过。

## 2. 已做到的能力（保留并回归）

| 能力 | 当前实际实现 | 代码入口 |
| --- | --- | --- |
| 公共知识 source of truth | `data/knowledge/sources.json` 中人工审核的 `sections`，每节一个稳定 `source_id:section_id` | `rag/catalog.py`、`rag/rag_service.py` |
| FTS5 词法检索 | 独立 SQLite 的 `chunks` 与 `search_index`，`MATCH`、BM25、至少 1～2 个词项重合；来源状态和期限过滤 | `rag/rag_service.py` |
| 双字 / jieba | 默认连续中文相邻双字；显式 `FITNESS_RETRIEVAL_TOKENIZER=jieba` 时用 `cut_for_search` 替换词项化，不是两套词法结果同时融合 | `config.py`、`rag/rag_service.py` |
| 公共资料向量 | 阿里云 `text-embedding-v4` 默认 1024 维；文档输入为完整标题＋摘要＋关键词，查询输入为原始完整问题；独立 SQLite 缓存，显式离线构建 | `model/embeddings.py`、`rag/vector_store.py`、`scripts/build_knowledge_vectors.py` |
| 可选混合检索 | `FITNESS_RETRIEVAL_MODE=hybrid` 时词法与余弦检索各最多取 20 条，用等权 RRF（k=60）融合；向量缺失或失败可降级词法 | `app.py`、`rag/hybrid.py` |
| 知识问答接线 | 独立 `/api/knowledge/ask` 与主聊天知识分支使用候选快照；最多 6 条证据送受约束摘答，按 ID 复核来源 | `api/knowledge.py`、`api/coach.py`、`services/knowledge_answers.py`、`services/coach_knowledge.py` |
| 私人历史初步找回 | E2a 对本人已核对的旧聊天做有界 SQL 扫描与 jieba 词项匹配，带日期和来源 ID，用于“上次”等表达的理解上下文 | `services/coach_memory.py`、`services/coach_reviews.py` |
| 结构化事实读取 | 训练、饮食、档案按认证用户及日期从私人 SQLite 查询；实际记录需用户明确保存 | `database.py`、`services/records.py`、`services/coach.py` |

## 3. 尚未做到或只做到一部分

1. **默认并非用户选择的向量＋jieba。** `config.py` 和 `.env.example` 默认仍为 `lexical + bigram`。不要把“可配置”表述成“已经在当前服务启用”。独立 `/api/agent/chat` 仍显式取 lexical；配餐、训练主要按固定 chunk ID 取证，也不走混合排序。
2. **没有私人记录的检索管道。** 聊天、训练、饮食写入业务表后，不自动 chunk、FTS 或 embedding。没有旧私人数据 backfill。E2a 只是确认过的聊天原话按需扫描，不是私人向量库；训练记录中的自由文本也不在公共知识索引中。
3. **公共知识不是运行时 chunking。** `sections` 由人工准备，没有固定 token 切块或语义自动切块。短训练记录没有与原始记录关联的 chunk 表。是否需要自动切块，应以真实资料长度和检索失误为依据。
4. **没有检索前 query rewrite。** 最近轮次和 E2a 历史片段可供意图理解，但公共知识检索仍使用原问题。没有完整指代消解、时间解析或历史事实纠正机制。
5. **没有独立 reranker。** RRF 后模型从最多 6 条证据选引用，这不等于候选重排模型。
6. **安全分流不完整。** 现有疼痛、伤病、医疗等词法范围拦截和业务约束暂停需要保留；尚无经过验证的风险分级、red flag 检测及医疗资料优先路由。不可把现有校验称为完整语义安全系统。
7. **检索可观测性不完整。** 返回值可含模式、排名、RRF 分数、降级原因，主聊天知识结果可保存在对话 JSON；缺少受控的逐路候选、耗时、改写依据、用户反馈和离线回放轨迹。私人原话和向量不得进入常规日志。
8. **上线状态未核实。** 本机默认公共向量缓存与当前有效资料匹配，但没有核查正在运行的进程或线上环境变量、代码版本与真实问答结果。

## 4. 开发顺序与具体交付

### 第一阶段：把当前检索状态说清并完成配置验收

- 先检查当前分支、工作树、现有 E1/E2a 改动和 `docs/EXECUTION.md`、`STATUS.md`、`ROADMAP.md`，避免覆盖进行中的工作。
- 核对目标环境的实际 `retrieval_mode`、`retrieval_tokenizer`、知识问答开关、模型身份和向量缓存状态。只报告安全的状态字段，不打印密钥、个人内容或完整向量。
- 用户已经选择“向量＋jieba”作为后续方向。为这一组合准备明确、可复现的启动配置和验证步骤；如果要改默认值，先确保无密钥时账号与记录功能仍可运行，并让知识检索降级状态明确。不要把配置文件变化等同于已部署；未经用户授权，不部署公网或改运行中服务。
- 用认证接口验证同一公共问题在词法模式与显式 hybrid＋jieba 模式下的 `mode`、`tokenizer`、`embedding_called`、来源 ID 与降级原因。确认 `/api/agent/chat` 的 lexical 特例是保留兼容还是纳入统一知识问答，避免界面与文档宣称不一致。

### 第二阶段：按 E2 路线补私人历史，而不是混入公共知识库

- 先明确哪些**已确认**的私人文本需要检索：聊天原话、训练自由文本、饮食描述各自的用途、用户可见性、删除和更正语义。档案与组数、次数、体重、日期等结构化事实继续以 SQL 为准。
- 设计私人 source of truth → 派生索引的稳定 ID、`user_id` 隔离、来源类型、记录版本与时间字段。若采用 chunking，定义长文本切分规则、短记录单块规则、chunk → 原记录映射；删除索引后应能从原记录重建。
- 实施前给出迁移与回填方案，涵盖现有旧记录、增删改同步、失败可重试、重启持久化及索引完整性核对。不得把未经确认的模型输出自动晋升为用户事实，不得把私人文本放进公共知识 JSON/FTS/向量库。
- 在用户明确提到“上次”等指代时，先利用 E2a 可追溯证据；后续 query rewrite 需保留原问题、改写依据与时间边界，不能把历史肩伤擅自判定为已恢复或自动当作当前约束。

### 第三阶段：质量与安全增强，以评测决定是否接入

- 为知识、私人历史、结构化事实建立明确分流；疼痛、伤病等情形保守暂停个人处方。风险路由和 reranker 先定义可测失败案例，再决定模型与候选数量；不可只因目标架构图中存在就加一层调用。
- 检索轨迹至少能在受控测试中比较原问题、改写问题、词法/向量候选、融合结果、最终证据和耗时；生产记录遵守隐私规则，禁止把密钥、上传图片、私人原话或完整向量写入常规日志。
- 用现有内部 QA 与冻结的外部 T2 抽样对照验证。保留已测结果：外部抽样 Recall@6 双字 72.11%、jieba 70.74%、纯向量 88.51%、双字混合 83.29%、jieba 混合 84.16%。向量＋jieba 是用户选定的方向，不得宣称它在该抽样中最优，也不按目标百分比倒推挑题。

## 5. 验收与边界

- 给出两条真实调用链：①新增训练/聊天信息写入、确认、索引或不索引；②一般知识问题从入口到最终证据与回答。分别标记默认、显式 hybrid＋jieba、向量故障降级。
- 覆盖跨用户隔离、旧数据回填、编辑/删除后不再召回、重复请求、重启持久化、来源过期/撤回、缺失密钥、模型失败和伤病范围拦截。真实模型验证与模拟测试分别记录。
- 只把已实现、已接线、已验证和已部署的状态分别写入相应文档；完成里程碑后同步 `EXECUTION.md`、`STATUS.md`、`ARCHITECTURE.md`、`ROADMAP.md`。未执行的验证不得勾选。
- 保持用户已定业务边界：饮食只给食材与份量，不提供伤病训练处方；AI 输出仍是建议或待确认草稿，记录写入与计划替换必须经用户明确操作。

## 6. 起点文件

`app.py`、`config.py`、`rag/rag_service.py`、`rag/hybrid.py`、`rag/vector_store.py`、`model/embeddings.py`、`services/knowledge_answers.py`、`services/coach_memory.py`、`services/coach_reviews.py`、`database.py`、`services/records.py`；状态与顺序以本仓库 `docs/EXECUTION.md` 和 `docs/ROADMAP.md` 为准。

## 7. 交接后的复核进展（2026-09-25）

针对第一阶段，已在 `scripts/start.ps1` 增加 `-EnableHybridRetrieval`，它只为本次本机启动设置 `hybrid + jieba`；`.env.example` 仍留安全的无密钥默认值。此前 `RETRIEVAL_ENHANCEMENT.md` 的运行示例误写 `bigram`，现已改为 `jieba`。`app.py` 对显式 hybrid 但无密钥的情况不再创建不可用提供者，目录和查询分别显示 `embedding_not_configured`，并保持账号、记录路径可用。旧 `/api/agent/chat` 保持词法兼容，不能拿它验收主聊天知识检索。

新增合成数据的认证接口测试覆盖旧双字词法与显式 jieba 混合的模式、分词、问题编码、来源 ID、额度和无密钥回退；检索及知识问答相关 5 个测试模块共 158 项通过（其中 `tests/test_hybrid_api.py` 22 项）。当前 Shell 环境的安全状态字段为 `lexical + bigram`、知识摘答关闭、默认向量型号 `text-embedding-v4`；本地有效公共 section 及默认型号缓存读取得到 133/133。未核查运行中服务环境、未调用真实模型、未发布。第二、三阶段所列能力仍待实施与验收。

## 8. 用户后续调整（同日）

用户明确要求代码默认采用向量＋jieba，旧双字只保留评测基线，并开始 E3 有界检索纠正。因此本文件第 1～4 节是**调整前的核查快照**，其中“默认 lexical + bigram”不再是当前源码事实。当前 `config.py` 与 `.env.example` 默认为 `hybrid + jieba`；无密钥时退到 jieba 词法，运行中的服务和真实效果仍未核查。E3a 的合成可回放流程已接入证据不足时最多一次改写重检，详见 `docs/ROADMAP.md`、`docs/ARCHITECTURE.md` 和 `tests/test_knowledge_loop.py`；完整离线回放、自优化和知识沉淀仍未交付。
