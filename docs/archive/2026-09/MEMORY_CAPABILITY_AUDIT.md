# 当前记忆能力核查

结论：Fitness 有实用的记忆基础，E2a 已补按需搜索经核对的旧对话，E2b 已补用户显式保存的温记忆和需核对的历史摘录；仍不具备模型自动学习/晋升的完整分层管理。核查与实现未读取现有私人数据库，见 [E2a 接线和边界](MEMORY_E2A.md)及[E2 进度](MEMORY_E2_DESIGN.md)。

| 已有能力 | 代码入口 | 范围 |
| --- | --- | --- |
| 持久会话 | `database.py` 的 coach_conversations/coach_turns；`services/coach.py` 的 send/view | 原消息与结构化响应持久化、重启恢复、按账号隔离 |
| 会话状态继承 | `coach.py` 的 meal_context/conversation_constraints；`training_context.py` | 继承已核对的餐次、器械、训练需求和限制 |
| 从原话提取事实 | `coach_reviews.py` 的 snapshot/understand/apply | 模型处理待核对消息，确认后累积会话 notes 和限制，不仅是存聊天原文 |
| 长期档案 | `records.py` 的 get_profile/save_profile；`profile_context.py` | 跨会话读取明确保存的目标、经验、器械、偏好、过敏等字段 |
| 真实历史读取 | `training_context.py` 的 recent_training；`training_progression.py` 的 performance_history | 按本人、日期与动作 ID 查询最近训练，并提供有限历史参考 |
| 用户确认的温记忆 | `coach_memory_entries.py`、`coach_reviews.py`、聊天页个人记忆 | 显式保存的交流偏好和术语按账号读取，可更正删除；不替代档案硬约束 |
| 旧对话摘录候选 | `coach_memory_digests.py`、聊天页核对入口 | 仅已核对原话，候选需用户核对、源变化失效、源删除级联删除；按需词项找回 |

当前缺少：模型自动对话摘要压缩、跨会话**语义向量**回忆、三层自动晋升/降级/淘汰、模型自动学习个人术语。E2b 增加的是用户可编辑的 `USER.md`/`MEMORY.md` 等价逻辑和人工核对的原话摘录，不是复制 Hermes 的文件或让模型自行写档案；公共知识向量库仍只含公共审核摘要。

40 轮对话上限、50 个对话上限和七天历史窗口分别是业务容量/查询限制；不能把它们描述成自动分层或遗忘算法。部分 summary 函数只是营养、体测、训练统计，不是模型生成的对话摘要。

`tests/test_coach_reviews.py` 明确检查新会话不继承旧会话的健康限制；长期健康信息仍由档案承载。后续不能为做“记忆”而破坏这一区分。知识摘答当前发送的是当次问题和公开候选，不会自动读取全部历史聊天。

Hermes 官方文档描述了 MEMORY.md/USER.md 持久记忆以及 session_search 历史检索；这些能力应逐项对照，不把泛称的热/温/冷术语当作已接入证据：[持久记忆](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)、[会话](https://hermes-agent.nousresearch.com/docs/user-guide/sessions)。

已从 `services/coach_reviews.py` 接入按本人确认状态过滤的历史证据与用户显式保存的温记忆。下一段仍需独立多轮质量评测、真实模型/浏览器验收；不能把 E2a/E2b 称为 agent 自主学习或已上线记忆系统。
