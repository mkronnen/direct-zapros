"""Клиентский бот ПрямойАвтоИмпорт: меню-бот без нейросети + мини-приложение (каталог и подбор).

Что умеет:
  • /start — приветствие с логотипом и кнопки снизу;
  • кнопки: «Каталог и подбор» (мини-приложение), «Этапы работы», «Образец договора», «Отзывы», «Контакты»;
  • заявки из мини-приложения — сразу менеджеру;
  • любое другое сообщение клиента пересылается менеджеру; если менеджер ответит на него (reply)
    в чате с ботом — бот отправит ответ клиенту;
  • Артём присылает Excel китайцев — бот обновляет таблицу наличия (sync.py);
  • каждый день в 12:00 МСК — подборка машин в канал по темам из вкладки «Темы» (posts.py).

Переменные окружения (лежат на сервере в /etc/direct-bot.env, в репозитории их нет):
  BOT_TOKEN, ADMIN_CHAT_ID, DATA_DIR, [LEADS_CHAT_ID], [APP_URL], [CHANNEL_ID]
"""
import asyncio
import html
import json
import logging
import os
import re
import sqlite3
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from PIL import Image

import posts
import sync
import web

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
MSK = timezone(timedelta(hours=3))
POST_HOUR = 12                              # подборка в канал в 12:00 по Москве
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@directautoimport")
BOT_USERNAME = "DirectAutoImport_bot"       # уточняется при запуске (getMe)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе в журнал попадает URL с токеном бота

# ---------------------------------------------------------------- тексты и кнопки

BTN_APP = "🚗 Каталог и подбор"
BTN_STEPS = "📋 Этапы работы"
BTN_CONTRACT = "📄 Образец договора"
BTN_REVIEWS = "⭐ Отзывы"
BTN_CONTACTS = "📍 Контакты"
BTN_CHANNEL = "📢 Наш канал"
BTN_CHAT = "💬 Наш чат"

CHANNEL_URL = "https://t.me/directautoimport"
CHAT_URL = "https://t.me/+0WVWNO0CagM5ZTIy"
REVIEWS_URL = "https://t.me/+qnIV-9KL7lYwNDUy"


def kb(chat_id: int | None = None) -> dict:
    """Клавиатура снизу. Если клиент пришёл по реферальной ссылке — промокод уходит в мини-приложение."""
    # v=версия репозитория: после каждого обновления приложения у клиентов открывается свежая версия, а не из кэша Telegram
    url = APP_URL + ("&" if "?" in APP_URL else "?") + "v=" + app_version()
    promo = kv_get(f"promo:{chat_id}") if chat_id else None
    if promo:
        url += "&promo=" + promo
    return {"keyboard": [[{"text": BTN_APP, "web_app": {"url": url}, "style": "danger"}],   # danger = красная кнопка
                         [{"text": BTN_CHANNEL}, {"text": BTN_CHAT}],
                         [{"text": BTN_STEPS}, {"text": BTN_CONTRACT}],
                         [{"text": BTN_REVIEWS}, {"text": BTN_CONTACTS}]],
            "resize_keyboard": True, "is_persistent": True}


WELCOME = (
    "Здравствуйте! Это бот <b>ПрямойАвтоИмпорт</b> — привозим автомобили под заказ из Китая, Кореи и Японии.\n\n"
    "🚗 <b>Каталог и подбор</b> — машины в наличии с фото и ценой до Уссурийска или заявка на подбор под ваш запрос\n"
    "📢 <b>Наш канал</b> — подборки, кейсы, новости\n"
    "💬 <b>Наш чат</b> — общение с клиентами и командой\n"
    "📋 <b>Этапы работы</b> — как проходит покупка\n"
    "📄 <b>Образец договора</b> — наш агентский договор\n"
    "⭐ <b>Отзывы</b> — что говорят клиенты\n"
    "📍 <b>Контакты</b> — где мы находимся и как связаться\n\n"
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
    [{"text": "Отзывы в Telegram", "url": REVIEWS_URL}],
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
CREATE TABLE IF NOT EXISTS posted (lot TEXT, topic INTEGER, ts REAL);
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


async def send_photo_bytes(chat_id, data: bytes, **extra):
    form = {"chat_id": str(chat_id)}
    for k, v in extra.items():
        form[k] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    r = await http.post(f"{TG}/sendPhoto", data=form, files={"photo": ("post.jpg", data, "image/jpeg")})
    res = r.json()
    if not res.get("ok"):
        raise RuntimeError(f"sendPhoto: {r.text[:200]}")
    return res["result"]


# ---------------------------------------------------------------- обновление наличия из Excel

SYNC_PENDING: dict[int, dict] = {}


async def sheet_call(payload: dict) -> dict:
    payload = dict(payload, key=sync.sheet_key(BOT_TOKEN))
    r = await http.post(sync.APPS_URL, json=payload, timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=True)
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"таблица ответила не JSON (код {r.status_code}): {r.text[:300]}")
    if data.get("error"):
        raise RuntimeError("таблица: " + data["error"])
    return data


