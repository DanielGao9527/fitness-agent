# 记忆增强 E2a：按需回忆已核对的旧对话

状态：2026-09-25 已实现本阶段代码与针对性测试；尚未部署服务器。E2a 是完整长短期记忆计划中的第一段，不等于自动摘要、自动写长期档案或三层自动迁移已经交付。

## 当前可演示的行为

1. 原有短期记忆继续由本次对话的 `coach_turns`、已核对的 `constraints`/`meal_context`/`training_context` 负责；长期档案和真实训练记录继续从各自的业务表读取。
2. 新增 `services/coach_memory.py`：只从**当前已认证账号**的历史对话中寻找经 `reviewed_understanding` 或 `applied_understanding` 确认过的原话。候选有原对话 ID、轮次和业务日期；待核对消息不进入结果。每次最多扫描现有产品上限内的 2,000 轮，返回不超过 3 条给理解步骤，独立搜索接口最多 5 条。
3. 用户这次说“上次／之前／说过”等，并且旧消息有相关词时，`CoachReviewService.snapshot` 才附上 `historical_evidence`。模型提示词把它定义为旧对话证据；原话提取与训练修改仍只能依据本次待核对消息。资料在模型调用前后都参与上下文指纹，旧消息被删或变化会拒绝使用迟到结果。
4. 登录后可用 `POST /api/coach/conversations/memory/search`，请求正文 `{"query":"哑铃"}`，查看同一账号已核对的历史片段。接口不接受请求正文中的 `user_id`；查询不放 URL，避免进入普通访问路径日志。删除旧对话后，搜索立即不再返回它；重启后从现有 SQLite 历史恢复，不建第二份个人资料库。

演示可用两个对话：在第一个对话说“在家，只有哑铃”并完成理解核对；在第二个对话输入“上次说过的哑铃训练，我想继续”，触发理解时会看到第一个对话的证据。**当前条件仍需本轮明确确认**；旧健康限制、器械和偏好不会自动跨会话成为计划硬约束。只有原话里确实出现相近词才会命中；“上次那个”这类没有锚点的指代仍应澄清。

## 为什么这样接

Fitness 已有用户隔离的会话表和确认状态。借鉴 [Hermes 的精简持久记忆与按需搜索历史会话](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)、[ScholarForge 的 pending/confirmed/session/historical 分层思路](https://github.com/redmaplewww/ScholarForge)，但保持 Fitness 现有 Python/FastAPI/SQLite 和用户确认边界。没有直接复制其运行时或把其他项目的用户文件放进公共 RAG。用户提供的 [yunpai-customer-service](https://github.com/redmaplewww/yunpai-customer-service) 可作为后续知识沉淀与回放架构的参考；本轮未取得其源码全文，因此不宣称复用具体实现。

现有数据库最多 50 个对话、每个 40 轮，第一段采用有上限的本人历史扫描和 jieba 词项匹配，不新增表或数据库版本，避免对当前 v14 备份与发布流程制造未经验证的迁移。检索结果只是“用户以前说过”，不等于“今天仍适用”。

## 本阶段的测试与剩余工作

`tests/test_coach_memory.py` 检查已核对才可检索、理解步骤拿到出处、旧条件不自动继承、未核对消息排除、未来业务日/轮次过滤、跨用户隔离、删除失效、重启持久化及未登录拒绝。与 `test_coach_reviews.py`、`test_coach.py` 一起运行：**54 passed，2 条既有依赖弃用警告**。模型替身验证了历史证据接线；没有做本阶段真实模型、浏览器或线上验收。

下一段 E2b 才做经用户确认、可查看/更正/删除的个人记忆条目与长对话摘要；需要明确迁移及备份方案，不能偷偷借旧档案字段存额外模型文本。再用独立的多轮对话集比较“无回忆/有回忆”在指代、纠正、时间和硬约束上的实际表现。E3 有界检索循环与 E4 受控自提升、知识沉淀保持后续状态。
