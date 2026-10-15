# 历史版本同题对照

`scripts/evaluate_memory_pair.py` 复用已有 Scenario、模型网关和真实 API，比较增强前 `07b6ec2` 与指定已提交版本。它不修改旧版源码、不补入新记忆接口，也不使用日常账号数据库。默认是标注接线，不能用于模型质量结论。

## 输入与控制条件

默认使用冻结新表达 v2：24 组、8 族，开发/留出各 12。题目来自项目自建合成场景，用户复核记录不使它成为外部或盲测基准。真实运行前核对独立于模型结果的人工标签记录，绑定整个题集哈希和逐题哈希；正常冻结 manifest 不原地修改。复核表和个人工作材料仅保存在被忽略的 artifacts 中。

两版分别导出 Git 源码，独立进程、临时账号和 SQLite；同一 Python/依赖、型号、业务日期、种子档案、原话及用户确认操作。对照时关闭 auto_apply，两边都使用明确确认；确认仍只调用该版原有 API。旧版不接受的 equipment_scope 不发送，也不代它写长期档案或当天条件。正常应用的自动应用行为没有因此修改。

评测器、判分代码和依赖工具另行冻结为同一份，禁止两边在不同工作区代码上运行。每版及工具快照均核对文件哈希。知识和动作目录也应核对语义内容相同；不同提交的换行不应误作资料变化。模型系统提示词和记忆处理代码随版本变化，因而收益是整条增强工作流的对照，不能单独归因为某一行提示词或算法。

真实模式只发送应用正常构造的提示词与事实，不发送标签/参考答案。只调用理解文本模型，本地词法检索，不调用 Embedding。标注模式会从标签构造两版各自支持的假响应，因此不输出质量百分比或提升率。

## 共同判分与能力诊断

| 指标 | 共同标准 |
| --- | --- |
| raw_equipment_value | 旧版 training.equipment / 新版 equipment_memory.value 或合法训练条件，严格匹配已标注摘录；冲突字段不择优选取 |
| accepted_equipment_value | API 成功，业务校验后器械摘录匹配 |
| manual_confirmation_required / confirmation_applied | 两版明确确认流程可用；不是模型自主保存成功率 |
| condition_adoption | 最终建议 API 成功、状态 ready、器械文本匹配，且动作非空、所需器械均在允许集合内 |
| clarification / no_false_equipment | 应补问时明确补问，且不产生器械条件 |
| no_unconfirmed_profile_write / no_actual_record_writes | 未确认不改档案，不写实际饮食/训练记录 |

器械只去首尾空白/句末标点后精确匹配；不覆盖标签未列出的同义表达，不等于整份训练方案的专业质量。来源、范围、当天版本和旧建议 stale 字段另记诊断，旧版缺字段不计零分。不同能力下不报告一个全场景通过率差值。

每个共同指标仅在相同案例/步骤上配对，保留分子、分母、错→对和对→错。完整、已复核且源码未变的真实批次才输出两侧比例和百分点差；标注接线、未运行、环境错误或供应商失败导致的残缺批次不产生提升率。开发与留出结果应分别列出，不能只公布较好的一组；留出不用来调参。

## 运行

先查看计划，不读取密钥或调用模型：

```powershell
python scripts/evaluate_memory_pair.py --split all --plan
python scripts/evaluate_memory_pair.py --split all --prepare-review --output artifacts/local-delivery/memory-review-new
python scripts/evaluate_memory_pair.py --split dev --mode annotated --output artifacts/memory-pair-wiring-new
```

人工逐条核对 REVIEW.md 后，在 review.json 保留真实复核人/日期及决定；代码只生成 pending，不自动填写审核。收到明确调用授权、匹配记录、可用密钥及联网权限后执行：

```powershell
python scripts/evaluate_memory_pair.py --split all --mode live --model qwen3.6-plus --max-calls 54 --review-file artifacts/local-delivery/memory-review-new/review.json --output artifacts/memory-pair-live-new
```

完整 24 组每版最多 27 次，两版 54；仅开发 12 组每版最多 15 次，两版 30。这里是尝试次数上限，不是金额。输出目录必须全新且位于 artifacts；不会覆盖或自动续跑失败批次。首个供应商错误即停止后续请求和另一版本。计费/网络错误不得自动换模型或重试。

