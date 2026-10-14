# 安装、检查与维护工具

这些工具从仓库根目录运行。使用项目虚拟环境，确保操作目标是预期的数据文件和运行环境。

| 脚本 | 用途 |
| --- | --- |
| [setup.ps1](setup.ps1) | 在 Windows 创建虚拟环境，优先安装锁定依赖 |
| [start.ps1](start.ps1) | 启动本地服务，选择可用端口，并按参数启用 AI 能力 |
| [create_account.py](create_account.py) | 在私密交互终端创建账号，无需开放网页注册 |
| [preflight.py](preflight.py) | 检查访问配置、数据库、备份与调用次数限制，不发起模型请求或部署 |
| [backup.py](backup.py) | 创建一致性备份、校验备份、恢复到新路径 |
| [migrate_memory.py](migrate_memory.py) | 在服务已停且备份目标为全新私有路径时，将 v14/v15/v16/v17 私人库显式升级为 v18，并按原有字段投影验证业务行不变；不部署或自动恢复服务 |
| [evaluate_memory.py](evaluate_memory.py) | 默认固定 v1 的 100 组器械记忆场景；`--suite` 可选择独立冻结的 v2 新表达 24 条。标注接线与有预算真实评测分离，临时库、逐次记录、失败不重试；先使用 `--plan`，参见 [评测说明](../evals/memory/README.md) |
| [report_memory_evaluation.py](report_memory_evaluation.py) | 支持 --suite，按所选版本的实际题量合并互不重叠且语料/源码/型号一致的评测分片；拒绝重复题目和模拟/真实混用，生成逐指标与场景族报告 |
| [replay_memory_responses.py](replay_memory_responses.py) | 仅重放旧真实运行中的开发集结构化输出，对比当前后端处理；新增云调用0次，缺失响应单列，不评估新提示词效果或覆盖旧报告 |
| [rehearse_recovery.py](rehearse_recovery.py) | 在独立新目录演练恢复并核对数据库 |
| [build_knowledge_vectors.py](build_knowledge_vectors.py) | 默认只盘点审核公开块；显式 `--live` 才按预算调用云端构建文档向量 |
| [evaluate_retrieval.py](evaluate_retrieval.py) | 用项目 QA 对比词法、jieba、向量与 RRF；不调用答案模型 |
| `check_*.py` | 特定功能或真实供应商的手动检查工具，运行前阅读文件参数与适用范围 |

## 本地使用

```powershell
.\scripts\setup.ps1
.\scripts\start.ps1
```

当前代码及本机启动脚本默认以 `hybrid + jieba` 检索公开知识，启动脚本覆盖继承的旧检索环境变量；若没有服务端 Key 或匹配向量缓存，明确降级为 jieba 词法，账号和手动记录仍可用。`-UseLexicalBaseline` 仅供重放旧双字方案；启动不会自动付费构建文档向量。需要 AI 知识摘答还须显式 `-EnableKnowledge` 并配置私密 Key。

第一次正常启动生成数据库后，可执行只读配置检查：

```powershell
.\.venv\Scripts\python.exe scripts/preflight.py
```

查看备份工具参数：

```powershell
.\.venv\Scripts\python.exe scripts/backup.py --help
```

完整备份、恢复和共享环境检查步骤见[运行与恢复](../docs/OPERATIONS.md)。恢复工具只接受新目标路径，不自动覆盖现有数据库；备份包含私人记录，应保存在受限位置。

## 私密创建试用账号

先正常启动一次应用，让目标数据库初始化，再通过服务器私密终端运行：

```bash
sudo -u fitness /opt/fitness-agent/current/.venv/bin/python /opt/fitness-agent/current/scripts/create_account.py --database /var/lib/fitness-agent/fitness.sqlite3
```

按提示输入用户名及两次密码；密码输入时不显示。脚本要求已有数据库的绝对路径，不接受密码参数、环境变量或管道输入，不修改已有账号，也不改变网页注册开关。不要将密码粘贴到命令、聊天或日志。

## 真实接口检查

`check_*.py` 不属于自动发布时可随意全部运行的无费用测试集合。其中部分工具会连接真实模型服务，需要单独满足参数、凭据和调用次数约束。优先使用 [tests/](../tests) 中的合成测试定位问题，再按需要执行具体检查。

常规本地检查不能代替服务器上的依赖安装、HTTPS、代理、账号、重启持久化与恢复验证。服务器发布流程见[部署说明](../docs/DEPLOYMENT.md)。

## 外部中文检索对照

`prepare_t2_pilot.py` 下载并校验固定版本的公开 T2Retrieval 输入，固定种子抽样 100 个问题，保留全部标注正相关文档后用随机干扰补到 1,000 篇。`pyarrow==23.0.1` 仅为准备数据的可选依赖，不加入应用运行依赖。

