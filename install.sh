#!/usr/bin/env bash
# Установка vk-notify на чистую ноду одним запуском (с локальной машины).
#
#   ./install.sh                          — поставить на хост по умолчанию
#   DEPLOY_HOST=root@1.2.3.4 ./install.sh — на другую ноду
#
# Требуется локальный .env рядом со скриптом: cp .env.example .env + вписать токены.
# Один SSH-коннект = один ввод пароля. Повторный запуск безопасен (переустановка).
set -euo pipefail

HOST="${DEPLOY_HOST:-root@1.2.3.4}"
APP_DIR=/opt/vk-notify

cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
    echo "Нет .env — выполни: cp .env.example .env и впиши токены" >&2
    exit 1
fi

PY=python3
command -v python3 >/dev/null || PY=python
$PY -m py_compile vk_notify.py
echo "syntax OK, installing to $HOST..."

tar czf - vk_notify.py requirements.txt vk-notify.service .env | ssh "$HOST" "
    set -e
    mkdir -p $APP_DIR
    tar xzf - --no-same-owner -C $APP_DIR
    chmod 600 $APP_DIR/.env
    mv $APP_DIR/vk-notify.service /etc/systemd/system/
    apt-get install -y -qq python3-venv >/dev/null 2>&1 || true
    python3 -m venv $APP_DIR/.venv
    $APP_DIR/.venv/bin/pip install -q -r $APP_DIR/requirements.txt
    systemctl daemon-reload
    systemctl enable vk-notify >/dev/null 2>&1
    systemctl restart vk-notify
    sleep 3
    systemctl is-active vk-notify
    journalctl -u vk-notify --no-pager -n 5"

echo "install done"
