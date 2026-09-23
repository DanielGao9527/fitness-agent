# 安装、检查与维护工具

这些工具从仓库根目录运行。使用项目虚拟环境，确保操作目标是预期的数据文件和运行环境。

| 脚本 | 用途 |
| --- | --- |
| [setup.ps1](setup.ps1) | 在 Windows 创建虚拟环境，优先安装锁定依赖 |
| [start.ps1](start.ps1) | 启动本地服务，选择可用端口，并按参数启用 AI 能力 |
| [create_account.py](create_account.py) | 在私密交互终端创建账号，无需开放网页注册 |
| [preflight.py](preflight.py) | 检查访问配置、数据库、备份与调用次数限制，不发起模型请求或部署 |
| [backup.py](backup.py) | 创建一致性备份、校验备份、恢复到新路径 |
| [rehearse_recovery.py](rehearse_recovery.py) | 在独立新目录演练恢复并核对数据库 |
| `check_*.py` | 特定功能或真实供应商的手动检查工具，运行前阅读文件参数与适用范围 |

## 本地使用

```powershell
.\scripts\setup.ps1
.\scripts\start.ps1
```

第一次正常启动生成数据库后，可执行只读配置检查：

```powershell
.\.venv\Scripts\python.exe scripts/preflight.py
```

查看备份工具参数：

```powershell
.\.venv\Scripts\python.exe scripts/backup.py --help
```

完整备份、恢复和共享环境检查步骤见[运行与恢复](../docs/OPERATIONS.md)。恢复工具只接受新目标路径，不自动覆盖现有数据库；备份包含私人记录，应保存在受限位置。

## 私密创建试用账号

先正常启动一次应用，让目标数据库初始化，再通过服务器私密终端运行：

```bash
sudo -u fitness /opt/fitness-agent/current/.venv/bin/python /opt/fitness-agent/current/scripts/create_account.py --database /var/lib/fitness-agent/fitness.sqlite3
```

按提示输入用户名及两次密码；密码输入时不显示。脚本要求已有数据库的绝对路径，不接受密码参数、环境变量或管道输入，不修改已有账号，也不改变网页注册开关。不要将密码粘贴到命令、聊天或日志。

## 真实接口检查

`check_*.py` 不属于自动发布时可随意全部运行的无费用测试集合。其中部分工具会连接真实模型服务，需要单独满足参数、凭据和调用次数约束。优先使用 [tests/](../tests/) 中的合成测试定位问题，再按需要执行具体检查。

常规本地检查不能代替服务器上的依赖安装、HTTPS、代理、账号、重启持久化与恢复验证。服务器发布流程见[部署说明](../docs/DEPLOYMENT.md)。
