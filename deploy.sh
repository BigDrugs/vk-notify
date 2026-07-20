#!/usr/bin/env bash
# Деплой vk-notify на ноду. Один SSH-коннект = один ввод пароля.
#
#   ./deploy.sh          — залить vk_notify.py и перезапустить сервис
#   ./deploy.sh --full   — также requirements.txt + vk-notify.service (+ pip install)
#
# Другая нода: DEPLOY_HOST=root@1.2.3.4 ./deploy.sh
set -euo pipefail

HOST="${DEPLOY_HOST:-root@1.2.3.4}"
APP_DIR=/opt/vk-notify

cd "$(dirname "$0")"

PY=python3
command -v python3 >/dev/null || PY=python
$PY -m py_compile vk_notify.py
echo "syntax OK, deploying to $HOST..."

if [[ "${1:-}" == "--full" ]]; then
    tar czf - vk_notify.py requirements.txt vk-notify.service | ssh "$HOST" "
        set -e
        tar xzf - --no-same-owner -C $APP_DIR
        mv $APP_DIR/vk-notify.service /etc/systemd/system/
        $APP_DIR/.venv/bin/pip install -q -r $APP_DIR/requirements.txt
        systemctl daemon-reload
        systemctl restart vk-notify
        sleep 3
        systemctl is-active vk-notify
        journalctl -u vk-notify --no-pager -n 5"
else
    ssh "$HOST" "
        set -e
        cat > $APP_DIR/vk_notify.py
        systemctl restart vk-notify
        sleep 3
        systemctl is-active vk-notify
        journalctl -u vk-notify --no-pager -n 5" < vk_notify.py
fi

echo "deploy done"
