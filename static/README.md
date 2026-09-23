# 中文前端

原生 HTML、CSS 和 JavaScript 页面，由 FastAPI 直接提供，不需要额外的前端编译步骤。通过本地服务访问首页，直接双击 HTML 文件无法完成账号和接口交互。

| 文件或分组 | 页面职责 |
| --- | --- |
| [index.html](index.html) / [style.css](style.css) | 页面结构、导航与响应式样式 |
| [app.js](app.js) | 页面启动、登录状态、接口请求、错误呈现和主视图切换 |
| `coach.js` | 助手对话、条件确认、理解与建议结果 |
| `quick-meals.js` / `meal-*.js` / `manual-nutrition.js` | 饮食草稿、照片、营养核对及餐次建议 |
| `workouts.js` / `training-*.js` | 实际训练录入、当天建议与已有草稿兼容展示 |
| `intake-targets.js` / `nutrition-targets.js` | 能量与营养目标 |
| `body-measurements.js` | 体测历史及变化回顾 |
| `knowledge.js` / `knowledge-ask.js` | 知识浏览、检索与资料问答 |
| `speech.js` | 主动录音、停止、转写和核对回填 |
| `usage.js` / `business-time.js` | 调用次数展示与业务日期处理 |

所有 AI 请求都通过已认证的后端，浏览器不持有供应商密钥。录音与照片需要明确操作；转写、识别或推荐结果不会自动变成实际记录。

调整交互时同时检查刷新、取消、网络失败、重复点击、退出登录和迟到响应。公共页面只呈现用户完成任务所需的信息，保留估算、来源、适用范围与可恢复错误，不显示开发路径和内部配置。

验证入口见 [tests/README.md](../tests/README.md)。当前页面套件清单在 [tests/browser-current.json](../tests/browser-current.json)；旧脚本名称不保证仍对应现行页面入口。
