"""Клиентский бот ПрямойАвтоИмпорт: меню-бот без нейросети + мини-приложение (каталог и подбор).

Что умеет:
  • /start — приветствие с логотипом и кнопки снизу;
  • кнопки: «Каталог и подбор» (мини-приложение), «Этапы работы», «Образец договора», «Отзывы», «Контакты»;
  • заявки из мини-приложения — сразу менеджеру;
  • любое другое сообщение клиента пересылается менеджеру; если менеджер ответит на него (reply)
    в чате с ботом — бот отправит ответ клиенту.

Переменные окружения (лежат на сервере в /etc/direct-bot.env, в репозитории их нет):
  BOT_TOKEN, ADMIN_CHAT_ID, DATA_DIR, [LEADS_CHAT_ID], [APP_URL]
"""
import asyncio
import html
import json
import logging
import os
import sqlite3
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
# куда слать заявки (по умолчанию — Артёму)
LEADS_CHAT_ID = int(os.environ.get("LEADS_CHAT_ID", ADMIN_CHAT_ID))
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
APP_URL = os.environ.get("APP_URL", "https://mkronnen.github.io/direct-zapros/app/")

HERE = Path(__file__).parent
TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
TZ = timezone(timedelta(hours=7))           # Красноярск
DAILY_REPORT_HOUR = 9                       # суточный отчёт в 09:00 по Красноярску
FORWARDS_PER_HOUR = 30                      # защита от флуда: столько сообщений клиента в час пересылаем

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе в журнал попадает URL с токеном бота

# ---------------------------------------------------------------- тексты и кнопки

BTN_APP = "🚗 Каталог и подбор"
BTN_STEPS = "📋 Этапы работы"
BTN_CONTRACT = "📄 Образец договора"
BTN_REVIEWS = "⭐ Отзывы"
BTN_CONTACTS = "📍 Контакты"

KEYBOARD = {"keyboard": [[{"text": BTN_APP, "web_app": {"url": APP_URL}}],
                         [{"text": BTN_STEPS}, {"text": BTN_CONTRACT}],
                         [{"text": BTN_REVIEWS}, {"text": BTN_CONTACTS}]],
            "resize_keyboard": True, "is_persistent": True}

CHANNEL_URL = "https://t.me/directautoimport"

WELCOME = (
    "Здравствуйте! Это бот <b>ПрямойАвтоИмпорт</b> — привозим автомобили под заказ из Китая, Кореи и Японии.\n\n"
    "🚗 <b>Каталог и подбор</b> — машины в наличии с фото и ценой до Уссурийска или заявка на подбор под ваш запрос\n"
    "📋 <b>Этапы работы</b> — как проходит покупка\n"
    "📄 <b>Образец договора</b> — наш агентский договор\n"
    "⭐ <b>Отзывы</b> — что говорят клиенты\n"
    "📍 <b>Контакты</b> — где мы находимся и как связаться\n\n"
    f"Подборки и кейсы — в нашем <a href=\"{CHANNEL_URL}\">Telegram-канале</a>.\n\n"
    "Выбирайте кнопками ниже 👇 или просто напишите вопрос — его получит менеджер."
)

CONTACTS = (
    "📍 <b>Офис</b>\n"
    "г. Красноярск, ул. Ладо Кецховели, 22а, оф. 12-01/2\n"
    "Работаем с клиентами по всей России — договор подписывается дистанционно.\n\n"
    "📞 <b>Связаться с нами</b>\n"
    "Артём Авдеев — <a href=\"tel:+79954315514\">+7 (995) 431-55-14</a>\n"
    "Антон Долгов — <a href=\"tel:+79135155372\">+7 (913) 515-53-72</a>\n\n"
    "🌐 Сайт: <a href=\"https://directautoimport.ru\">directautoimport.ru</a>\n"
    f"✈️ Канал: <a href=\"{CHANNEL_URL}\">@directautoimport</a>"
)

REVIEWS = "⭐ Отзывы наших клиентов — видео с получения, истории покупок и кейсы:"
REVIEWS_BUTTONS = {"inline_keyboard": [
    [{"text": "Отзывы в Telegram", "url": "https://t.me/directautoimport/reviews"}],
    [{"text": "Мы во ВКонтакте", "url": "https://vk.ru/direct_ai"}],
    [{"text": "Наш сайт", "url": "https://directautoimport.ru"}],
]}

