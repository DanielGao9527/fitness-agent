# 配置与模型接入

应用从进程环境变量读取配置，**不会自动加载 `.env`**。[.env.example](../.env.example) 仅列出配置示例，不包含真实密钥。修改服务环境后需要重新启动对应服务。

本机 2026-10-15 使用用户专用环境配置：`FITNESS_QWEN_MODEL=qwen3.6-plus`、`FITNESS_QWEN_VISION_MODEL=qwen3.6-plus`；源码默认型号没有因此改名。文字模型与 `text-embedding-v4` 查询编码已真实联调；照片/语音仍待本轮真实验收。原 `qwen3.7-plus` 遇到 FreeTierOnly 限制，各模型免费额度不能互用，见 [实际记录](../evals/results/real-acceptance-qwen36-2026-10-15.json)。

## 无密钥运行

默认 `FITNESS_MODEL_PROVIDER=disabled`、`FITNESS_RETRIEVAL_MODE=hybrid`、`FITNESS_RETRIEVAL_TOKENIZER=jieba`。没有 Key 时，知识查询明确退到 jieba 词法；账号、档案和手动饮食/训练/体测记录仍可使用。依赖 AI 的功能返回未配置状态。启动、健康检查不会调用付费向量服务。不要为自动测试配置真实模型 Key。

## 核心配置

| 变量 | 作用 |
| --- | --- |
| `FITNESS_DB_PATH` | 私人 SQLite 路径；部署时使用独立持久目录的绝对路径 |
| `FITNESS_ACCESS_MODE` | `local` 本机试用，`shared` HTTPS 共享访问 |
| `FITNESS_PUBLIC_ORIGIN` | 共享入口，如 `https://fitness.example.com` |
| `FITNESS_ALLOWED_HOSTS` | 明确主机名，用逗号分隔，不写通配符、协议或端口 |
| `FITNESS_COOKIE_SECURE` | HTTPS 共享必须为 `true` |
| `FITNESS_REGISTRATION_ENABLED` | 是否允许网页注册；本机与共享模式均可配置，已有生产环境需显式改为 `true` |
| `FITNESS_GUEST_ENABLED` | 是否开放游客；本机默认开启，共享环境未配置时默认关闭 |
| `FITNESS_GUEST_AI_DAILY_LIMIT` | 同一网络每日共享游客 AI 次数，默认20；同时受账号及全站上限约束，0关闭游客付费调用 |
| `FITNESS_MODEL_PROVIDER` | `disabled` 或 `qwen` |
| `DASHSCOPE_API_KEY` | 服务端千问凭据，不进入浏览器、日志或 Git |
| `FITNESS_QWEN_MODEL` | 文字模型名称；默认值以 `config.py` 为准 |
| `FITNESS_QWEN_BASE_URL` | 兼容接口地址，代码限制允许的官方 HTTPS 地址 |
| `FITNESS_AI_USER_DAILY_LIMIT` | 每个账号每日尝试次数上限 |
| `FITNESS_AI_GLOBAL_DAILY_LIMIT` | 全服务每日尝试次数上限 |

共享模式缺少 HTTPS、匹配主机或安全 Cookie 时拒绝启动。注册与游客可独立开关；关闭注册不影响已有账号登录。公网注册沿用持久化频率限制，游客另有创建频率限制和模型额度，不替代网关层防滥用。当前代码运行时要求私人库 v22；发布器对旧 v14–v21 库会先拒绝切换，须按[运行与恢复](OPERATIONS.md)完成离线维护迁移，v13 库另参考[账号与游客](ACCOUNTS.md)，不能只修改环境变量。

## 独立能力开关

以下变量默认均为 `false`：

| 变量 | 能力 |
| --- | --- |
| `FITNESS_SPEECH_ENABLED` | 用户主动录音后的语音转写 |
| `FITNESS_PHOTO_ENABLED` | 饮食照片识别 |
| `FITNESS_NUTRITION_ENABLED` | 饮食营养估算 |
| `FITNESS_WORKOUT_ENABLED` | 训练描述解析和消耗估算 |
| `FITNESS_KNOWLEDGE_QA_ENABLED` | 有依据的资料摘答 |
| `FITNESS_MEAL_PLAN_ENABLED` | 饮食建议 |
| `FITNESS_TRAINING_PLAN_ENABLED` | 训练建议相关能力 |
| `FITNESS_COACH_ENABLED` | 自由描述的 AI 意图理解 |

