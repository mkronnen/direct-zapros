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
