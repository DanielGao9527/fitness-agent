# 业务服务

应用的核心规则集中在这里。接口层负责接收请求，本目录负责判断能否执行、读取本人事实、生成结果和安全保存。模型输出不能绕过这些规则。

| 业务 | 主要入口与职责 |
| --- | --- |
| 档案与实际记录 | `records.py` 管理档案、饮食和训练；`body_measurements.py`、`body_state.py` 维护体测及当前体重来源 |
| 饮食录入 | `meal_drafts.py`、`meal_input.py`、`nutrition.py` 处理草稿、餐次核对和营养估算 |
| 训练录入 | `workouts.py`、`workout_totals.py` 处理训练草稿、解析、消耗与汇总 |
| 目标与摄入 | `nutrition_targets.py`、`intake_targets.py`、`meal_intake.py` 管理长期目标、单日覆盖和已记录摄入 |
| 配餐 | `quick_meals.py`、`meal_plans.py` 组织候选；`meal_balance.py` 联合配量并复核；`meal_variety.py` 处理搭配偏好 |
| 营养参考与反馈 | `meal_nutrient_reference.py` 计算参考范围；`energy_feedback.py` 根据有效历史给出有界调整 |
| 当天训练 | `training_recommendations.py` 组织建议；`training_catalog.py` 校验目录；`training_program.py` 按器械、历史和时间编排 |
| 训练上下文与负重 | `training_context.py` 处理本次条件；`training_progression.py` 只依据可比较的实际表现给出条件提示 |
| 连续对话 | `coach.py` 保存对话并路由意图；`coach_reviews.py` 校验模型理解 |
| 知识问答 | `knowledge_answers.py` 组织资料摘答和引用核对 |
| 公共支撑 | `usage.py` 管理模型调用次数，`access_guard.py` 管理认证请求限制，`backups.py` 管理一致性备份与恢复 |

## 建议阅读路径

饮食建议可以沿 `api/coach.py → coach.py / quick_meals.py → meal_balance.py` 阅读；训练建议可以沿 `api/coach.py → training_recommendations.py → training_program.py` 阅读。各模块的输入契约见 [schemas.py](../schemas.py)，完整关系见[架构说明](../docs/ARCHITECTURE.md)。

## 需要保持的业务语义

- 真实记录、待核对草稿和建议分开；生成或刷新建议不会自动记账。
- 身份由认证会话绑定。写入前核对用户归属、版本和防重复标识，不能把过期或迟到结果覆盖到新状态。
- 配餐先遵守明确食物限制，再核对目标与完整营养区间；没有可行解时明确说明，不修改目标凑结果。
- 当天训练读取实际完成历史、明确器械与当前档案；可用时间是上限，不把建议当成完成历史。
- 未知摄入、缺来源或模型失败不能被当成成功或零值。有限规则不等于任意自然语言的完整语义校验。
- 业务日期统一为北京时间；模型调用次数按 UTC 日计数，二者用途不同。

部分计划和执行草稿服务保留历史数据兼容职责。判断能否删除时，需要同时查看路由、现行页面与兼容测试。
