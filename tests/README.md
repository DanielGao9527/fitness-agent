# 测试

测试分为后端回归、前端单元和隔离浏览器流程。默认回归使用合成数据和模型替身；真实供应商调用属于另外的手动验证，不包含在常规测试结论中。

## 后端回归

在仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Linux／macOS 将解释器路径换为 `.venv/bin/python`。可以指定一个文件定位问题，例如：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_public_release.py
```

主要覆盖账号隔离、输入校验、草稿防重复、重启持久化、资料有效性、配餐约束、训练条件、备份恢复与共享访问边界。测试数据库应保持隔离，不将 `FITNESS_DB_PATH` 指向真实用户数据来运行测试。

## 前端单元

安装 Node.js 后，从仓库根目录运行单个 `*-unit.cjs` 文件，例如：

```powershell
node tests/speech-flow-unit.cjs
```

这些测试使用 DOM、媒体或接口替身，验证状态和交互逻辑；不会打开真实麦克风。

## 浏览器流程

当前有效套件以 [browser-current.json](browser-current.json) 为准。浏览器脚本使用 Playwright，并调用本机 Chrome；需要预先安装这些测试依赖。

先在独立终端启动合成服务：

```powershell
.\.venv\Scripts\python.exe tests/ui_fixture_server.py --coach
```

服务固定监听 `127.0.0.1:8767`，使用 `artifacts/` 下的隔离数据库和模型替身。不要用它保存真实记录或部署给用户。

再在另一个终端运行需要的套件：

```powershell
node tests/browser-assistant-quality.cjs
```

套件会生成合成账号和截图等测试产物，检查普通桌面及较窄视口下的页面行为。完成后停止合成服务；测试产物不提交到公开仓库。

## 如何解释结果

替身测试通过只能说明对应输入和异常场景的工程行为符合预期。它不能证明营养测量准确、任意口语均能理解、真实语音／图片供应商可用、实体手机权限正常或云端部署完成。

历史升级检查及旧页面脚本可能只用于特定兼容场景，不能把目录里所有 `browser-*.cjs` 都当成当前验收入口。真实接口检查工具说明见 [scripts/README.md](../scripts/README.md)。
