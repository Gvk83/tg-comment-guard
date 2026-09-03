#!/usr/bin/env bash
# Установка бота на чистый сервер Ubuntu 22.04/24.04. Запускать от root.
set -euo pipefail

APP_DIR=/opt/moderator
SERVICE=moderator

echo "==> Обновление системы и установка Python"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip

PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "    Python $PY_VER"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
    echo "ОШИБКА: нужен Python 3.10 или новее, найден $PY_VER" >&2
    exit 1
}

echo "==> Создание пользователя botuser"
id -u botuser >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin botuser

echo "==> Подготовка каталога $APP_DIR"
mkdir -p "$APP_DIR/data"

echo "==> Виртуальное окружение и зависимости"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> Права"
chown -R botuser:botuser "$APP_DIR"
chmod 600 "$APP_DIR/.env" 2>/dev/null || true

echo "==> Часовой пояс и автообновления безопасности"
timedatectl set-timezone Europe/Moscow || true
apt-get install -y -qq unattended-upgrades
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

echo "==> Служба systemd"
cp "$APP_DIR/deploy/moderator.service" /etc/systemd/system/$SERVICE.service
systemctl daemon-reload
systemctl enable $SERVICE
systemctl restart $SERVICE

sleep 3
systemctl --no-pager status $SERVICE | head -12
echo
echo "Готово. Журнал: journalctl -u $SERVICE -f"
