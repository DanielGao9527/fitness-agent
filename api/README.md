# HTTP 接口

这一层把浏览器请求交给业务服务：读取认证会话、校验输入、构造绑定当前用户的服务，并将结果或明确错误返回前端。路由在 [app.py](../app.py) 中注册，输入结构主要定义在 [schemas.py](../schemas.py)。

| 文件 | 接口范围 |
| --- | --- |
| [routes.py](routes.py) | 健康状态、注册登录、档案、饮食记录与草稿、语音、目标、汇总和调用次数 |
| [coach.py](coach.py) | 连续对话、请求理解、餐次与当天训练建议 |
| [meal_plans.py](meal_plans.py) | 配餐候选、营养核算和建议状态 |
| [workouts.py](workouts.py) | 训练解析、估算、草稿和实际记录相关操作 |
| [training_plans.py](training_plans.py) | 训练建议及已有执行草稿的兼容接口 |
| [body_measurements.py](body_measurements.py) | 体测历史与变化回顾 |
| [knowledge.py](knowledge.py) | 资料目录、检索与受来源约束的问答 |
| [meal_photos.py](meal_photos.py) | 经认证的饮食照片识别请求 |

推荐阅读顺序：登录接口 → 档案与记录 → `coach.py` → 对应 [services/](../services/) 模块。本地模式可访问 `/docs` 查看接口结构；共享部署关闭交互式接口文档。

接口层不信任客户端或模型提供的用户身份。写入必须对应明确操作，并校验所属用户、版本及防重复标识；生成建议不等于写入实际记录。
