# 配置与模型接入

应用从进程环境变量读取配置，**不会自动加载 `.env`**。[.env.example](../.env.example) 仅列出配置示例，不包含真实密钥。修改服务环境后需要重新启动对应服务。

## 无密钥运行

默认 `FITNESS_MODEL_PROVIDER=disabled`。账号、档案和手动饮食/训练/体测记录可以使用；依赖 AI 的功能返回未配置状态。不要为健康检查或自动测试配置真实模型 Key。

## 核心配置

| 变量 | 作用 |
| --- | --- |
| `FITNESS_DB_PATH` | 私人 SQLite 路径；部署时使用独立持久目录的绝对路径 |
| `FITNESS_ACCESS_MODE` | `local` 本机试用，`shared` HTTPS 共享访问 |
| `FITNESS_PUBLIC_ORIGIN` | 共享入口，如 `https://fitness.example.com` |
| `FITNESS_ALLOWED_HOSTS` | 明确主机名，用逗号分隔，不写通配符、协议或端口 |
| `FITNESS_COOKIE_SECURE` | HTTPS 共享必须为 `true` |
| `FITNESS_REGISTRATION_ENABLED` | 是否允许新注册；共享模式必须为 `false` |
| `FITNESS_MODEL_PROVIDER` | `disabled` 或 `qwen` |
| `DASHSCOPE_API_KEY` | 服务端千问凭据，不进入浏览器、日志或 Git |
| `FITNESS_QWEN_MODEL` | 文字模型名称；默认值以 `config.py` 为准 |
| `FITNESS_QWEN_BASE_URL` | 兼容接口地址，代码限制允许的官方 HTTPS 地址 |
| `FITNESS_AI_USER_DAILY_LIMIT` | 每个账号每日尝试次数上限 |
| `FITNESS_AI_GLOBAL_DAILY_LIMIT` | 全服务每日尝试次数上限 |

共享模式缺少 HTTPS、匹配主机、安全 Cookie 或注册未关闭时会拒绝启动。先通过可信私密入口建立试用账号，再关闭注册切入共享模式。

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

语音和照片模型分别使用 `FITNESS_QWEN_ASR_MODEL`、`FITNESS_QWEN_VISION_MODEL`。开关打开不代表供应商模型权限或计费状态已验证。模型名称、权限和额度应在所用供应商控制台核对，不在源码中写入个人账户信息。

Windows 的 [启动脚本](../scripts/start.ps1) 提供相应 `-Enable...` 参数，说明见 [scripts](../scripts/README.md)。Linux 服务采用 [deploy](../deploy/README.md) 的环境文件和 systemd 配置。

记录日期按北京时间处理；AI 次数按 UTC 日统计。供应商实际收费以其账单为准，应用次数上限不能代替费用预算。
