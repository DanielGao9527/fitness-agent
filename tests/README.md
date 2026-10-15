# 测试与复现

实际执行结果看 [STATUS](../docs/STATUS.md)。以下是运行入口与覆盖范围，不因脚本存在就视为已经验收。

## 后端回归

在源码仓库根目录使用项目虚拟环境：

~~~powershell
.\.venv\Scripts\python.exe -m pytest
~~~

Linux／macOS 使用 .venv/bin/python。已有本机工作区可复用外层虚拟环境；独立克隆先按 [安装说明](../README.md#本地启动) 创建环境。常规回归使用临时合成数据库与模型替身，不要求真实 API Key；不要将测试指向私人账号库。

| 范围 | 主要入口与检查 |
| --- | --- |
| 账号、记录与草稿 | 认证和跨账号隔离、确认保存、幂等、输入校验、并发、重启与过期结果 |
| 配餐与训练 | 既有摄入、营养区间、有限禁忌、份量和时间约束；无可行解、实际记录与建议分离 |
| 中文混合检索 | test_embeddings.py、test_hybrid_retrieval.py、test_hybrid_api.py；接口角色/顺序、RRF、缓存版本、额度、来源变化与词法降级 |
| 知识分流与有限重试 | test_coach_knowledge.py、test_knowledge_answers.py、test_knowledge_loop.py；一次候选快照、原问题复核、引用失效、停止条件 |
| 零命中恢复与策略回放 | test_knowledge_recovery.py、test_knowledge_replay.py；数字/单位、时间/否定、重复候选重排、额度、截止时间、迟到输出、冻结输入和无原文报告 |
| 私人反馈与摘录候选 | test_knowledge_workbench.py；回答绑定、幂等、并发、内容/来源去重、显式核对、来源失效、隔离、修订、删除、重启与旧库迁移 |
| 公共摘录预审 | test_knowledge_review.py；分享同意、服务端权限/游客隔离、字段白名单、版本与语料指纹、离线检查、人工预审、修订/来源/权限撤销失效、并发、删除、重启及 v19→v20 迁移 |
| 候选知识版本 | test_knowledge_builds.py、test_knowledge_build_gate.py；完整包/内容去重/缓存身份、缺短摘录编码停止、实际 Top 6 不刷分、只评 dev、变更/故障/并发、损坏文件、失败诊断及线上库保持 |
| 版本登记与发布准备 | test_knowledge_releases.py；哈希/来源组成、角色和字段边界、并发/重启、撤回/删除/权限及文件变化失效、临时故障、非空恢复、v20→v21 迁移；不测试为真实质量通过 |
| 批准、在线版本与回滚 | test_knowledge_publications.py；服务端质量/非退化与最低覆盖、手工批准、原子指针/幂等/并发、账号/角色撤销、真实聊天引用/回滚/序号 ABA、非空恢复与 v21→v22；合成向量只验证工程 |
| 会话／温／冷记忆 | test_coach_memory.py、test_coach_warm_memory.py；保存、读取、更正、删除、来源变化与本人隔离 |
| 偏好原话与集合版本 | test_memory_provenance.py；显式核对、连续原话、来源撤销、集合 A→B→A、更正解绑、并发、隔离、重启及 v17→v18 非空旧笔记保留 |
| 器械事实生命周期 | test_profile_fact_versions.py、test_equipment_memory_scopes.py、test_equipment_clarification.py；本次/今天/长期、显式确认、到期、A→B→A、并发、未知条件和 v17 迁移 |
| 评测完整性 | test_memory_evaluation.py、test_memory_suite_versions.py、test_memory_response_replay.py；冻结哈希、场景族划分、标准答案隔离、预算、失败保留和重复分片拒绝 |
| 历史基线兼容性工具 | test_memory_baseline.py；归档越界/链接拒绝、环境密钥清除、外网阻止、输出保护、失败留证及源码改写检测；缺协议不计成质量零分 |
| 同题版本对照 | test_memory_pair.py；共同分母、增益/退步、缺字段排除、未运行/失效/异常不出提升率、复核与预算前置、原标签不外发、首个供应商错误停止及离线密钥清除 |
| 断点与有限补跑 | test_memory_pair_resume.py；保留答错场景、排除基础设施中断、初始缓存不混入失败题、全局尝试预算、默认停错及超时/连接各自授权 |
| 本机启动兼容 | test_workspace_startup.py；原入口转增强源码、保留原库、无密钥/手动模式及端口占用。仅对应 Windows 工作区执行，独立克隆和其他平台跳过 |
| 分享与恢复 | test_public_release.py、备份/迁移相关测试；访问边界、库结构、原表指纹和恢复持久化 |

训练理解与一般知识不依赖饮食确认；配餐确认缺失或调用期间撤销时，只保留待核对草稿。取得确认后可以复用原理解，长期事实与实际记录仍须相应的明确操作。

指定单文件示例：

~~~powershell
.\.venv\Scripts\python.exe -m pytest tests/test_public_release.py
~~~

## 前端单元

安装 Node.js 后，从源码仓库运行任意 *-unit.cjs，例如：

~~~powershell
node tests/coach-memory-unit.cjs
node tests/knowledge-flow-unit.cjs
~~~

测试使用 DOM、媒体与接口替身，检查内容转义、重复请求、迟到响应、取消和账号切换。不会打开真实麦克风或调用真实模型。

## 隔离浏览器流程

需要 Node.js 可解析 Playwright，以及本机 Chrome。只对合成测试服务执行浏览器套件，不对真实账号服务造数据。

记忆范围、更正和冲突验证：

~~~powershell
.\.venv\Scripts\python.exe tests/ui_fixture_server.py --fact-versions
~~~

该进程监听 127.0.0.1:8772，使用临时库、模型替身和词法检索。在另一终端依次执行：

~~~powershell
node tests/browser-equipment-scopes.cjs
node tests/browser-profile-facts.cjs
node tests/browser-memory-provenance.cjs
~~~

检查未知范围不保存、显式确认、本次/今天/长期来源、刷新补问、双页面 A→B→A 冲突与 390/320 宽度。结束后停止本次测试服务。

用户笔记页面另用 ui_fixture_server.py --memory（8771）与 browser-coach-memory.cjs。一般业务页面用 --coach（8767），当前有效套件列表见 [browser-current.json](browser-current.json)。旧页面脚本不全是当前验收入口；测试端口被占用时先确认归属，不停止未知服务。

反馈工作台使用 ui_fixture_server.py --coach（8767）与 node tests/browser-knowledge-workbench.cjs，覆盖原回答选择、连续摘录核对、修订/搁置、文本转义、两账号隔离、刷新持久化、级联删除及 1440/390/320 宽度。测试服务为词法检索和模型替身，不向真实账号库写入数据。

同一隔离服务可运行 node tests/browser-knowledge-review.cjs；需要 FITNESS_TEST_PYTHON 指向项目 Python（未指定时复用本工作区外层虚拟环境）。它仅通过本机管理命令为合成测试账号授予/撤回权限，验证本人分享、两账号字段隔离、工程检查、人工预审、修订失效、撤回后再提交和窄屏。只连接 artifacts/ui-coach-review-test.sqlite3，不操作正常账号库。

构建页面使用 tests/ui_fixture_server.py --coach --knowledge-build（8767）与 node tests/browser-knowledge-builds.cjs。新增开关生成明确标识的合成文档向量缓存，聊天仍走词法；不是实际 Embedding 或质量成绩。流程验证明确选择/确认、完整重复摘要去重、独立包、缩短摘录缺编码时暂停、旧包保留及 1440/390/320。只在 artifacts 写合成测试库/包，不操作正常账号库。

## 质量评测与解释边界

[260 条检索 QA](../evals/README.md)、[100 组记忆 v1](../evals/memory/README.md) 和 [24 条新表达 v2](../evals/memory/expressions_v2/README.md) 都记录了来源、划分和局限。标注模式只验证业务接线；真实模式须显式预算，保留失败与未运行项。评测命令见 [scripts](../scripts/README.md)。

旧 test_knowledge.py 的 16 条 Top 3 检查属于已知回归案例，不是未见留出集。自建数据、外部 T2 抽样和真实模型结果分别报告；Recall/MRR/nDCG 不等于最终回答准确率，固定旧响应回放不等于新提示词效果。

工程测试和模拟浏览器通过不证明营养测量准确、任意口语理解、供应商实时可用、实体手机权限或公网部署完成。

发布浏览器使用独立合成服务：tests/ui_fixture_server.py --coach --knowledge-build --knowledge-publish，再运行 node tests/browser-knowledge-builds.cjs --publication。包含批准/切换/回滚各自确认；没有生产配置绕过开关，不连接正常账号库。