每次派发前同步落盘 attempt，失败也计数；保留逐题检查、原始结构化响应、失败类别、未运行项、源码/工具哈希和复核凭据哈希。发生中断时检查两侧 `*-calls.jsonl`，不能因没有模型返回就从预算中删掉尝试。新的明确重跑授权也必须保留旧批次账本。

报告记录供应商调用耗时；case_seconds 包含临时库/账号准备，不能当成线上响应 SLA。当前网关不采集 token 或账单，费用填未采集。两版按旧版→新版的固定顺序运行，未随机交错；共享当前依赖，不重建历史虚拟环境。需要在结果中披露这些限制。

## 明确授权后的断点续测

`resume_memory_pair.py` 复用完整且收到供应商响应的场景，答错的场景也保留；只补基础设施失败/中断及尚未运行的场景。多步场景若中途失败，可能重做该未完成场景，尝试仍全部记账。不能拿它反复抽取更好的答案。

```powershell
python scripts/resume_memory_pair.py artifacts/memory-pair-live-new --plan
```

它要求同一题集/逐题划分、原提交、型号、Python、复核记录及全部冻结评测器哈希。继续使用原两个应用版本，新增的续测工具不修改模型提示词或判分函数；不允许修了代码后把旧成绩直接拼接。完整保留原批次和每个阶段账本，`total_attempts` 包含已核对的历史尝试，新阶段调用次数单列。

新预算必须显式覆盖所有既有尝试与待补上限。默认仍遇错停止；`--timeout-restarts` 最多 2 轮，需要另行明确授权；`--retry-connections` 也须明确授权才可把连接中断纳入同一有限轮数。鉴权、额度和其他错误不能靠这些开关重试；预算不足就停止，不扩大上限。首批本机阻断等额外账本可用 `--budget-extra` 只计入一次，已合并的续测记录不能重复叠加。

```powershell
# 仅为命令形式；次数与补跑授权必须先落实，不能作为日常自动任务。
python scripts/resume_memory_pair.py artifacts/memory-pair-live-new --review-file artifacts/local-delivery/memory-review-new/review.json --total-limit 60 --output artifacts/memory-pair-continuation-new
```

跨阶段继续不具备连续运行的延迟可比性。保留每阶段真实耗时，不把断点拼接报告当作线上耗时提升或稳定 SLA。

## 修复开发集的离线回放

`scripts/replay_memory_pair_dev.py` 只读取完整同题报告中属于 dev 的增强版结构化响应，经当前 API 和相同确认操作回放。开发集所有有响应的场景都保留，答错也不重抽；留出响应不读取、不回放，缺响应另列。使用临时账号/数据库、固定日期、auto_apply=false 和原有判分要求，不替换模型输出。

```powershell
python scripts/replay_memory_pair_dev.py artifacts/memory-pair-live-complete-20261016/report.json --output artifacts/memory-scope-dev-replay-new
```

输出限定在 artifacts 的新目录；应用默认实例的导入也绑定临时库并清除运行环境覆盖/凭据。记录原报告及当前源码哈希、零云调用、外网阻断计数、缺失项及共同检查的计数。所有质量比例和百分点保持 null，标记 real_model_quality=false。这能验证后台修复，不能评价新提示词、泛化或训练专业质量，也不能覆盖原 de32154 的真实成绩。

v17 仅在原话/器械已校验、单条明确声明、没有其他操作或安全缺项、全文剩余文字符合有限核对表达时，修复冗余 which_training，仍须用户选择范围并明确确认。未知文字/动作替换/未支持结构/其他待办继续阻断；它不是通用意图分类或完整语义安全系统。新提示词质量须另外授权真实验证。

## 与正式验收的关系

这个对照检查器械理解、确认、跨会话采用及更正等已有链路，不评估所有偏好、长期自然对话或医疗安全，也不覆盖 E3 检索循环。E3 的单/两轮语义对照及知识发布正常标签复核仍按 ROADMAP 分开推进。失败的网络调用不算旧版准确率差；接线通过也不算模型增强效果。
