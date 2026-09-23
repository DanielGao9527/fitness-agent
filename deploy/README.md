# 部署到阿里云 ECS

这个目录负责把 **GitHub 中检查通过的提交** 安装到 Ubuntu 22.04 服务器，并在更新时保留云端账号、记录和密钥。当前文件是可审查的部署实现；仓库中存在这些文件，不代表服务器已经安装或公网已经验收。

## 更新怎样到达服务器

```text
本机改代码 → 推送 main → GitHub 离线测试
                             ↓ 成功，且 DEPLOY_ENABLED=true
                      推进 deploy 发布分支
                             ↓ ECS 每约 5 分钟主动检查
                      安装确定提交 → 备份 → 切换 → 本机健康检查
```

服务器主动通过 HTTPS 读取公开仓库，不需要 GitHub 连接服务器的 SSH 端口，不需要把服务器密码、模型 Key 或 GitHub 长期令牌放进工作流，也不在生产 ECS 安装 GitHub runner。服务器仍须能访问 GitHub、Python 软件包源；中国内地的实际网络需首次验证。GitHub 发布任务完成只代表发布分支推进，**服务器安装结果以 ECS 日志为准**。

ECS 实测 Git HTTPS 返回 HTTP/2 错误，改 HTTP/1.1 后仍超时；官方 REST API 与固定提交归档则可访问。因此服务器通过 `api.github.com` 查询 `deploy` 的确定 SHA，再从官方跳转到 `codeload.github.com` 下载该提交。发布器校验提交先后关系、跳转域名及安全解包，不依赖服务器 `git clone/fetch`，不使用第三方镜像或关闭 TLS 校验。

公开 API 无凭据额度按出口 IP 计为每小时 60 次；五分钟空闲轮询约 12 次，每次真正更新另需比较提交、下载归档及切换前复查。发布器保存 ETag 和已校验 SHA，但不把匿名 304 宣称为免费请求；遇到限流会把等待时间写入状态文件，后续定时任务在窗口恢复前不发请求。网络错误不会用旧缓存继续发布。

开发分支与 PR 只跑测试。只有 `main` 的成功检查或在 `main` 手动运行工作流才可能推进 `deploy`。部署分支采用普通快进推送，不强制覆盖历史；不要手工向 `deploy` 提交代码，修复和回退都通过 `main` 创建新提交。定时拉取、完整 SHA 校验、服务器部署锁防止同机并发切换。

## 文件职责

| 文件 | 用途 |
| --- | --- |
| `install.sh` | 安装账户、持久目录、固定管理脚本和 systemd 服务，不直接启动应用 |
| `deploy.py` | 拉取发布分支、安装新版本、备份和切换，失败时处理恢复 |
| `github_source.py` | 官方 API、提交先后校验、限流缓存和有大小限制的归档下载/安全解包；也可首次取源码 |
| `check_release.py` | 以应用账户检查共享配置、数据库结构、备份与本机健康，不调用模型 |
| `fitness-agent.service` | 用非 root 用户运行一个应用进程，只监听本机 8765 |
| `fitness-agent-pull.service`、`.timer` | 周期查询已检查的发布分支，没有新提交时不重装 |
| `fitness-agent.env.example` | 生产配置模板；首次全部 AI 开关与额度关闭 |
| `Caddyfile.example` | 域名 HTTPS 反向代理模板 |
| `tests/` | 临时库与服务替身验证失败恢复边界，不连接云端 |
| [工作流](../.github/workflows/check-and-release.yml) | Ubuntu 22.04 / Python 3.12 离线检查，以及可选发布分支推进 |

## 服务器的文件放在哪里

| 位置 | 内容与权限 |
| --- | --- |
| `/opt/fitness-python/` | 已安装的独立 Python 3.12，不替换 Ubuntu 的系统 Python |
| `/opt/fitness-tools/uv` | 管理 Python 虚拟环境的工具 |
| `/opt/fitness-agent/releases/<完整 SHA>/` | 每个版本的源码与独立 `.venv`；安装完成后由 root 持有，只读运行 |
| `/opt/fitness-agent/current` | 当前版本的软链接 |
| `/var/lib/fitness-agent/` | 应用用户 `fitness` 的私密数据库、索引和备份 |
| `/etc/fitness-agent.env` | root 可读的生产配置，由 systemd 加载 |
| `/usr/local/lib/fitness-agent-deploy/` | 管理员审核后安装的固定发布器；源码更新不会自行替换它 |
| `/var/lib/fitness-agent-deploy/` | root 的 API 缓存、限流时间、部署锁与成功/失败状态，不含 GitHub 凭据 |
| `/var/cache/fitness-build/` | 非 root 依赖安装账户的缓存；不能读取正式用户数据或密钥 |