async def sync_start(chat_id: int, doc: dict):
    e = html.escape
    status = await send(chat_id, "📥 Получил файл, разбираю…")
    edit = lambda t, **kw: tg("editMessageText", chat_id=chat_id, message_id=status["message_id"], text=t,
                              parse_mode="HTML", disable_web_page_preview=True, **kw)
    try:
        f = await tg("getFile", file_id=doc["file_id"])
        raw = (await http.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{f['file_path']}")).content
        rows = sync.parse_excel(raw, doc.get("file_name", "x.xlsx"))
        if not rows:
            await edit("Не нашёл в файле ни одной машины. Это тот же формат, что присылают китайцы (ссылка на сайт в первой колонке)?")
            return
        await edit(f"📥 В файле {len(rows)} машин. Сверяю с таблицей и проверяю сайт…")
        diff = await sheet_call({"action": "diff", "lots": [r["lot"] for r in rows]})
        fresh = set(sync.norm(l) for l in diff["fresh"])
        # проверяем все страницы: новые — забираем данные, у остальных — что машина ещё продаётся
        sem = asyncio.Semaphore(6)
        site: dict[str, dict | None] = {}
        async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as client:
            async def one(r):
                async with sem:
                    try:
                        site[r["lot"]] = await sync.fetch_page(client, r["url"])
                    except Exception:
                        site[r["lot"]] = "error"
            await asyncio.gather(*(one(r) for r in rows))
        errors = [r for r in rows if site.get(r["lot"]) == "error"]
        if len(errors) > len(rows) // 2:
            await edit("⚠️ Сайт avtomirhrb.ru не отвечает с сервера — обновление отменил, таблица не тронута. Попробуйте позже.")
            return
        sold = [r for r in rows if site.get(r["lot"]) is None]          # страница 404 — машина продана
        out, added, broken = [], [], []
        for r in rows:
            s = site.get(r["lot"])
            if s is None:
                continue
            if sync.norm(r["lot"]) in fresh:
                if s == "error":
                    broken.append(r)
                    continue
                x = sync.enrich(r, s)
                out.append(x)
                added.append(x)
            else:
                out.append(r)
        hide = [m["lot"] for m in diff["missing"]] + [r["lot"] for r in sold]
        SYNC_PENDING[chat_id] = {"rows": out, "hide": hide}
        lines = [f"📊 <b>Файл {e(doc.get('file_name', ''))}</b>: {len(rows)} машин", "",
                 f"♻️ Обновятся цена, пробег, состояние, тест: {len(out) - len(added)}"]
        if added:
            lines.append(f"🆕 Новых: {len(added)}")
            lines += [f"   • {e(x['brand'])} {e(x['model'])} {e(x['trim'])} — {x['price']:,} ¥".replace(",", " ") for x in added[:15]]
            if len(added) > 15:
                lines.append(f"   …и ещё {len(added) - 15}")
        if hide:
            lines.append(f"🙈 Скрою (нет в файле или страница на сайте удалена): {len(hide)}")
            names = {m["lot"]: m["title"] for m in diff["missing"]}
            names.update({r["lot"]: f"{r['brand']} {r['model']}" for r in sold})
            lines += [f"   • {e(names.get(l, ''))} (лот {e(l)})" for l in hide[:15]]
            if len(hide) > 15:
                lines.append(f"   …и ещё {len(hide) - 15}")
        if broken:
            lines.append(f"⚠️ Не открылись страницы {len(broken)} новых машин — их пропущу: "
                         + ", ".join(e(r["lot"]) for r in broken[:10]))
        sold_n = {sync.norm(r["lot"]) for r in sold}
        manual = [l for l in diff.get("manualHidden", []) if sync.norm(l) not in sold_n]
        if manual:
            lines.append(f"✋ Есть в файле, но скрыты вручную — не трогаю: {', '.join(e(str(l)) for l in manual[:10])}")
        if added:
            lines.append("\nФото у новых машин — по стандартной схеме дилера (кузов, торпедо, сиденья). Проверьте карточки после обновления.")
        warn = len(hide) > max(10, len(rows) // 4)
        if warn:
            lines.append("\n⚠️ Скрыть нужно много машин — проверьте, что это полный файл наличия.")
        lines.append("\nПрименить?")
        await edit("\n".join(lines), reply_markup={"inline_keyboard": [[
            {"text": "✅ Применить", "callback_data": "sync:apply"}, {"text": "✖️ Отмена", "callback_data": "sync:cancel"}]]})
    except Exception:
        await report("сбой обновления наличия", traceback.format_exc(), chat_id)
        await edit("⚠️ Не получилось обработать файл — ошибку уже прислал. Таблица не тронута.")


async def on_callback(cq: dict):
    data = cq.get("data", "")
    chat_id = cq.get("message", {}).get("chat", {}).get("id")
    mid = cq.get("message", {}).get("message_id")
    await tg("answerCallbackQuery", callback_query_id=cq["id"])
    if data.startswith("want:"):
        await on_want(cq, data[5:])
        return
    if data == "menu":
        await send_welcome(chat_id)
        return
    if data.startswith("post:") and chat_id == ADMIN_CHAT_ID:
        await on_post_button(data, mid)
        return
    if not data.startswith("sync:") or chat_id != ADMIN_CHAT_ID:
        return
    job = SYNC_PENDING.pop(chat_id, None)
    if data == "sync:cancel" or not job:
        await tg("editMessageReplyMarkup", chat_id=chat_id, message_id=mid, reply_markup={"inline_keyboard": []})
        await send(chat_id, "Отменено, таблица не тронута." if job or data == "sync:cancel" else "Эта сводка устарела — пришлите файл ещё раз.")
        return
    await tg("editMessageReplyMarkup", chat_id=chat_id, message_id=mid, reply_markup={"inline_keyboard": []})
    try:
        res = await sheet_call({"action": "apply", "rows": job["rows"], "hide": job["hide"]})
    except Exception:
        await report("сбой записи в таблицу", traceback.format_exc(), chat_id)
        await send(chat_id, "⚠️ Не удалось записать в таблицу — ошибку прислал выше.")
        return
    text = (f"✅ Таблица обновлена: обновлено {res.get('updated', 0)}, добавлено {res.get('added', 0)}, "
            f"скрыто {res.get('hidden', 0)}" + (f", снова показано {res['unhidden']}" if res.get("unhidden") else "") + ".\n"
            "Каталог в приложении обновится в течение 5–10 минут.")
    if res.get("newModels"):
        text += ("\n\n❓ Новые модели без кузова: " + ", ".join(res["newModels"]) +
                 ". Впишите их во вкладку «Модели» (кузов и происхождение), иначе они не попадут в подборки.")
    await send(chat_id, text)
    event("sync")


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
                            caption=WELCOME, parse_mode="HTML", reply_markup=kb(chat_id))
            return
    except Exception:
        await report("не отправилось фото приветствия", traceback.format_exc(), chat_id)
    await send(chat_id, WELCOME, parse_mode="HTML", reply_markup=kb(chat_id))


async def send_contract(chat_id: int):
    await send_file("sendDocument", "document", chat_id, CONTRACT_FILE, CONTRACT_NAME, "application/pdf",
                    caption="Образец нашего агентского договора. Цифры и данные заполняются под вас.",
                    reply_markup=kb(chat_id))


async def on_menu(chat_id: int, text: str) -> bool:
    """Нажатие кнопки меню. True — если обработали."""
    if text == BTN_STEPS:
        await send(chat_id, STEPS, reply_markup=kb(chat_id))
        event("steps")
    elif text == BTN_CONTRACT:
        await send_contract(chat_id)
        event("contract")
    elif text == BTN_REVIEWS:
        await send(chat_id, REVIEWS, reply_markup=REVIEWS_BUTTONS)
        event("reviews")
    elif text == BTN_CONTACTS:
        await send(chat_id, CONTACTS, parse_mode="HTML", reply_markup=kb(chat_id))
        event("contacts")
    elif text == BTN_CHANNEL:
        await send(chat_id, "📢 Наш канал — подборки машин, кейсы клиентов, новости рынка и промокоды.",
                   reply_markup={"inline_keyboard": [[{"text": "Перейти в канал", "url": CHANNEL_URL}]]})
        event("channel")
    elif text == BTN_CHAT:
        await send(chat_id, "💬 Наш чат — задавайте вопросы, общайтесь с клиентами и командой.",
                   reply_markup={"inline_keyboard": [[{"text": "Перейти в чат", "url": CHAT_URL}]]})
        event("chat")
    elif text == BTN_APP:   # нажали кнопку как текст (старый клиент Telegram без мини-приложений)
        await send(chat_id, "Откройте каталог кнопкой «🚗 Каталог и подбор» внизу экрана. Если она не открывается — "
                            "обновите Telegram или напишите, какую машину ищете, и менеджер ответит здесь.",
                   reply_markup=kb(chat_id))
    else:
        return False
    return True


# ---------------------------------------------------------------- реферальные ссылки
# Ссылка вида https://t.me/<бот>?start=КОД — Telegram присылает боту "/start КОД".
# Код (латиница, цифры, _ и -) запоминается за клиентом навсегда: учитывается первая ссылка,
# по которой человек пришёл, и подставляется промокодом в заявки.

REF_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


async def save_ref(chat_id: int, user: dict, text: str):
    parts = text.split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""
    if not REF_RE.match(code) or code.startswith(("lot_", "post_")) or kv_get(f"promo:{chat_id}"):
        return
    kv_set(f"promo:{chat_id}", code)
    event(f"ref:{code}")
    if chat_id != ADMIN_CHAT_ID:
        await send(LEADS_CHAT_ID, f"🎟 Новый клиент по ссылке с промокодом <b>{html.escape(code)}</b>: "
                                  f"{user_link(chat_id, user)}", parse_mode="HTML")


def promo_line(chat_id: int, typed: str | None) -> str:
    ref = kv_get(f"promo:{chat_id}")
    typed = (typed or "").strip()
    if typed and ref and typed.upper() != ref.upper():
        return f"Промокод: {html.escape(typed)} (пришёл по ссылке {html.escape(ref)})"
    if typed:
        return f"Промокод: {html.escape(typed)}"
    if ref:
        return f"Промокод: {html.escape(ref)} (по ссылке)"
    return "Промокод: —"


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
        for k, label in [("car", "Марка/модель"), ("years", "Годы"), ("km", "Пробег"), ("drive", "Привод"), ("cond", "Состояние"),
                         ("colors", "Цвета"), ("budget", "Бюджет"), ("city", "Город доставки")]:
            if d.get(k):
                lines.append(f"{label}: {e(d[k])}")
    lines += ["", f"Имя: {e(d.get('name', ''))}", f"Телефон: {e(d.get('phone', ''))}",
              f"Связь: {e(CONTACT_LABEL.get(d.get('contact'), d.get('contact', '')))}"
              + (f" (@{e(user['username'])})" if d.get("contact") == "tg" and user.get("username") else ""),
              promo_line(chat_id, d.get("promo")),
              "", "↩️ Ответьте на это сообщение — ответ уйдёт клиенту в бот."]
    if chat_id == ADMIN_CHAT_ID:
        lines.insert(0, "🧪 ТЕСТ")
    sent = await send(LEADS_CHAT_ID, "\n".join(lines), parse_mode="HTML")
    kv_set(f"relay:{LEADS_CHAT_ID}:{sent['message_id']}", chat_id)
    how = {"tg": "здесь, в Telegram", "wa": "в WhatsApp", "call": "по телефону"}.get(d.get("contact"), "")
    await send(chat_id, f"Заявка №{lead_id} отправлена менеджеру — он свяжется с вами {how} в ближайшее время. "
                        "Если появятся вопросы, пишите сюда.", reply_markup=kb(chat_id))
    save_msg(chat_id, "system", f"заявка №{lead_id} ({kind})")


# ---------------------------------------------------------------- сообщения клиентов → менеджеру

async def forward_to_manager(msg: dict, user: dict):
    chat_id = msg["chat"]["id"]
    text = msg.get("text") or msg.get("caption") or ""
    save_msg(chat_id, "user", text or "[вложение]")
    if msgs_last_hour(chat_id) > FORWARDS_PER_HOUR:
        return   # флуд — молча не пересылаем
    ref = kv_get(f"promo:{chat_id}")
    head = await send(LEADS_CHAT_ID, f"💬 Сообщение в боте от {user_link(chat_id, user)}"
                                     + (f" · промокод {html.escape(ref)}" if ref else "")
                                     + (" — 🧪 ТЕСТ" if chat_id == ADMIN_CHAT_ID else "")
                                     + "\n↩️ Ответьте на сообщение ниже — ответ уйдёт клиенту.", parse_mode="HTML")
    kv_set(f"relay:{LEADS_CHAT_ID}:{head['message_id']}", chat_id)
    fwd = await tg("forwardMessage", chat_id=LEADS_CHAT_ID, from_chat_id=chat_id, message_id=msg["message_id"])
    kv_set(f"relay:{LEADS_CHAT_ID}:{fwd['message_id']}", chat_id)
    event("forward")
    # не отвечаем «передал» на каждое сообщение подряд — максимум раз в 10 минут
    if time.time() - float(kv_get(f"ack:{chat_id}", 0)) > 600:
        kv_set(f"ack:{chat_id}", time.time())
        await send(chat_id, FORWARDED_OK, reply_markup=kb(chat_id))


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
    doc = msg.get("document")
    if doc and chat_id == ADMIN_CHAT_ID and re.search(r"\.xlsx?$", doc.get("file_name", ""), re.I):
        asyncio.create_task(sync_start(chat_id, doc))
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
        arg = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
        event("start")
        if arg.startswith("lot_"):            # нажали машину под подборкой в канале
            event("post_lot")
            await show_car(chat_id, arg[4:])
            return
        if arg.startswith("post_"):
            event("post_open")
        await save_ref(chat_id, user, text)
        await send_welcome(chat_id)
        return
    if text.startswith("/post") and chat_id == ADMIN_CHAT_ID:
        await make_draft(chat_id)
        return
    if text.startswith("/emoji") and chat_id == ADMIN_CHAT_ID:
        await emoji_cmd(chat_id, text)
        return
    if (chat_id == ADMIN_CHAT_ID and text and not text.startswith("/") and kv_get("await_emoji") == "1"
            and text not in (BTN_APP, BTN_STEPS, BTN_CONTRACT, BTN_REVIEWS, BTN_CONTACTS, BTN_CHANNEL, BTN_CHAT)):
        await emoji_save(chat_id, msg)
        return
    if text.startswith("/autopost") and chat_id == ADMIN_CHAT_ID:
        await autopost_cmd(chat_id, text)
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


# ---------------------------------------------------------------- подборки в канал

MODE_TXT = {"auto": "сам публикует в 12:00 МСК", "check": "в 12:00 МСК присылает черновик вам на одобрение",
            "off": "выключен"}
_CAT: dict = {"ts": 0, "cars": []}


async def get_catalog(max_age: int = 300) -> list[dict]:
    if time.time() - _CAT["ts"] > max_age or not _CAT["cars"]:
        _CAT["cars"] = await posts.load_catalog(http)
        _CAT["ts"] = time.time()
    return _CAT["cars"]


def find_car(cars: list[dict], lot: str) -> dict | None:
    k = sync.norm(lot)
    return next((c for c in cars if sync.norm(c["lot"]) == k), None)


def posted_map() -> dict[str, float]:
    return {lot: ts for lot, ts in db.execute("SELECT lot, MAX(ts) FROM posted GROUP BY lot")}


async def build_post(start: int | None = None, skip: set[int] = frozenset()) -> dict | None:
    cars = await get_catalog(max_age=0)
    topics = posts.parse_topics((await sheet_call({"action": "topics"})).get("topics", []))
    start = start or int(kv_get("post_next", 1))
    t, sel = posts.next_topic(topics, cars, posted_map(), start, skip)
    if not t:
        return None
    imgs, ok = [], []
    for c in sel:   # главное фото (перёд 3/4); если не скачалось — следующее
        for u in c["ph"][:3]:
            try:
                imgs.append(await posts.fetch_image(http, u))
                ok.append(c)
                break
            except Exception as e:
                log.warning("фото %s: %s", u, e)
    if not ok:
        raise RuntimeError(f"не скачалось ни одного фото для темы №{t['n']}")
    logo = Image.open(HERE / "files" / "logo.png")
    photo = await asyncio.to_thread(posts.collage, imgs, logo)
    emj = post_emoji()
    return {"n": t["n"], "title": t["title"], "lots": [c["lot"] for c in ok], "photo": photo,
            "caption": posts.caption(t, ok, emj), "caption_plain": posts.caption(t, ok, emj, custom=False),
            "custom": any(e.get("id") for e in emj), "markup": posts.buttons(ok, BOT_USERNAME, t["n"])}


def post_emoji() -> list[dict]:
    raw = kv_get("post_emoji")
    if raw == "none":
        return posts.NO_EMOJI
    try:
        e = json.loads(raw) if raw else None
        return e if isinstance(e, list) and len(e) == len(posts.SLOTS) else posts.DEFAULT_EMOJI
    except Exception:
        return posts.DEFAULT_EMOJI


async def emoji_cmd(chat_id: int, text: str):
    arg = text.split(maxsplit=1)[1].strip().lower() if len(text.split()) > 1 else ""
    if arg in ("нет", "без", "off", "none"):
        kv_set("post_emoji", "none")
        await send(chat_id, "Ок, подборки будут без эмодзи: машины пронумерую «1. 2. 3.».")
        return
    if arg in ("сброс", "reset", "стандарт"):
        kv_set("post_emoji", "")
        await send(chat_id, "Вернул стандартные эмодзи: 1️⃣ 2️⃣ 3️⃣ 🎨 💰")
        return
    kv_set("await_emoji", "1")
    await send(chat_id, "Пришлите одним сообщением 5 эмодзи через пробел, по порядку:\n"
                        + "\n".join(f"{i + 1}) {s}" for i, s in enumerate(posts.SLOTS))
                        + "\n\nМожно премиум-эмодзи. Без эмодзи — /emoji нет, вернуть стандартные — /emoji сброс.")


async def emoji_save(chat_id: int, msg: dict) -> bool:
    kv_set("await_emoji", "")
    items = posts.parse_emoji_message(msg.get("text", ""), msg.get("entities", []))
    if len(items) != len(posts.SLOTS):
        await send(chat_id, f"Нужно ровно {len(posts.SLOTS)} эмодзи через пробел, а пришло {len(items)}. "
                            "Попробуйте ещё раз: /emoji")
        return True
    kv_set("post_emoji", json.dumps(items, ensure_ascii=False))
    prem = sum(1 for e in items if e.get("id"))
    lines = "\n".join(f"{posts.em(e)} — {s}" for e, s in zip(items, posts.SLOTS))
    await send(chat_id, f"Сохранил:\n{lines}" + (f"\n\nПремиум-эмодзи: {prem}. Если Telegram не пропустит их в канал, "
                                                  "бот опубликует с обычными и напишет вам." if prem else ""),
               parse_mode="HTML")
    return True


DRAFT_IMG = DATA_DIR / "draft.jpg"


def save_draft(p: dict):
    DRAFT_IMG.write_bytes(p["photo"])
    kv_set("post_draft", json.dumps({k: v for k, v in p.items() if k != "photo"}, ensure_ascii=False))


def load_draft() -> dict | None:
    raw = kv_get("post_draft")
    if not raw or not DRAFT_IMG.exists():
        return None
    return dict(json.loads(raw), photo=DRAFT_IMG.read_bytes())


DRAFT_BUTTONS = {"inline_keyboard": [[{"text": "✅ Опубликовать в канал", "callback_data": "post:pub"}],
                                     [{"text": "🔄 Другая тема", "callback_data": "post:next"},
                                      {"text": "⏭ Пропустить тему", "callback_data": "post:skip"}]]}


async def make_draft(chat_id: int, start: int | None = None, skip: set[int] = frozenset()):
    try:
        p = await build_post(start, skip)
    except Exception:
        await report("не собралась подборка", traceback.format_exc())
        return
    if not p:
        await send(chat_id, "Подборку собрать не из чего: ни под одну включённую тему сейчас нет машин «Готово».")
        return
    await send_photo_bytes(chat_id, p["photo"], caption=p["caption"], parse_mode="HTML", reply_markup=p["markup"])
    save_draft(p)
    await send(chat_id, f"👆 Черновик подборки — тема №{p['n']} «{p['title']}». Так пост будет выглядеть в канале. "
                        "Публикуем?", reply_markup=DRAFT_BUTTONS)


async def publish(p: dict) -> str:
    plain_note = ""
    try:
        res = await send_photo_bytes(CHANNEL_ID, p["photo"], caption=p["caption"], parse_mode="HTML",
                                     reply_markup=p["markup"])
        if p.get("custom") and not any(e.get("type") == "custom_emoji" for e in res.get("caption_entities", [])):
            plain_note = "Telegram показал в канале обычные эмодзи вместо премиум."
    except Exception as e:
        if not p.get("custom"):
            raise
        plain_note = f"Премиум-эмодзи в канал не пропустили ({str(e)[:120]}), опубликовал с обычными."
        res = await send_photo_bytes(CHANNEL_ID, p["photo"], caption=p.get("caption_plain", p["caption"]),
                                     parse_mode="HTML", reply_markup=p["markup"])
    if plain_note and kv_get("emoji_warned") != "1":
        kv_set("emoji_warned", "1")
        await send(ADMIN_CHAT_ID, "ℹ️ " + plain_note)
    now = time.time()
    db.executemany("INSERT INTO posted VALUES (?, ?, ?)", [(lot, p["n"], now) for lot in p["lots"]])
    db.commit()
    kv_set("post_next", p["n"] + 1)
    kv_set("post_draft", "")
    event("post_pub")
    chat = res.get("chat", {})
    return (f"https://t.me/{chat['username']}/{res['message_id']}" if chat.get("username") else "")


async def on_post_button(data: str, mid: int):
    await tg("editMessageReplyMarkup", chat_id=ADMIN_CHAT_ID, message_id=mid, reply_markup={"inline_keyboard": []})
    p = load_draft()
    if not p:
        await send(ADMIN_CHAT_ID, "Этот черновик уже неактуален. Новый — командой /post.")
        return
    if data == "post:pub":
        try:
            link = await publish(p)
        except Exception:
            await report("не опубликовалась подборка", traceback.format_exc())
            return
        await send(ADMIN_CHAT_ID, f"📢 Подборка «{p['title']}» опубликована. {link}")
    elif data == "post:next":
        await make_draft(ADMIN_CHAT_ID, start=p["n"] + 1, skip={p["n"]})
    elif data == "post:skip":
        kv_set("post_next", p["n"] + 1)
        kv_set("post_draft", "")
        await send(ADMIN_CHAT_ID, f"Тема «{p['title']}» пропущена. Завтра — следующая по списку.")


async def autopost_cmd(chat_id: int, text: str):
    arg = text.split(maxsplit=1)[1].strip().lower() if len(text.split()) > 1 else ""
    alias = {"auto": "auto", "авто": "auto", "check": "check", "проверка": "check", "off": "off", "выкл": "off"}
    if arg in alias:
        kv_set("post_mode", alias[arg])
    mode = kv_get("post_mode", "check")
    last = db.execute("SELECT topic, MAX(ts) FROM posted").fetchone()
    when = datetime.fromtimestamp(last[1], MSK).strftime("%d.%m %H:%M") if last and last[1] else "ещё не было"
    await send(chat_id, f"📢 Автопостинг: {MODE_TXT.get(mode, mode)}.\n"
                        f"Следующая тема: №{kv_get('post_next', 1)} (по порядку из вкладки «Темы»).\n"
                        f"Последняя подборка: {when}.\n\n"
                        "Команды:\n/autopost auto — публиковать самому без проверки\n"
                        "/autopost check — сначала присылать черновик мне\n/autopost off — выключить\n"
                        "/post — собрать черновик прямо сейчас\n/emoji — свои эмодзи для подборок")


async def autopost_loop():
    while True:
        now = datetime.now(MSK)
        today = now.strftime("%Y-%m-%d")
        # окно до 18:00 — если бот был выключен в 12:00, подборка выйдет, когда он поднимется, но не ночью
        if POST_HOUR <= now.hour < 18 and kv_get("post_day") != today:
            kv_set("post_day", today)
            mode = kv_get("post_mode", "check")
            try:
                if mode == "check":
                    await make_draft(ADMIN_CHAT_ID)
                elif mode == "auto":
                    p = await build_post()
                    if p:
                        link = await publish(p)
                        await send(ADMIN_CHAT_ID, f"📢 Вышла подборка «{p['title']}». {link}")
            except Exception:
                await report("сбой автопостинга", traceback.format_exc())
        await asyncio.sleep(60)


# ---------------------------------------------------------------- машина из подборки → клиенту

async def send_album(chat_id: int, urls: list[str], caption: str):
    urls = urls[:10]
    media = [dict({"type": "photo", "media": u + "?x-oss-process=image/resize,w_1280"},
                  **({"caption": caption, "parse_mode": "HTML"} if i == 0 else {})) for i, u in enumerate(urls)]
    try:
        await tg("sendMediaGroup", chat_id=chat_id, media=media)
        return
    except Exception as e:
        log.warning("альбом по ссылкам не ушёл: %s", e)
    files = {}
    for i, u in enumerate(urls):   # Telegram не смог скачать сам — качаем и загружаем
        try:
            r = await http.get(u + "?x-oss-process=image/resize,w_1280", follow_redirects=True)
            r.raise_for_status()
            files[f"p{i}"] = (f"p{i}.jpg", r.content, "image/jpeg")
        except Exception:
            pass
    if files:
        media = [dict({"type": "photo", "media": f"attach://{k}"},
                      **({"caption": caption, "parse_mode": "HTML"} if j == 0 else {})) for j, k in enumerate(files)]
        r = await http.post(f"{TG}/sendMediaGroup", data={"chat_id": str(chat_id), "media": json.dumps(media)},
                            files=files)
        if r.json().get("ok"):
            return
    await send(chat_id, caption, parse_mode="HTML")


async def show_car(chat_id: int, lot: str):
    try:
        c = find_car(await get_catalog(), lot)
    except Exception:
        await report("не загрузился каталог", traceback.format_exc(), chat_id)
        c = None
    if not c:
        await send(chat_id, "Эта машина уже продана или забронирована 😔 Посмотрите другие в каталоге — кнопка "
                            "«🚗 Каталог и подбор» внизу.", reply_markup=kb(chat_id))
        return
    await send_album(chat_id, c["ph"], posts.card(c))
    await send(chat_id, "Понравилась? Нажмите «Хочу эту машину» — менеджер напишет вам здесь, в Telegram, "
                        "ответит на вопросы и сделает точный расчёт.",
               reply_markup={"inline_keyboard": [[{"text": "✍️ Хочу эту машину", "callback_data": f"want:{c['lot']}"}],
                                                 [{"text": "🚗 Весь каталог и подбор", "callback_data": "menu"}]]})


async def on_want(cq: dict, lot: str):
    chat_id = cq["message"]["chat"]["id"]
    user = remember_user({"from": cq.get("from", {}), "chat": {"id": chat_id}})
    if time.time() - float(kv_get(f"want:{chat_id}:{lot}", 0) or 0) < 3600:
        await send(chat_id, "Заявка на эту машину уже у менеджера — он скоро напишет.", reply_markup=kb(chat_id))
        return
    kv_set(f"want:{chat_id}:{lot}", time.time())
    try:
        c = find_car(await get_catalog(), lot) or {"lot": lot}
    except Exception:
        c = {"lot": lot}
    cur = db.execute("INSERT INTO leads (chat_id, type, data, ts) VALUES (?, ?, ?, ?)",
                     (chat_id, "подборка", json.dumps({"lot": lot}, ensure_ascii=False), time.time()))
    db.commit()
    lead_id = cur.lastrowid
    event("want")
    e = html.escape
    lines = [f"🚗 Хочет машину из подборки в канале — №{lead_id}", f"Клиент в Telegram: {user_link(chat_id, user)}", ""]
    if c.get("brand"):
        lines.append(f"<b>{e(c['brand'])} {e(c['model'])}</b>")
    lines.append(f"Лот: {e(lot)}")
    for k, label in [("trim", "Комплектация"), ("made", "Выпуск"), ("km", "Пробег, км"), ("cond", "Состояние")]:
        if c.get(k):
            lines.append(f"{label}: {e(c[k])}")
    if c.get("priceN"):
        lines.append(f"Цена в канале: ≈ {posts.fmt(c['priceN'])} ₽")
    lines += ["", "Связь: Telegram" + (f" (@{e(user['username'])})" if user.get("username") else ""),
              promo_line(chat_id, None), "", "↩️ Ответьте на это сообщение — ответ уйдёт клиенту в бот."]
    if chat_id == ADMIN_CHAT_ID:
        lines.insert(0, "🧪 ТЕСТ")
    sent = await send(LEADS_CHAT_ID, "\n".join(lines), parse_mode="HTML")
    kv_set(f"relay:{LEADS_CHAT_ID}:{sent['message_id']}", chat_id)
    await send(chat_id, f"Заявка №{lead_id} отправлена менеджеру — он напишет вам здесь, в Telegram, в ближайшее время.",
               reply_markup=kb(chat_id))
    save_msg(chat_id, "system", f"заявка №{lead_id} (подборка, лот {lot})")


# ---------------------------------------------------------------- отчёты

def stats_text(hours: int) -> str:
    since = time.time() - hours * 3600
    cnt = lambda kind: db.execute("SELECT COUNT(*) FROM events WHERE kind=? AND ts>?", (kind, since)).fetchone()[0]
    return (f"📊 Бот за {hours} ч\n"
            f"Новых клиентов: {cnt('new_user')}, нажали /start: {cnt('start')}\n"
            f"Заявок из приложения: {cnt('lead_app')}\n"
            f"Сообщений переслано вам: {cnt('forward')}, ваших ответов клиентам: {cnt('relay')}\n"
            f"Открыли: этапы {cnt('steps')}, договор {cnt('contract')}, отзывы {cnt('reviews')}, "
            f"контакты {cnt('contacts')}, канал {cnt('channel')}, чат {cnt('chat')}\n"
            f"Из подборок в канале: открыли машин {cnt('post_lot')}, «Хочу эту машину» {cnt('want')}\n"
            f"Ошибок: {cnt('error')}"
            + refs_text(since))


def refs_text(since: float) -> str:
    rows = db.execute("SELECT substr(kind, 5), COUNT(*) FROM events WHERE kind LIKE 'ref:%' AND ts>? "
                      "GROUP BY kind ORDER BY 2 DESC", (since,)).fetchall()
    return ("\nПо реферальным ссылкам: " + ", ".join(f"{k} — {n}" for k, n in rows)) if rows else ""


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


_APP_VER = None


def app_version() -> str:
    global _APP_VER
    if _APP_VER is None:
        _APP_VER = version() if version() != "?" else str(int(time.time()))
    return _APP_VER


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
    global BOT_USERNAME
    me = await tg("getMe")
    BOT_USERNAME = me["username"]
    await tg("deleteWebhook", drop_pending_updates=False)
    await tg("setMyCommands", commands=[{"command": "start", "description": "Главное меню"}])
    sheet = ""
    try:   # проверка связи с таблицей (первый запрос заодно закрепляет ключ доступа)
        res = await sheet_call({"action": "ping"})
        sheet = f" Таблица на связи: {res.get('cars', '?')} машин."
    except Exception as e:
        sheet = f" ⚠️ Таблица не отвечает: {str(e)[:200]}"
    try:
        cm = await tg("getChatMember", chat_id=CHANNEL_ID, user_id=me["id"])
        ok = cm.get("status") == "creator" or (cm.get("status") == "administrator" and cm.get("can_post_messages"))
        sheet += " Канал: можно постить ✅" if ok else " ⚠️ Канал: бот не админ с правом публикации."
    except Exception as e:
        sheet += f" ⚠️ Канал недоступен: {str(e)[:150]}"
    sheet += f" Автопостинг: {MODE_TXT.get(kv_get('post_mode', 'check'), '?')}."
    await send(ADMIN_CHAT_ID, f"🤖 Бот @{me['username']} запущен, версия {version()}.{sheet}")
    asyncio.create_task(daily_report_loop())
    asyncio.create_task(autopost_loop())
    try:   # веб-сервер мини-приложения (за Caddy): каталог и фото с нашего сервера
        await web.Site(DATA_DIR, http).start(report)
    except Exception:
        await report("не запустился веб-сервер приложения", traceback.format_exc())
    offset = int(kv_get("offset", 0))
    fails = 0
    while True:
        try:
            updates = await tg("getUpdates", offset=offset, timeout=50, allowed_updates=["message", "callback_query"])
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
            if "callback_query" in u:
                try:
                    await on_callback(u["callback_query"])
                except Exception:
                    await report("сбой обработки кнопки", traceback.format_exc())
            if "message" in u:
                try:
                    await on_message(u["message"])
                except Exception:
                    await report("сбой при приёме сообщения", traceback.format_exc())


if __name__ == "__main__":
    asyncio.run(main())