多数模型能力还需要 `FITNESS_MODEL_PROVIDER=qwen` 和有效 Key；语音转写可独立于 provider 开关启用，仍需有效 Key。当天训练编排使用本地规则，检查训练开关，不额外调用模型生成训练动作。

主聊天的一般知识摘答同样由 `FITNESS_KNOWLEDGE_QA_ENABLED` 控制，不借用配餐开关。资料可检索不代表知识模型已启用；明确问题可直接进入摘答，其他口语可能先需要 `FITNESS_COACH_ENABLED` 做结构化理解。词法模式的一次口语问题可能消耗一次理解和一次摘答；混合模式还可能增加一次问题编码，均共用账号、游客及全站调用限额。主聊天失败或重复加载不自动重试，网页刷新不会重新发起本轮调用。

## 检索与向量配置

| 变量 | 作用 |
| --- | --- |
| `FITNESS_RETRIEVAL_MODE` | 默认 `hybrid`；可设为 `lexical` 仅使用词法检索 |
| `FITNESS_RETRIEVAL_TOKENIZER` | 默认 `jieba` 搜索模式；可设为 `bigram` 重放旧双字基线，文档与查询使用相同词项规则 |
| `FITNESS_EMBEDDING_MODEL` | 云端文本向量型号，当前默认见 `config.py`；不复用聊天型号 |
| `FITNESS_EMBEDDING_DIMENSIONS` | 默认 1024；型号或维度变化需要独立文档向量 |
| `FITNESS_EMBEDDING_BASE_URL` | DashScope 官方原生 `/api/v1` 地址；留空时从 `FITNESS_QWEN_BASE_URL` 推导，未经允许的地址拒绝请求 |
| `FITNESS_EMBEDDING_BATCH_SIZE` | 文档批量编码大小，默认 10，范围 1–10 |
| `FITNESS_EMBEDDING_TIMEOUT_SECONDS` | 向量请求超时，默认 20 秒，范围 1–60 秒；不自动重试 |

向量能力复用服务端 `DASHSCOPE_API_KEY`，由检索模式独立启用。已有聊天权限不代表向量型号在该地域可用。默认文档缓存与词法索引同目录，文件名为 `knowledge-vectors.sqlite3`；不得与私人数据库、来源文件或词法索引共用路径。显式构建方式见[脚本说明](../scripts/README.md)。

开启 `hybrid` 不会自动付费建库。没有完整、匹配的文档缓存时，检索明确降级为词法模式；不会因为一次用户问题自动编码全部文档。缓存完整后，非空查询按需编码一次并计入 AI 使用次数，词法降级不等于该次向量尝试没有产生费用。目录、空查询浏览和按 ID 取证据不调用向量模型。

用户选定的组合现已是代码、样例环境文件及本机启动脚本的默认值；启动脚本覆盖继承的旧检索环境变量，`-UseLexicalBaseline` 只供重放旧双字基线。没有密钥时目录显示 `embedding_not_configured`，查询以 jieba 词法降级，不会假称向量可用。Linux 服务如显式保留了旧环境值，需要在其私密部署配置中改为新组合后再按已有发布流程切换。

问题编码与摘答分别预留额度。证据足够的普通混合问答通常各调用一次；E3 仅在模型明确判定证据不足并提出有效改写时允许第二轮，最多两次问题编码、两次证据判断，口语理解可能另有一次。额度可能在后续步骤耗尽，失败的主聊天轮次也不会自动再次编码。独立离线构建与评测使用脚本的调用数/输入字符预算，不打开私人业务库、不计入应用账号额度，供应商费用仍由运行者承担。输入字符预算不是计费 token 或金额的精确上限。

更完整的数据流、降级及验证边界见[检索增强说明](archive/2026-09/RETRIEVAL_ENHANCEMENT.md)。

语音和照片模型分别使用 `FITNESS_QWEN_ASR_MODEL`、`FITNESS_QWEN_VISION_MODEL`。开关打开不代表供应商模型权限或计费状态已验证。模型名称、权限和额度应在所用供应商控制台核对，不在源码中写入个人账户信息。

Windows 的 [启动脚本](../scripts/start.ps1) 提供相应 `-Enable...` 参数，说明见 [scripts](../scripts/README.md)。Linux 服务采用 [deploy](../deploy/README.md) 的环境文件和 systemd 配置。

记录日期按北京时间处理；AI 次数按 UTC 日统计。供应商实际收费以其账单为准，应用次数上限不能代替费用预算。