应用只使用 `/var/lib/fitness-agent/fitness.sqlite3`。发布器禁止改成代码目录里的数据库，也不会上传或覆盖本机私人记录。服务器初次使用空数据库，演示账户需要单独准备。

## 第一次配置

以下在 ECS 上逐步执行，先完成源码推送与 Linux 检查，再进入服务器配置。示例域名必须替换为实际域名；地域、域名解析、适用的备案与 HTTPS 条件另行核对。

1. 安装基础工具，确认之前的 Python 3.12 路径存在：

   ```bash
   sudo apt update
   sudo apt install -y ca-certificates curl
   env UV_PYTHON_INSTALL_DIR=/opt/fitness-python /opt/fitness-tools/uv python find --managed-python 3.12
   df -h /opt /var/lib
   ```

2. 在 GitHub 的仓库设置 → Secrets and variables → Actions → Variables 新增仓库变量 `DEPLOY_ENABLED`，值先保持 `false`。首次推送后等待“Linux 离线检查”通过。PR 检查不接触生产秘密。该流程不需要新增任何 Secret。

3. 核对通过检查的 `main` 提交，复制其完整 40 位 SHA。首次通过官方 API 取得该提交中的下载工具，再用它下载同一提交的完整源码；把下面 `经过检查的完整提交SHA` 替换掉。不要直接执行未核对的分支最新脚本：

   ```bash
   FITNESS_SHA=经过检查的完整提交SHA
   FITNESS_STAGE=$(mktemp -d /tmp/fitness-bootstrap.XXXXXX)
   curl --fail --show-error --silent --proto '=https' --connect-timeout 10 --max-time 90 \
     -H 'Accept: application/vnd.github.raw+json' -H 'X-GitHub-Api-Version: 2022-11-28' \
     "https://api.github.com/repos/DanielGao9527/fitness-agent/contents/deploy/github_source.py?ref=$FITNESS_SHA" \
     -o "$FITNESS_STAGE/github_source.py"
   python3 "$FITNESS_STAGE/github_source.py" "$FITNESS_SHA" "$FITNESS_STAGE/source"
   sudo bash "$FITNESS_STAGE/source/deploy/install.sh"
   sudo nano /etc/fitness-agent.env
   ```

   每条命令成功后再执行下一条，报错即停止。临时目录为每次新建；下载工具只接受固定仓库与完整 SHA，归档最多 25 MiB、解压后最多 100 MiB/10000 项，并拒绝越界路径、链接、重复文件或多个根目录。它不启动应用。初始化脚本保留已有环境文件和用户数据库，不自动启用应用或定时拉取。

4. 配置真实 `FITNESS_PUBLIC_ORIGIN=https://实际域名` 和对应 `FITNESS_ALLOWED_HOSTS=实际域名`。保留 `shared`、安全 Cookie 与关闭注册。首次保留禁用 AI 与零额度，验证账号和记录不需要生成费用。模型 Key 以后只在服务器编辑器中填入，不粘贴到 GitHub、聊天记录或命令行参数中。

5. 准备域名 HTTPS 和访问入口。Caddy 使用模板转发到 `127.0.0.1:8765`；应用端口 8765 不向公网开放。当前目录不自动生成备案信息或证书；演示账户在下一步首次服务启动、数据库生成后创建。

6. 把 `DEPLOY_ENABLED` 改为 `true`，在 GitHub Actions 手动运行“检查与发布”，选择 `main`。检查成功后会创建或推进 `deploy`。记录这次的完整提交 SHA，再在 ECS 执行第一次部署：

   ```bash
   sudo python3 /usr/local/lib/fitness-agent-deploy/deploy.py 经过检查的完整提交SHA
   sudo systemctl status fitness-agent --no-pager
   ```

   发布器会再检查 SHA 必须恰好等于当前 `deploy` 分支。安装期间旧应用继续运行；备份与版本切换会有短暂维护窗口，不承诺零停机。首次没有旧版本可恢复，失败后服务停止，保留日志和失败目录供处理。

   首次服务健康且数据库已生成后，在自己的私密交互终端使用 [账号创建工具](../scripts/create_account.py)：

   ```bash
   sudo -u fitness /opt/fitness-agent/current/.venv/bin/python /opt/fitness-agent/current/scripts/create_account.py --database /var/lib/fitness-agent/fitness.sqlite3
   ```

   按提示输入用户名和两次密码，密码输入不显示；不要把密码写在命令参数、管道、聊天或仓库中。每名试用者建立独立账号，网页注册始终保持关闭。此工具只操作已存在的数据库，不会替你初始化数据库或重置已有账号。

