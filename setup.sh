#!/usr/bin/env bash
# Установка vk-notify прямо на сервере одной командой:
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/BigDrugs/vk-notify/main/setup.sh)
#
# Клонирует репозиторий в /opt/vk-notify, спрашивает токены, поднимает systemd-сервис.
# Повторный запуск обновляет код (git pull) и не трогает существующий .env.
set -euo pipefail

REPO="https://github.com/BigDrugs/vk-notify.git"
APP_DIR=/opt/vk-notify

if [[ $EUID -ne 0 ]]; then
    echo "Нужен root: запусти через sudo -i" >&2
    exit 1
fi

echo "== vk-notify: установка =="
apt-get update -qq >/dev/null 2>&1 || true
apt-get install -y -qq git python3 python3-venv >/dev/null

if [[ -d $APP_DIR/.git ]]; then
    git -C "$APP_DIR" pull -q
elif [[ -d $APP_DIR ]]; then
    # старая не-git установка: сохраняем .env, каталог пересоздаём клоном
    tmp=$(mktemp -d)
    [[ -f $APP_DIR/.env ]] && cp "$APP_DIR/.env" "$tmp/.env"
    rm -rf "$APP_DIR"
    git clone -q "$REPO" "$APP_DIR"
    [[ -f $tmp/.env ]] && cp "$tmp/.env" "$APP_DIR/.env"
    rm -rf "$tmp"
else
    git clone -q "$REPO" "$APP_DIR"
fi

if [[ ! -f $APP_DIR/.env ]]; then
    echo
    echo "Нужны три значения (Enter после каждого):"
    echo
    echo "1) VK-токен: открой vkhost.github.io → Kate Mobile → «Разрешить»,"
    echo "   из адресной строки скопируй access_token=... (до &)"
    read -rp "VK_TOKEN: " VK_TOKEN
    echo
    echo "2) Токен бота: в Telegram @BotFather → /newbot"
    read -rp "TG_BOT_TOKEN: " TG_BOT_TOKEN
    echo
    echo "3) Твой chat_id: спроси у @userinfobot."
    echo "   Важно: напиши своему боту /start, иначе он не сможет тебе писать!"
    read -rp "TG_CHAT_ID: " TG_CHAT_ID

    cat > "$APP_DIR/.env" <<ENV
VK_TOKEN=$VK_TOKEN
TG_BOT_TOKEN=$TG_BOT_TOKEN
TG_CHAT_ID=$TG_CHAT_ID
ONLY_PRIVATE=false
FORWARD_TEXT=true
MUTED_MODE=skip
IGNORE_PEER_IDS=
ENV
    chmod 600 "$APP_DIR/.env"
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
cp "$APP_DIR/vk-notify.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable vk-notify >/dev/null 2>&1
systemctl restart vk-notify
sleep 3

if systemctl is-active --quiet vk-notify; then
    echo
    echo "✅ Установлено и запущено. Проверь Telegram — бот должен был написать."
    echo "Логи:      journalctl -u vk-notify -f"
    echo "Настройки: nano $APP_DIR/.env && systemctl restart vk-notify"
else
    echo
    echo "❌ Сервис не запустился. Логи:"
    journalctl -u vk-notify --no-pager -n 10
    exit 1
fi
