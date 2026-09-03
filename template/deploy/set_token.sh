#!/usr/bin/env bash
# Записывает токен бота прямо на сервер. Токен нигде не сохраняется
# на вашем компьютере и не попадает в историю команд.
#
# Использование:  bash deploy/set_token.sh root@IP-АДРЕС-СЕРВЕРА
set -euo pipefail

SERVER="${1:-}"
KEY="${SSH_KEY:-$HOME/.ssh/moderator_bot}"

if [ -z "$SERVER" ]; then
    echo "Укажите сервер: bash deploy/set_token.sh root@85.12.34.56" >&2
    exit 1
fi

printf 'Вставьте токен от @BotFather и нажмите Enter (ввод не виден): '
read -rs TOKEN
printf '\n'

if [ -z "${TOKEN}" ] || [[ "${TOKEN}" != *:* ]]; then
    echo "Это не похоже на токен. Токен выглядит так: 1234567890:AAF..." >&2
    exit 1
fi

printf '%s' "$TOKEN" | ssh -i "$KEY" -o BatchMode=yes "$SERVER" '
    read -r T
    sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=${T}|" /opt/moderator/.env
    chmod 600 /opt/moderator/.env
    chown botuser:botuser /opt/moderator/.env
    systemctl restart moderator
    sleep 6
    echo "--- Состояние службы: $(systemctl is-active moderator) ---"
    journalctl -u moderator -n 8 --no-pager | sed "s/[0-9]\{8,\}:[A-Za-z0-9_-]\{30,\}/***ТОКЕН СКРЫТ***/g"
'