```powershell
python -m pip install --only-binary=:all: --no-deps pyarrow==23.0.1
python scripts/prepare_t2_pilot.py
python scripts/evaluate_t2_pilot.py --live --max-calls 120 --max-input-chars 1500000 --max-document-chars 6000
```

各检索方法统一使用每篇文档的前 6,000 个字符；原下载文件、原题和相关性标注不改写。报告记录截断篇数，保留可能位于前缀之外的证据造成的局限。语料、索引和向量全部位于独立的 `artifacts/public-eval/t2/`，不会进入 Fitness 知识或私人数据库。

离线公开文档编码每成功一批就写入校验过的批次缓存；超时不自动重试。操作者再次明确运行 `--live` 才继续缺失批次，已有批次免重算。完整向量准备后去掉 `--live` 可复现排名。缓存绑定模型身份、维度、角色、文本和顺序，不能用于未经授权存储私人在线查询。

该实验缩小了候选库，且没有编码全库 118,605 篇文档，不是官方 MTEB 成绩，也不直接代表 Fitness 个性化问答效果。不得把外部与内部题集分数混在一起。

## 显式构建公共文档向量

以下命令使用当前进程中的[向量配置](../docs/CONFIGURATION.md)，不自动加载 `.env`。先盘点当前有效文档数量与输入字符数，不调用模型，也不打开私人业务数据库：

```powershell
.\.venv\Scripts\python.exe scripts/build_knowledge_vectors.py
```

在服务端凭据、型号、调用授权和预算就绪后，显式构建：

```powershell
.\.venv\Scripts\python.exe scripts/build_knowledge_vectors.py --live --max-calls 50 --max-input-chars 500000
```

默认输出到应用词法索引同目录的 `knowledge-vectors.sqlite3`，可用 `--output` 指定独立文件、`--date YYYY-MM-DD` 固定资料有效日期。若指定自定义输出，必须确保应用确实配置读取同一缓存，不能把离线产物存在别处就认为已接入。脚本拒绝输出与私人数据库、来源文件或词法索引使用同一路径；已有其他类型数据库也不会被覆盖。

向量按模型身份隔离，未变化文档复用缓存；编码失败不发布半份索引。`--max-calls` 和 `--max-input-chars` 是本次进程限制，不是金额保证；真实 token 用量以返回字段和供应商账单为准。不要将这个带 `--live` 的命令加入每次启动、自动测试或无条件发布步骤。

## 检索离线评测

题集及其限制见 [evals/README.md](../evals/README.md)。默认只运行 `dev`，公开语料的全文字段与有效日期对所有方法一致；不把正确领域标签作为查询过滤条件。

不调用模型的词法对照：

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_retrieval.py --lexical-only --split dev --date 2026-09-24
```

允许有界云端编码，比较同一型号下原词法、jieba、纯向量与两种 RRF：

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_retrieval.py --live --split dev --date 2026-09-24 --max-calls 100 --max-input-chars 500000
```

型号来自配置，也可用 `--model` 指定；`--output` 默认 `artifacts/retrieval-eval`，保存评测专用词法索引、文档向量、公开测试问题的向量缓存和结果。它不会将这些问题写进生产知识库，也不读取私人记录。去掉 `--live` 可以复用完整的已有向量缓存；缓存不足时停止，不偷偷付费补齐。公开测试问题的离线缓存与生产用户查询不落库是两条不同边界。

只用 `dev` 选择分词与模型参数，配置冻结后才运行 `--split test`；如测试问题向量未缓存，仍需显式 `--live` 和预算。`--split all` 会同时读取两部分，不用于反复调参。语料或题目更改应形成新版本；当前固定日期只适用于这份题集的来源快照。

结果中的 `cached_retrieval_*` 是复用问题向量后的本地检索时间，不包含线上编码往返；真实编码耗时、调用数和 token 用量另列。证据不足题不计入可答 Recall/MRR/nDCG 分母，检索非空不能证明回答正确。脚本不调用聊天模型，因此不能据此声称问答准确率、拒答准确率或幻觉率改善。更完整的实现与实验结论见[检索增强说明](../docs/archive/2026-09/RETRIEVAL_ENHANCEMENT.md)。

## 器械记忆真实模型单场景验收

在本机私有环境已有 `DASHSCOPE_API_KEY` 时，可显式运行 `.\.venv\Scripts\python.exe scripts/verify_equipment_memory_live.py`。脚本用临时库和合成账号，限制最多一次千问理解调用，然后检查“确认哑铃→新会话建议→档案改徒手→旧来源不再检索→重新建议”的最终状态；退出即删除临时库，不打印密钥或模型原始输出。训练动作由受约束规则生成。它是单场景联调，不是记忆准确率评测，不能代替独立多轮题集或浏览器验收。
