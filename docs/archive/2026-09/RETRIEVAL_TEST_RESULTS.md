# 混合检索增强：测试结果与基线对照

核验日期：2026-09-24（Asia/Shanghai），2026-09-25 增补公开适配器与续跑测试。工作分支：`codex/fitness-hybrid-retrieval`。

**最终检索专项回归为 213 项通过；全套检查仍有 2 项在原始基线也能复现的训练测试失败，不能称为“全套全绿”。** 本文记录工程测试，不把合成向量或模型替身的结果当作真实语义检索提升。

## 最终定向回归

用户恢复实施后，针对最终 Python 源码重新执行以下 7 个文件；不是沿用此前被中断、没有最终汇总的那次运行。

```powershell
& 'C:/Users/Gao/Desktop/RAG/fitness-agent/.venv/Scripts/python.exe' -m pytest tests/test_embeddings.py tests/test_hybrid_retrieval.py tests/test_hybrid_api.py tests/test_retrieval_evaluation.py tests/test_knowledge.py tests/test_knowledge_answers.py tests/test_coach_knowledge.py
```

执行目录：`C:/Users/Gao/Desktop/RAG/fitness-agent/data/runtime/remote-auth`。

结果：**213 passed, 2 warnings in 76.38s**，退出码 `0`。两条警告来自既有 Starlette/httpx、AnyIO 接口弃用提示。

随后新增公开评测适配器与离线续跑测试，单独运行 `tests/test_public_retrieval_evaluation.py`：**33 passed in 15.27s**。覆盖双字/jieba 在相同语料下与生产检索排名一致、五方案共享同样的 6,000 字符前缀、RRF 接线、批次缓存重启复用、身份/角色/文本/顺序隔离、损坏拒绝与失败不自动重试。与上面合计为 246 项相关检查，不冒充重新执行了全部应用测试。

相关前端替身检查 `node tests/knowledge-flow-unit.cjs` 与 `node tests/knowledge-ask-unit.cjs` 均通过；它们不代表已执行真实浏览器或手机测试。

覆盖范围：

- embedding 接口输入输出、批量结果、异常与调用预算；测试使用合成响应，不访问真实云模型。
- 文档向量缓存身份、内容版本、维度、失效来源及重启读取；未建缓存时明确降级，不在在线请求里补建。
- 词法与向量两路融合、去重、RRF 排名及公开检索元数据。
- 认证 API 的用户与全局额度、账号隔离、来源撤回、失败计费，以及免费入口不触发向量调用。
- 同一知识请求共享候选快照，最终按选中 ID 核验来源；HTTP 重试不重复检索或回答调用。
- 已调用 embedding 但随后因适用范围暂停时，如实记录 `model_called` 和 `retrieval.embedding_called`。
- 多相关项 Recall、MRR、nDCG、重复候选与排名截断；无答案题独立计数。
- 评测题组和相关证据在 dev/test 间隔离，问题规范化去重，非法引用拒绝，离线查询缓存绑定模型身份、维度和问题顺序。
- 向量构建脚本的路径冲突保护，以及 dry-run 不构造供应商、不打开私人业务数据库。

## 全套执行结果

此前在同一增强分支执行过一次完整 `python -m pytest`：

```text
3 failed, 1629 passed, 2 warnings in 768.23s
```

即共执行 **1632 项**。之后没有重跑整套；对失败项和最终增强范围做了定向验证。

| 首轮失败 | 原因与后续核验 |
| --- | --- |
| `test_hybrid_api.py::test_disabled_knowledge_answer_stops_before_paid_embedding` | 新测试夹具将 `create_knowledge_model` 无条件替换成 fake，绕过了实际的“功能未启用”检查。该用例恢复真实配置工厂；禁用状态在构造/调用云模型之前返回 503。修正后单独重跑通过，也包含在上述最终 213 项通过结果内。 |
| `test_training_today.py::test_auto_understanding_requires_consent_then_is_idempotent_and_no_actual_write` | 既有测试第 100 行期望训练自动理解在未确认配餐条件时返回 409，实际 200；当前分支与原始基线都失败。 |
| `test_training_today.py::test_auto_understanding_does_not_bypass_questions_or_changed_conditions[revoked]` | 既有测试第 139 行期望撤回配餐确认后返回 409，实际 200；当前分支与原始基线都失败。 |

训练两项是原有测试预期与现有配餐确认适用范围之间的差异；本次没有通过修改业务规则、删除用例或放宽断言将它们隐藏。应另行结合既有产品约定处理。

## 原始基线复现证据

基线提交完整 SHA：`07b6ec2ea0f6ce8b5605700b7a32cb8d170fd00d`。

从 `git archive HEAD` 导出该提交的 Git 跟踪源码，解压到独立目录后，用同一个外层 Python 环境运行上述两条训练用例。导出前检查了跟踪路径，没有复制私人运行目录、业务 SQLite、密钥或日志；发现的 `.env.example` 是仓库配置模板。

本机证据位置：

- 归档：`C:/Users/Gao/Desktop/RAG/fitness-agent/data/runtime/remote-auth/artifacts/baseline-check-07b6ec2.zip`
- 归档 SHA-256：`8262959745e66b44a282c61ab4e2f0c1ba9876f1b055cb190dde60c88d1281f9`
- 独立源码：`C:/Users/Gao/Desktop/RAG/fitness-agent/data/runtime/remote-auth/artifacts/baseline-check-07b6ec2/`

在独立源码目录执行：

```powershell
& 'C:/Users/Gao/Desktop/RAG/fitness-agent/.venv/Scripts/python.exe' -m pytest tests/test_training_today.py::test_auto_understanding_requires_consent_then_is_idempotent_and_no_actual_write 'tests/test_training_today.py::test_auto_understanding_does_not_bypass_questions_or_changed_conditions[revoked]' -q
```

结果：**2 项均失败**，仍分别位于第 100、139 行，均为预期 `409`、实际 `200`。当前增强目录中定向重跑同两项得到相同结果。因此可以确认这两项失败在增强前已经存在，不是本次混合检索引入。

上述归档是本机检查产物，不要求提交版本控制。提交 SHA、命令与失败位置可用于重新生成相同基线。测试数据与数据库来自临时夹具，未读取真实个人记录；本文不代表真实模型调用或公开基准评测已完成。