FORWARDED_OK = "Спасибо! Передал ваше сообщение менеджеру — он ответит здесь в ближайшее время."


def load_steps() -> str:
    """Текст «Этапы работы» — дословно из answers/process.txt (первая строка-заголовок с # пропускается)."""
    f = HERE / "answers" / "process.txt"
    lines = f.read_text(encoding="utf-8").strip().splitlines()
    if lines and lines[0].startswith("#"):
        lines = lines[1:]
    return "\n".join(lines).strip()


STEPS = load_steps()
CONTRACT_FILE = HERE / "files" / "dogovor_obrazec.pdf"
CONTRACT_NAME = "Образец договора DirectAI (Китай).pdf"
WELCOME_IMG = HERE / "files" / "welcome.jpg"

# ---------------------------------------------------------------- хранилище

DATA_DIR.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DATA_DIR / "bot.sqlite3", check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS messages (chat_id INTEGER, role TEXT, content TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS ix_messages_chat ON messages(chat_id, ts);
CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY, username TEXT, name TEXT, first_seen REAL);
CREATE TABLE IF NOT EXISTS leads (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, type TEXT,
                                  data TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS events (kind TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
""")
db.commit()


def event(kind: str) -> None:
    db.execute("INSERT INTO events VALUES (?, ?)", (kind, time.time()))
    db.commit()


def kv_get(k, default=None):
    row = db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def kv_set(k, v):
    db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (k, str(v)))
    db.commit()


def save_msg(chat_id: int, role: str, content: str) -> None:
    db.execute("INSERT INTO messages VALUES (?, ?, ?, ?)", (chat_id, role, content, time.time()))
    db.commit()


def msgs_last_hour(chat_id: int) -> int:
    return db.execute("SELECT COUNT(*) FROM messages WHERE chat_id=? AND role='user' AND ts>?",
                      (chat_id, time.time() - 3600)).fetchone()[0]


# ---------------------------------------------------------------- telegram

http = httpx.AsyncClient(timeout=httpx.Timeout(70.0, connect=15.0))


async def tg(method: str, **params):
    r = await http.post(f"{TG}/{method}", json=params)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description')}")
    return data["result"]


async def send(chat_id: int, text: str, **extra):
    res = None
    for chunk in [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]:
        res = await tg("sendMessage", chat_id=chat_id, text=chunk, disable_web_page_preview=True, **extra)
    return res


async def send_file(method: str, field: str, chat_id: int, path: Path, filename: str, mime: str, **extra):
    data = {"chat_id": str(chat_id)}
    for k, v in extra.items():
        data[k] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    r = await http.post(f"{TG}/{method}", data=data, files={field: (filename, path.read_bytes(), mime)})
    if not r.json().get("ok"):
        raise RuntimeError(f"{method}: {r.text[:200]}")


# ---------------------------------------------------------------- отчёты об ошибках

class Reporter:
    """Шлёт ошибки Артёму в Telegram. Одинаковые ошибки не чаще раза в 10 минут, со счётчиком."""
    WINDOW = 600

    def __init__(self):
        self.last = {}
        self.suppressed = {}

    async def __call__(self, kind: str, details: str = "", chat_id: int | None = None):
        event("error")
        log.error("%s: %s", kind, details)
        now = time.time()
        if now - self.last.get(kind, 0) < self.WINDOW:
            self.suppressed[kind] = self.suppressed.get(kind, 0) + 1
            return
        extra = self.suppressed.pop(kind, 0)
        self.last[kind] = now
        text = f"⚠️ Ошибка бота: {kind}"
        if chat_id:
            text += f"\nКлиент: {chat_id}"
        if extra:
            text += f"\n(+{extra} таких же за последние 10 мин)"
        if details:
            text += f"\n\n{details[-1500:]}"
        try:
            await send(ADMIN_CHAT_ID, text)
        except Exception as e:  # некуда репортить — только в лог
            log.error("report failed: %s", e)


report = Reporter()

# ---------------------------------------------------------------- пользователи

users: dict[int, dict] = {}


def remember_user(msg: dict) -> dict:
    u = msg.get("from", {})
    info = {"username": u.get("username"),
            "name": " ".join(x for x in [u.get("first_name"), u.get("last_name")] if x)}
    chat_id = msg["chat"]["id"]
    users[chat_id] = info
    if db.execute("SELECT 1 FROM users WHERE chat_id=?", (chat_id,)).fetchone() is None:
        db.execute("INSERT INTO users VALUES (?, ?, ?, ?)", (chat_id, info["username"], info["name"], time.time()))
        db.commit()
        event("new_user")
    return info


def user_link(chat_id: int, user: dict) -> str:
    e = html.escape
    link = f'<a href="tg://user?id={chat_id}">{e(user.get("name") or "клиент")}</a>'
    if user.get("username"):
        link += f" (@{e(user['username'])})"
    return link


# ---------------------------------------------------------------- разделы меню

async def send_welcome(chat_id: int):
    try:
        if WELCOME_IMG.exists():
            await send_file("sendPhoto", "photo", chat_id, WELCOME_IMG, "welcome.jpg", "image/jpeg",
                            caption=WELCOME, parse_mode="HTML", reply_markup=KEYBOARD)
            return
    except Exception:
        await report("не отправилось фото приветствия", traceback.format_exc(), chat_id)
    await send(chat_id, WELCOME, parse_mode="HTML", reply_markup=KEYBOARD)


async def send_contract(chat_id: int):
    await send_file("sendDocument", "document", chat_id, CONTRACT_FILE, CONTRACT_NAME, "application/pdf",
                    caption="Образец нашего агентского договора. Цифры и данные заполняются под вас.",
                    reply_markup=KEYBOARD)


async def on_menu(chat_id: int, text: str) -> bool:
    """Нажатие кнопки меню. True — если обработали."""
    if text == BTN_STEPS:
        await send(chat_id, STEPS, reply_markup=KEYBOARD)
        event("steps")
    elif text == BTN_CONTRACT:
        await send_contract(chat_id)
        event("contract")
    elif text == BTN_REVIEWS:
        await send(chat_id, REVIEWS, reply_markup=REVIEWS_BUTTONS)
        event("reviews")
    elif text == BTN_CONTACTS:
        await send(chat_id, CONTACTS, parse_mode="HTML", reply_markup=KEYBOARD)
        event("contacts")
    elif text == BTN_APP:   # нажали кнопку как текст (старый клиент Telegram без мини-приложений)
        await send(chat_id, "Откройте каталог кнопкой «🚗 Каталог и подбор» внизу экрана. Если она не открывается — "
                            "обновите Telegram или напишите, какую машину ищете, и менеджер ответит здесь.",
                   reply_markup=KEYBOARD)
    else:
        return False
    return True


# ---------------------------------------------------------------- заявки из мини-приложения

CONTACT_LABEL = {"tg": "Telegram", "wa": "WhatsApp", "call": "Звонок"}


async def on_webapp(chat_id: int, user: dict, raw: str):
    try:
        d = json.loads(raw)
    except Exception:
        await report("мини-приложение прислало не JSON", raw[:500], chat_id)
        return
    e = lambda v: html.escape(str(v))
    is_car = d.get("kind") == "car"
    kind = "наличие" if is_car else "подбор"
    cur = db.execute("INSERT INTO leads (chat_id, type, data, ts) VALUES (?, ?, ?, ?)",
                     (chat_id, kind, json.dumps(d, ensure_ascii=False), time.time()))
    db.commit()
    lead_id = cur.lastrowid
    event("lead_app")
    title = "🚗 Хочет машину из наличия" if is_car else "🔎 Заявка на подбор"
    lines = [f"{title} — №{lead_id}", f"Клиент в Telegram: {user_link(chat_id, user)}", ""]
    if is_car:
        c = d.get("car", {})
        lines += [f"<b>{e(c.get('title', ''))}</b>", f"Лот: {e(c.get('lot', ''))}"]
        for k, label in [("trim", "Комплектация"), ("made", "Выпуск"), ("km", "Пробег"), ("price", "Цена в приложении")]:
            if c.get(k):
                lines.append(f"{label}: {e(c[k])}")
    else:
        for k, label in [("car", "Марка/модель"), ("years", "Годы"), ("km", "Пробег"), ("cond", "Состояние"),
                         ("colors", "Цвета"), ("budget", "Бюджет"), ("city", "Город доставки")]:
            if d.get(k):
                lines.append(f"{label}: {e(d[k])}")
    lines += ["", f"Имя: {e(d.get('name', ''))}", f"Телефон: {e(d.get('phone', ''))}",
              f"Связь: {e(CONTACT_LABEL.get(d.get('contact'), d.get('contact', '')))}"
              + (f" (@{e(user['username'])})" if d.get("contact") == "tg" and user.get("username") else ""),
              f"Промокод: {e(d['promo'])}" if d.get("promo") else "Промокод: —",
              "", "↩️ Ответьте на это сообщение — ответ уйдёт клиенту в бот."]
    if chat_id == ADMIN_CHAT_ID:
        lines.insert(0, "🧪 ТЕСТ")
    sent = await send(LEADS_CHAT_ID, "\n".join(lines), parse_mode="HTML")
    kv_set(f"relay:{LEADS_CHAT_ID}:{sent['message_id']}", chat_id)
    how = {"tg": "здесь, в Telegram", "wa": "в WhatsApp", "call": "по телефону"}.get(d.get("contact"), "")
    await send(chat_id, f"Заявка №{lead_id} отправлена менеджеру — он свяжется с вами {how} в ближайшее время. "
                        "Если появятся вопросы, пишите сюда.", reply_markup=KEYBOARD)
    save_msg(chat_id, "system", f"заявка №{lead_id} ({kind})")


# ---------------------------------------------------------------- сообщения клиентов → менеджеру

async def forward_to_manager(msg: dict, user: dict):
    chat_id = msg["chat"]["id"]
    text = msg.get("text") or msg.get("caption") or ""
    save_msg(chat_id, "user", text or "[вложение]")
    if msgs_last_hour(chat_id) > FORWARDS_PER_HOUR:
        return   # флуд — молча не пересылаем
    head = await send(LEADS_CHAT_ID, f"💬 Сообщение в боте от {user_link(chat_id, user)}"
                                     + (" — 🧪 ТЕСТ" if chat_id == ADMIN_CHAT_ID else "")
                                     + "\n↩️ Ответьте на сообщение ниже — ответ уйдёт клиенту.", parse_mode="HTML")
    kv_set(f"relay:{LEADS_CHAT_ID}:{head['message_id']}", chat_id)
    fwd = await tg("forwardMessage", chat_id=LEADS_CHAT_ID, from_chat_id=chat_id, message_id=msg["message_id"])
    kv_set(f"relay:{LEADS_CHAT_ID}:{fwd['message_id']}", chat_id)
    event("forward")
    # не отвечаем «передал» на каждое сообщение подряд — максимум раз в 10 минут
    if time.time() - float(kv_get(f"ack:{chat_id}", 0)) > 600:
        kv_set(f"ack:{chat_id}", time.time())
        await send(chat_id, FORWARDED_OK, reply_markup=KEYBOARD)


async def relay_reply(msg: dict) -> bool:
    """Менеджер ответил (reply) на пересланное сообщение — копируем ответ клиенту."""
    reply = msg.get("reply_to_message")
    if not reply:
        return False
    target = kv_get(f"relay:{msg['chat']['id']}:{reply['message_id']}")
    if not target:
        return False
    target = int(target)
    try:
        await tg("copyMessage", chat_id=target, from_chat_id=msg["chat"]["id"], message_id=msg["message_id"])
        save_msg(target, "manager", msg.get("text") or msg.get("caption") or "[вложение]")
        event("relay")
    except Exception as e:
        await send(msg["chat"]["id"], f"Не удалось отправить клиенту: {e}")
        return True
    try:   # 👍 под ответом — значит, ушло клиенту
        await tg("setMessageReaction", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                 reaction=[{"type": "emoji", "emoji": "👍"}])
    except Exception:
        await send(msg["chat"]["id"], "✅ Отправлено клиенту")
    return True


# ---------------------------------------------------------------- приём сообщений

async def on_message(msg: dict):
    chat = msg.get("chat", {})
    if chat.get("type") != "private" and chat.get("id") != LEADS_CHAT_ID:
        return
    chat_id = chat["id"]
    # ответы менеджера (в личке с ботом или в рабочем чате заявок)
    if chat_id in (ADMIN_CHAT_ID, LEADS_CHAT_ID) and await relay_reply(msg):
        return
    if chat.get("type") != "private":
        return
    user = remember_user(msg)
    if msg.get("web_app_data"):
        try:
            await on_webapp(chat_id, user, msg["web_app_data"].get("data", ""))
        except Exception:
            await report("сбой заявки из мини-приложения", traceback.format_exc(), chat_id)
            await safe_send(chat_id, "Не получилось отправить заявку автоматически — я уже сообщил менеджеру, "
                                     "он свяжется с вами.")
        return
    text = (msg.get("text") or "").strip()
    if text.startswith("/start"):
        await send_welcome(chat_id)
        event("start")
        return
    if text.startswith("/stats") and chat_id == ADMIN_CHAT_ID:
        await send(chat_id, stats_text(24))
        return
    if text.startswith("/logs") and chat_id == ADMIN_CHAT_ID:
        proc = await asyncio.create_subprocess_exec(
            "journalctl", "-u", "direct-bot", "-n", "40", "--no-pager", "-o", "short",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        await send(chat_id, (out.decode(errors="replace")[-3800:] or "журнал пуст"))
        return
    if text and await on_menu(chat_id, text):
        return
    await forward_to_manager(msg, user)


async def safe_send(chat_id, text):
    try:
        await send(chat_id, text)
    except Exception as e:
        log.error("safe_send: %s", e)


# ---------------------------------------------------------------- отчёты

def stats_text(hours: int) -> str:
    since = time.time() - hours * 3600
    cnt = lambda kind: db.execute("SELECT COUNT(*) FROM events WHERE kind=? AND ts>?", (kind, since)).fetchone()[0]
    return (f"📊 Бот за {hours} ч\n"
            f"Новых клиентов: {cnt('new_user')}, нажали /start: {cnt('start')}\n"
            f"Заявок из приложения: {cnt('lead_app')}\n"
            f"Сообщений переслано вам: {cnt('forward')}, ваших ответов клиентам: {cnt('relay')}\n"
            f"Открыли: этапы {cnt('steps')}, договор {cnt('contract')}, отзывы {cnt('reviews')}, "
            f"контакты {cnt('contacts')}\n"
            f"Ошибок: {cnt('error')}")


async def daily_report_loop():
    while True:
        now = datetime.now(TZ)
        today = now.strftime("%Y-%m-%d")
        if now.hour >= DAILY_REPORT_HOUR and kv_get("daily_report") != today:
            kv_set("daily_report", today)
            try:
                await send(ADMIN_CHAT_ID, stats_text(24))
            except Exception as e:
                log.error("daily report: %s", e)
        await asyncio.sleep(60)


def version() -> str:
    try:
        git = HERE.parent / ".git"
        head = (git / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1]
            p = git / ref
            if p.exists():
                return p.read_text().strip()[:7]
            for line in (git / "packed-refs").read_text().splitlines():
                if line.endswith(ref):
                    return line[:7]
        return head[:7]
    except Exception:
        return "?"


# ---------------------------------------------------------------- главный цикл

async def main():
    me = await tg("getMe")
    await tg("deleteWebhook", drop_pending_updates=False)
    await tg("setMyCommands", commands=[{"command": "start", "description": "Главное меню"}])
    await send(ADMIN_CHAT_ID, f"🤖 Бот @{me['username']} запущен, версия {version()}.")
    asyncio.create_task(daily_report_loop())
    offset = int(kv_get("offset", 0))
    fails = 0
    while True:
        try:
            updates = await tg("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
            fails = 0
        except Exception as e:
            fails += 1
            log.warning("getUpdates failed (%s): %s", fails, e)
            if fails == 3:
                await report("нет связи с Telegram (3 сбоя подряд)", repr(e))
            await asyncio.sleep(min(60, 2 * fails))
            continue
        for u in updates:
            offset = u["update_id"] + 1
            kv_set("offset", offset)
            if "message" in u:
                try:
                    await on_message(u["message"])
                except Exception:
                    await report("сбой при приёме сообщения", traceback.format_exc())


if __name__ == "__main__":
    asyncio.run(main())
