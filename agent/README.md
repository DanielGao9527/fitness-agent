# 通用助手边界

这个目录保留通用助手接口的上下文和工具封装。当前页面上的饮食、训练与连续对话主要由 [api/coach.py](../api/coach.py) 和 [services/coach.py](../services/coach.py) 驱动。

| 文件 | 职责 |
| --- | --- |
| [tools.py](tools.py) | 用已经绑定用户身份的记录服务，只读获取指定日期的档案、饮食、训练和汇总 |
| [react_agent.py](react_agent.py) | 将本人事实与知识检索结果组织成上下文，传给模型接口 |

文件名中的 `react_agent` 不代表已经实现完整 ReAct 循环。当前通用 `/api/agent/chat` 使用关闭状态的模型入口；不要把它误认为前端现行助手，也不要据此宣称支持自主执行工具。

如需理解已接通的助手，请先读 [services/README.md](../services/README.md)。服务端决定身份，模型和请求正文均不能指定任意用户；这里也不提供模型直接写记录的工具。
