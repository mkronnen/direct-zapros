#!/bin/bash
# Запускается на сервере раз в минуту после git pull (из /usr/local/bin/bot-update).
# Ставит зависимости, перезапускает бота при новой версии и сообщает о сбоях в Telegram.
cd /opt/bot || exit 0
. /etc/direct-bot.env

tg() {
  curl -s -m 20 "https://api.telegram.org/bot$BOT_TOKEN/sendMessage" \
    -d chat_id="$ADMIN_CHAT_ID" --data-urlencode text="$1" > /dev/null
}
journal_tail() { journalctl -u direct-bot -n 20 --no-pager -o cat | tail -c 1500; }

# боту нужен доступ к своему журналу для команды /logs
id -nG bot | grep -qw systemd-journal || usermod -aG systemd-journal bot

# ---------- веб-сервер Caddy (HTTPS) для мини-приложения: ставим один раз, конфиг берём из репозитория
CADDY_VER=2.8.4
if [ ! -x /usr/local/bin/caddy ]; then
  case $(uname -m) in x86_64) A=amd64;; aarch64) A=arm64;; *) A=amd64;; esac
  if curl -fsSL -m 180 "https://github.com/caddyserver/caddy/releases/download/v$CADDY_VER/caddy_${CADDY_VER}_linux_$A.tar.gz" -o /tmp/caddy.tgz \
     && tar xzf /tmp/caddy.tgz -C /usr/local/bin caddy; then
    chmod +x /usr/local/bin/caddy
  else
    tg "⚠️ Не удалось скачать Caddy (веб-сервер приложения), попробую через минуту"
  fi
  rm -f /tmp/caddy.tgz
fi
if [ -x /usr/local/bin/caddy ]; then
  mkdir -p /etc/caddy /var/lib/caddy
  if [ ! -f /etc/systemd/system/caddy.service ]; then
    cat > /etc/systemd/system/caddy.service <<'UNIT'
[Unit]
Description=Caddy (HTTPS for mini app)
After=network-online.target

[Service]
Environment=XDG_DATA_HOME=/var/lib XDG_CONFIG_HOME=/etc
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
Restart=always
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
  fi
  # адрес сайта по собственному IP сервера: 1.2.3.4 -> 1-2-3-4.sslip.io (работает при любом переезде без DNS)
  IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')
  [ -n "$IP" ] || IP=$(hostname -I | awk '{print $1}')
  SITE="$(echo "$IP" | tr . -).sslip.io"
  echo "https://$SITE/" > /opt/bot/data/public_url.new
  if ! cmp -s /opt/bot/data/public_url.new /opt/bot/data/public_url; then
    mv /opt/bot/data/public_url.new /opt/bot/data/public_url; chown bot:bot /opt/bot/data/public_url
  else
    rm -f /opt/bot/data/public_url.new
  fi
  sed "s/SITE_ADDRESS/$SITE/" app/bot/deploy/Caddyfile > /tmp/Caddyfile.new
  if ! cmp -s /tmp/Caddyfile.new /etc/caddy/Caddyfile; then
    cp /tmp/Caddyfile.new /etc/caddy/Caddyfile
    if command -v ufw > /dev/null && ufw status | grep -q "Status: active"; then ufw allow 80/tcp; ufw allow 443/tcp; fi
    if systemctl is-active -q caddy; then systemctl reload caddy; else systemctl enable --now caddy; fi
    sleep 15
    if systemctl is-active -q caddy; then
      tg "🌐 Веб-сервер приложения запущен: https://$SITE/"
    else
      tg "⚠️ Caddy не запустился
$(journalctl -u caddy -n 15 --no-pager -o cat | tail -c 1200)"
    fi
  fi
  systemctl is-active -q caddy || systemctl start caddy
fi

HEAD=$(git -C app rev-parse --short HEAD)
LAST=$(cat .deployed 2>/dev/null)

if [ "$HEAD" != "$LAST" ]; then
  if ! venv/bin/python -m pip --version > /dev/null 2>&1; then
    rm -rf venv && python3 -m venv --system-site-packages venv
  fi
  if ! OUT=$(venv/bin/python -m pip install -q -r app/bot/requirements.txt 2>&1); then
    tg "⚠️ Обновление $HEAD: не удалось поставить зависимости
$(echo "$OUT" | tail -5)"
  fi
  echo "$HEAD" > .deployed
  systemctl restart direct-bot
  sleep 20
  if ! systemctl is-active -q direct-bot || journalctl -u direct-bot --since "-25s" --no-pager -o cat | grep -q "Failed with result"; then
    tg "⚠️ Обновление $HEAD: бот падает после запуска
$(journal_tail)"
  fi
  exit 0
fi

# Проверка здоровья: если бот лежит или падает — сообщить (не чаще раза в 30 минут) и поднять.
if ! systemctl is-active -q direct-bot || journalctl -u direct-bot --since "-2min" --no-pager -o cat | grep -q "Failed with result"; then
  NOW=$(date +%s)
  PREV=$(cat .last_crash_report 2>/dev/null || echo 0)
  if [ $((NOW - PREV)) -gt 1800 ]; then
    echo "$NOW" > .last_crash_report
    tg "⚠️ Бот не работает или падает
$(journal_tail)"
  fi
  systemctl is-active -q direct-bot || systemctl start direct-bot
fi