7. 完成本机和 HTTPS 检查、独立演示账号隔离、重启持久化、备份恢复演练、真实手机验收。健康检查只验证本机进程与共享状态，不会自动验证公网证书、模型生成或手机。

8. 首次人工验收成功后启用开机启动及自动拉取：

   ```bash
   sudo systemctl enable fitness-agent
   sudo systemctl enable --now fitness-agent-pull.timer
   sudo systemctl list-timers fitness-agent-pull.timer
   ```

此后 main 的检查通过会推进发布分支；网络和额度正常时，ECS 约五分钟后开始处理新版本，再加安装耗时。网络不可用或 API 冷却时保持已运行版本；不自动变更防火墙或关闭 TLS 校验。不要用归档文件的 SHA256 与 Git 提交 SHA 比较：官方保证固定提交的文件内容稳定，但压缩字节可变化。

## 更新失败时

先查看服务器日志，不把含隐私的完整日志公开上传：

```bash
sudo journalctl -u fitness-agent-pull.service -n 80 --no-pager
sudo journalctl -u fitness-agent -n 80 --no-pager
sudo cat /var/lib/fitness-agent-deploy/last-successful-sha
```

安装依赖或预检失败时不切换旧应用。切换前停止旧服务并创建一致性数据库备份，保留同名 `.manifest.json`。新服务本机健康失败且数据库结构保持不变时，恢复前一代码目录并重新启动；**不自动恢复数据库快照**，避免丢失用户新记录或恢复已经消耗的调用额度。

同一个失败提交不会每五分钟重复安装。排查修复后，可明确重试当前发布分支：

```bash
sudo python3 /usr/local/lib/fitness-agent-deploy/deploy.py 经过检查的完整提交SHA --retry
```

重试保留旧的失败目录，再创建新目录，不删除故障证据。若新代码改变数据库结构，会在切换前停止自动发布，要求单独安排迁移；若启动后发生了意外结构变化，则保持服务停止，并写入 `manual-recovery-required` 阻止后续发布。不要直接删除这个标记或强启旧代码，先完成数据库恢复/迁移核对。

需要暂停后续自动安装：

```bash
sudo systemctl disable --now fitness-agent-pull.timer
```

同时把 GitHub 的 `DEPLOY_ENABLED` 设为 `false` 可暂停推进发布分支；单独关闭 GitHub 变量不会撤回已经进入 `deploy` 分支的版本。定时器停止也不会中断已经运行的发布任务，需要先查看服务状态。

## 备份、维护与边界

- 每次更新前的同机快照只解决更新风险，不能应对整台云盘丢失；上线前还要配置并验证每日备份、保留周期及私密加密异地副本。
- 每版独立虚拟环境会占用磁盘。当前脚本不自动删除历史版本或备份；定期检查磁盘，核对当前与可恢复版本后再清理，不能删 `current` 指向的目录。
- 改 `/etc/fitness-agent.env` 后须重启应用，环境才生效。增加 AI 能力与额度前核对地域、密钥和预算；自动部署永远不执行真实模型生成探针。
- 发布器本身与 systemd 单元属于管理员配置。更新这些文件时人工审核后重新运行初始化并核对服务配置，不由仓库提交自动取得额外系统权限。
- 自动恢复只覆盖已测试的代码切换失败边界，不代替数据库迁移计划、磁盘/网络监控、异地恢复或公网验收。

参考：[GitHub 工作流权限](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions)、[GitHub 安全使用](https://docs.github.com/en/actions/reference/security/secure-use)、[systemd 定时器](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)。

源码获取依据：[固定提交归档](https://docs.github.com/en/rest/repos/contents#download-a-repository-archive-tar)、[比较提交](https://docs.github.com/en/rest/commits/commits#compare-two-commits)、[API 限流与条件请求](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)。
