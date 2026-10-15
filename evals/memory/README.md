# 器械记忆评测 v1

默认入口仍运行本页 v1。另有冻结的 [新表达 v2（24 条）](expressions_v2/README.md)，通过 `--suite evals/memory/expressions_v2` 选择；版本各自保留输入、划分和结果，不混成一个成绩。

2026-10-16 已完成 v2 的旧/新版真实同题首轮，方法见[配对协议](PAIRED.md)、证据见[聚合结果](../results/memory-pair-2026-10-16.json)。它与本页 v1 的旧报告分开，不混分母，也不改变 v1 的创建审核状态。

这是自建合成业务集，100组场景来自20个场景族、每族5种器械变体。它不是100个独立真实用户样本，也不是公开基准成绩；当前没有独立人工复核。结构与标注由本轮开发助手编写，`human_independent_review=false`。

所有输入和期望在第一次真实模型运行前保存到 `cases.jsonl`，SHA-256 固定在 `manifest.json`。按整个场景族划分开发60组、留出40组，变体不跨集合。不能为了让分数更高而删除失败题或改标签；标注有问题时应另发数据版本并说明。`build_cases.py` 保留编写来源，已有版本拒绝覆盖。

本集只验证器械事实的短链路：声明/确认 → 跨会话采用 → 更正、到期、删除、重启或隔离。没有长达数月的自然长对话、噪声历史检索、其他个人偏好或医疗推荐。每族仅5组，结果应同时列出场景族，不能把100组当作总体质量的随机样本来外推。

## 来源与复用

- [LongMemEval](https://github.com/xiaowu0162/LongMemEval)的评测维度包含信息提取、多会话推理、知识更新、时间推理和拒答；本集借鉴维度拆分，没有复制其题目，也没有运行官方数据或宣称官方成绩。
- [LoCoMo](https://github.com/snap-research/locomo)提供长时多会话对话评测；它与本项目需要用户确认、数据库修改及到期的工作流不同，后续适配需单独报告。
- 接线复用既有 FastAPI API、TestClient、Pydantic 与 QwenTextModel。没有引入第二套生产记忆系统或额外模型裁判。

## 三层判分

| 指标 | 通过条件 | 不代表什么 |
| --- | --- | --- |
| raw_candidate_exact | 模型直接输出的范围、连续器械摘录、完整来源原话和消息版本符合标签 | 不等于任意语义同义表达正确率 |
| accepted_candidate_exact | 经生产业务校验后的候选与标签一致 | 程序修正不算模型自己提取正确 |
| manual_confirmation_required / no_unconfirmed_* | 需要确认的候选仍为待确认；未确认不改长期或当天事实 | 不把自动保存算成功 |
| clarification / no_false_candidate | 不确定、未来或混合范围得到明确补问，且不产生记忆候选 | 502/超时即使未写入，也不算成功澄清 |
| equipment_adoption | 最终建议的器械文本与来源符合标签 | 不等于整份训练方案专业质量 |
| exercise_resources | 最终动作非空，且每项所需器械属于标签允许集合 | 不等于动作最优或个体处方 |
| concurrent_stale_rejection / superseded_advice_stale | 更正期间旧草稿拒绝；旧建议标为过期 | 不以文字相同代替版本相同 |
| account_isolation / no_actual_record_writes | 不得跨账号访问、删除；评测不产生实际饮食/训练记录 | 不声称所有生产安全风险均被覆盖 |

器械文本只去首尾空白和句末标点后精确比较；没有调用生产 `equipment()` 解析器生成期望或作为裁判，防止同一错误同时出现在实现与判分中。同义但标签未列出的摘录会被记为不匹配，应查看逐题结果，不能将该指标改称通用“记忆准确率”。需要用户选择范围的场景显式模拟这一操作，其余场景不会根据标准答案替模型修正范围。

每个指标单独记录分子和分母。API失败、超时、未得到候选均保留，不剔除难题。全场景通过必须每个适用检查通过；不建议把它作为唯一质量指标。尚无匹配的旧版本对照，不输出“提升了X%”。

## 运行

在实际源码根目录运行，`python` 指向项目 `.venv/Scripts/python.exe`：

```text
python scripts/evaluate_memory.py --split all --plan
python scripts/evaluate_memory.py --mode annotated --split all --output evals/results/memory-wiring-new
python scripts/evaluate_memory.py --mode live --split dev --max-calls 60 --output evals/results/memory-live-dev-new
python scripts/evaluate_memory.py --mode live --split holdout --max-calls 55 --output evals/results/memory-live-holdout-new
python scripts/report_memory_evaluation.py evals/results/memory-live-dev-new evals/results/memory-live-holdout-new --output evals/results/memory-report-new
```

`annotated` 用标注生成模型替身，只验证回放接线；即使全通过也不是模型效果。`live` 复用现有 `DASHSCOPE_API_KEY`，使用应用默认型号，也可显式 `--model`。标准答案不进入真实模型的系统提示词或消息。使用临时合成账号/私人库和本地公共词法检索，不读取正式用户数据、不调用向量模型、不需要浏览器或正式服务启动。

全100组最多115次理解调用，开发60次、留出55次。先 `--plan` 查看实际选择；`--case` 可重复用于诊断指定题目。真实模式必须显式给1–200次上限；失败算一次，不自动重试；连续3次供应商错误停止后续场景。额度是调用次数而非金额，不收集供应商token和账单，不估造费用。

每次必须使用新的输出目录，不覆盖旧结果，也不自动重跑不确定的已发请求。真实运行的`calls.jsonl`发送前记attempt，返回后记结果/失败，进程中断仍可核对；它包含合成模型输出，不包含凭据或完整HTTP响应。标注模式没有供应商调用，不生成此调用台账。合并工具拒绝重复题目或混合模型/源码/题集版本，未运行题目会单列，不从分母悄悄丢弃。

- `run.json`：语料哈希、选择ID、型号、调用预算、生产与判分源码哈希。
- `cases.jsonl`：逐步骤观测和每项判分。
- `summary.json`：每指标/场景族的分子分母、尚未运行ID、调用次数和供应商调用耗时。

失败不据此自动修改生产提示词、接受错误候选或放宽时间规则。保留本轮基线，再用开发集定位和修正；留出集不能拿来反复调参。
