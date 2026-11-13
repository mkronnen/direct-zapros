#!/bin/bash
# Установка бота @DirectAutoImport_bot на новый сервер (Ubuntu 24.04).
# Вставь этот текст целиком в поле «Cloud config» / «User data» при создании сервера.
# ============ ВСТАВЬ ТОКЕН БОТА ВМЕСТО ТЕКСТА В КАВЫЧКАХ ============
BOT_TOKEN="ВСТАВЬ_ТОКЕН_БОТА"
# ===================================================================
ADMIN_CHAT_ID="2031584462"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git curl sqlite3

useradd -r -m -d /opt/bot -s /usr/sbin/nologin bot || true
mkdir -p /opt/bot/data && chown bot:bot /opt/bot/data

printf 'BOT_TOKEN=%s\nADMIN_CHAT_ID=%s\nDATA_DIR=/opt/bot/data\n' "$BOT_TOKEN" "$ADMIN_CHAT_ID" > /etc/direct-bot.env
chmod 640 /etc/direct-bot.env && chgrp bot /etc/direct-bot.env

# Каждую минуту: забрать код с GitHub и запустить deploy/update.sh из репозитория
# (он ставит зависимости, веб-сервер Caddy с HTTPS, перезапускает бота и сообщает о сбоях в Telegram).
cat > /usr/local/bin/bot-update <<'UPD'
#!/bin/bash
cd /opt/bot || exit 0
if [ ! -d app/.git ]; then
  git clone -q https://github.com/mkronnen/direct-zapros.git app || exit 0
else
  git -C app pull -q --ff-only || true
fi
[ -f app/bot/deploy/update.sh ] && bash app/bot/deploy/update.sh
UPD
chmod +x /usr/local/bin/bot-update

cat > /etc/systemd/system/direct-bot.service <<'UNIT'
[Unit]
Description=Direct client bot
After=network-online.target
ConditionPathExists=/opt/bot/app/bot/main.py

[Service]
User=bot
EnvironmentFile=/etc/direct-bot.env
WorkingDirectory=/opt/bot/app/bot
ExecStart=/opt/bot/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/bot-update.service <<'UNIT'
[Unit]
Description=Pull bot code from GitHub and deploy
[Service]
Type=oneshot
ExecStart=/usr/local/bin/bot-update
UNIT

cat > /etc/systemd/system/bot-update.timer <<'UNIT'
[Unit]
Description=Pull bot code every minute
[Timer]
OnBootSec=30
OnUnitActiveSec=60
[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable direct-bot.service
systemctl enable --now bot-update.timer

curl -s -m 20 "https://api.telegram.org/bot$BOT_TOKEN/sendMessage" -d chat_id="$ADMIN_CHAT_ID" \
  --data-urlencode text="🖥 Новый сервер бота настроен. IP: $(hostname -I | awk '{print $1}'). Через 1–3 минуты бот запустится сам." > /dev/null
