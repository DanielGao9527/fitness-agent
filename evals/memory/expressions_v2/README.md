# 新表达与更正场景 v2

日期：2026-09-29。24 条手写合成场景，8 个场景族，每族 3 条；不是从公开数据抽样，也尚未经过独立人工审核。不把它称为外部基准或真实用户准确率。

相对 v1 单一器械句式，这批加入背景噪声、未声明时段、口语更正、不确定来源、未来安排、混合时段、完整否定和跨日场地变化。工作流步骤复用已有评测器，表达与期望逐条编写；没有用当前检索结果或模型得分筛题。

开发 12 条、留出 12 条按整个场景族划分。v1 的 100 条输入、划分和报告保持原样。manifest 保存哈希、划分和待独立复核状态；真实调用前需要复核标注，修改标注应发布新版本，不原地改题。

~~~powershell
python scripts/evaluate_memory.py --suite evals/memory/expressions_v2 --split all --plan
python scripts/evaluate_memory.py --suite evals/memory/expressions_v2 --mode annotated --split dev --output evals/results/memory-expressions-dev-new
~~~

这 24 条最多需要 27 次理解调用，开发集最多 15 次。plan 不调用模型；annotated 用标签构造响应，只验证接口与业务处理，不测模型理解质量。留出集尚不用于修复或调参；新提示词的真实效果仍待有明确预算的 live 验证。

结果保留失败、误判、未执行和供应商错误。不能把新增题目、标注模式通过或混合不同题集的分数写成记忆提升百分比。原始输出、校验后事实和最终建议继续分别判分。

## 已执行记录

2026-09-29 仅运行开发集标注模式：[首次 11/12](../../results/memory_expressions_v2_dev_20260929/summary.json)，修复后[同题 12/12](../../results/memory_expressions_v2_dev_fixed_20260929/summary.json)。题目与期望未修改，失败报告保留；新增供应商调用为 0。

失败发生在“只有瑜伽垫，其他器械都没有”的已标注完整摘录被后端拒绝，修复限定为完整列表的否定结尾识别。通过结果验证从待确认草稿到最终动作的业务处理，不验证模型能否提取这些事实。留出集 12 条未运行，独立人工复核及真实模型调用待办。
