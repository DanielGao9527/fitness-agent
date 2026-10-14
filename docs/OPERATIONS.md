# 运维与恢复

以下数据库命令使用项目虚拟环境中的 Python；在项目根目录执行。`data/runtime` 是本地默认路径，云端应替换为实际持久数据路径。备份包含私人记录，不进入 Git、网页静态目录或公开附件。

## 健康检查与服务

`GET /api/health` 可核对应用启动和能力配置状态，不会生成 AI 内容。`configured_unverified` 表示配置存在，不等于真实供应商调用已经验证。共享模式不返回开发版本或具体模型信息。

Linux 服务、日志和自动更新命令见 [deploy/README.md](../deploy/README.md)。修改环境变量后重新启动服务；不要仅因页面可打开就宣布模型网络连通。

## 私密创建账号

共享服务保持 `FITNESS_REGISTRATION_ENABLED=false`。首次正常启动服务生成空数据库后，在自己的 SSH 或云控制台交互终端执行：

```bash
sudo -u fitness /opt/fitness-agent/current/.venv/bin/python /opt/fitness-agent/current/scripts/create_account.py --database /var/lib/fitness-agent/fitness.sqlite3
```

输入用户名，再输入两次密码，密码不会显示。用户名为 3–32 位字母、数字或下划线，密码为 8–128 位；创建完成后从网页登录即可。工具只连接指定的已有应用数据库，不发登录会话、不修改已有账号、不开放网页注册。密码不接受命令参数、环境变量或管道输入；由账号使用者在私密终端输入，不发送到聊天或写入脚本。

## 一致性备份

```bash
python scripts/backup.py create --source data/runtime/fitness.sqlite3 --destination data/runtime/backups/manual-001.sqlite3
python scripts/backup.py verify --source data/runtime/backups/manual-001.sqlite3
```

工具使用 SQLite backup 接口处理运行中的已提交内容，同时生成清单并检查完整性/外键。目标必须是新路径，保留同名 `.manifest.json`；不要直接复制运行中的主文件，或手动删除 WAL/SHM。

备份未自动加密，哈希也不是来源签名；只恢复自己可信保管的副本。单机磁盘副本无法防止整机损坏，正式服务还应配置私密异地备份、保留周期与定期恢复演练。

## v14/v15/v16 → v17 私人记忆迁移

2026-09-29，本机 localhost:8765 已使用当前增强代码：原 v13 账号库经 scripts/migrate_guests.py 备份、演练和校验后直接升级到当前 v17，原有 18 张表的指纹保留。校验备份在本机 fitness-agent/data/runtime/backups/local-enhancement-20260929-222837/，不进版本控制。下面的 v14/v15/v16 步骤用于尚未升级的其他环境，不能在本机 v17 库重复执行；本次没有操作公网服务器。

v15 新增两张私人记忆表，v16 新增器械字段版本/来源事件表，v17 新增当前账号的当天器械条件表。发布器会拒绝旧库与新代码的结构不一致，不能通过直接推送或关闭预检绕过。正式环境先暂停自动发布并停服，确认目标库确为 v14、v15 或 v16；选一个未使用的私密备份路径，由维护人员在服务器上运行候选版本的 `scripts/migrate_memory.py --database <现有库绝对路径> --backup <新备份绝对路径> --confirm-service-stopped`。工具先创建并验证一致性备份，再升级、比较原有各表指纹；迁移成功后单独演练恢复、检查登录和记录，再决定是否切换新代码。命令不会停止/重启服务，也不会自动回滚。普通本地直接启动旧库时 `Database.initialize()` 仍会按旧机制自动升级并留下 `pre-v17.bak`；该副本不能替代正式维护的独立校验备份。若正式库仍为 v13，应另行按[账号与游客](ACCOUNTS.md)的旧库维护方案处理，不把此工具指向 v13。

v16 为已有器械档案建立 `legacy_profile` 基线版本，不修改原表数据或推断原始确认时间。没有版本绑定的旧会话器械不会继续覆盖档案，必要时重新补充本次器械；原对话文本仍保留。清空器械会撤销有效值并使旧派生结果失效，删除聊天不等于撤销单独保存的档案。维护细节见 [E2d.1](archive/2026-09/MEMORY_E2D_FACT_VERSIONS.md)。回退到旧代码必须停服并恢复相匹配的备份到新路径；不要让旧程序直接操作 v16 库，也不要覆盖迁移后产生的新数据。

## 恢复到新路径

```bash
python scripts/backup.py restore --source data/runtime/backups/manual-001.sqlite3 --destination data/runtime/recovered-001.sqlite3
python scripts/rehearse_recovery.py --source data/runtime/fitness.sqlite3 --directory data/runtime/backups/rehearsal-001
```

恢复工具不覆盖现有库。恢复副本中的旧会话及会话级确认会失效，需要重新登录；账号、记录和快照内的调用计数保留。备份之后新增的记录不会自动出现，不能通过回滚清除实际发生的调用费用。

本地运行或未采用本项目发布器的自部署环境：真正切换恢复库前先停服，保存现状，核对恢复副本与备份后的新增记录，再按该环境的配置方式设置 `FITNESS_DB_PATH` 指向新文件并重启。

采用本项目 `deploy/` 发布器的生产环境：数据库固定为 `/var/lib/fitness-agent/fitness.sqlite3`，不能通过修改 `FITNESS_DB_PATH` 绕过发布器的路径校验。恢复前先暂停自动拉取，确认没有部署任务正在运行，再停止应用并保全现有数据库及相关文件。核对恢复副本的结构、完整性、账号、记录和调用计数，确认如何处理备份后的新增数据后，由管理员按单独审核的恢复方案在固定路径切换数据库，并保持 `fitness` 用户的所有权与私密访问权限。重新启动后验证健康、登录及记录持久化，再恢复自动拉取；不要直接覆盖正在运行的数据库。

上述演练命令只生成独立副本，不切换正式数据库。代码回退也不自动执行数据库恢复。

## 分享前预检

```bash
python scripts/preflight.py --backup data/runtime/backups/manual-001.sqlite3
python scripts/preflight.py --for-sharing --backup data/runtime/backups/manual-001.sqlite3
```

预检核对当前进程环境、数据库位置/完整性、调用限制和备份状态，不输出密钥或私人正文。它不能代替 HTTPS、可信代理、网络连通和真实手机验收。

## 日期、限流与日志

业务日期按北京时间，AI 日次数按 UTC 日（北京时间 08:00 更新）。失败请求也可能消耗一次尝试；次数不是金额。登录限流状态持久化在私人库旁的独立数据库，不能删除它来绕过限制。

日志用于状态与故障定位，不主动输出密码、API Key、模型请求正文或私人记录。排障分享日志前仍需人工复核；供应商诊断使用安全故障编号和固定分类，不能根据通用错误提示推断真实根因。
