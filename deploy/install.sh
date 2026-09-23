#!/usr/bin/env bash
# 在 ECS 上，从管理员已审核的 Git 提交执行；不启动应用、定时器或 Caddy。
set -euo pipefail
test "$(id -u)" = 0 || { echo '请以管理员运行初始化。' >&2; exit 1; }
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
for tool in python3 systemctl runuser; do command -v "$tool" >/dev/null; done
test -x /opt/fitness-tools/uv
if test -e /opt/fitness-agent && ! test -d /opt/fitness-agent/releases; then
    echo '/opt/fitness-agent 已存在其他布局，请先人工核对。' >&2
    exit 1
fi
getent passwd fitness >/dev/null || useradd --system --home-dir /var/lib/fitness-agent --shell /usr/sbin/nologin fitness
getent passwd fitness-build >/dev/null || useradd --system --home-dir /var/cache/fitness-build --shell /usr/sbin/nologin fitness-build
install -d -o root -g root -m 755 /opt/fitness-agent /opt/fitness-agent/releases /usr/local/lib/fitness-agent-deploy
install -d -o root -g root -m 700 /var/lib/fitness-agent-deploy
install -d -o fitness -g fitness -m 700 /var/lib/fitness-agent /var/lib/fitness-agent/backups
install -d -o fitness-build -g fitness-build -m 700 /var/cache/fitness-build
install -o root -g root -m 644 deploy.py check_release.py github_source.py /usr/local/lib/fitness-agent-deploy/
install -o root -g root -m 644 fitness-agent.service fitness-agent-pull.service fitness-agent-pull.timer /etc/systemd/system/
if ! test -e /etc/fitness-agent.env; then
    install -o root -g root -m 600 fitness-agent.env.example /etc/fitness-agent.env
fi
systemctl daemon-reload
# 首次 current 尚不存在，systemd 的 ExecStart 存在性检查此时必然失败。
# 应用单元在发布器切换到实际版本后、启动之前再验证。
systemd-analyze verify /etc/systemd/system/fitness-agent-pull.service /etc/systemd/system/fitness-agent-pull.timer
if test -x /opt/fitness-agent/current/.venv/bin/python; then
    systemd-analyze verify /etc/systemd/system/fitness-agent.service
fi
echo '初始化文件已安装。请编辑 /etc/fitness-agent.env；尚未启动应用或自动拉取。'
