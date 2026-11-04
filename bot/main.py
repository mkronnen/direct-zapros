"""Клиентский бот ПрямойАвтоИмпорт: ИИ-ассистент на Groq + Telegram long polling.

Переменные окружения (лежат на сервере в /etc/direct-bot.env, в репозитории их нет):
  BOT_TOKEN, GROQ_API_KEY, ADMIN_CHAT_ID, DATA_DIR
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

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
# куда слать заявки (по умолчанию — Артёму; позже можно поставить id рабочей группы)
LEADS_CHAT_ID = int(os.environ.get("LEADS_CHAT_ID", ADMIN_CHAT_ID))
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
MODELS = [m.strip() for m in os.environ.get(
    "GROQ_MODELS", "openai/gpt-oss-120b,openai/gpt-oss-20b").split(",") if m.strip()]

HERE = Path(__file__).parent
TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
TZ = timezone(timedelta(hours=7))           # Красноярск
HISTORY_LIMIT = 30                          # сообщений истории в контексте
USER_MSGS_PER_HOUR = 40                     # защита лимитов Groq от флуда
DEBOUNCE_SEC = 2.0                          # склеиваем несколько сообщений подряд
DAILY_REPORT_HOUR = 9                       # суточный отчёт в 09:00 по Красноярску

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе в журнал попадает URL с токеном бота

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


def history(chat_id: int) -> list:
    rows = db.execute("SELECT role, content FROM messages WHERE chat_id=? ORDER BY ts DESC LIMIT ?",
                      (chat_id, HISTORY_LIMIT)).fetchall()
    return [{"role": r, "content": c} for r, c in reversed(rows)]


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
    for chunk in [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]:
        await tg("sendMessage", chat_id=chat_id, text=chunk, disable_web_page_preview=True, **extra)


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

# ---------------------------------------------------------------- нейросеть

SYSTEM_PROMPT = (HERE / "prompt.md").read_text(encoding="utf-8").replace(
    "{knowledge}", (HERE / "knowledge.md").read_text(encoding="utf-8"))

TOOLS = [{
    "type": "function",
    "function": {
        "name": "send_lead",
        "description": "Передать заявку или вопрос клиента менеджеру. type='подбор'/'конкретная машина' — когда "
                       "известны модель (или класс) И бюджет. type='вопрос менеджеру' — только если клиент сам просит "
                       "живого человека, хочет оплатить, торговаться или спрашивает про уже идущую сделку. "
                       "Вопрос «сколько стоит» — НЕ повод вызывать: сначала узнай год и бюджет.",
        "parameters": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["подбор", "конкретная машина", "вопрос менеджеру"]},
                "summary": {"type": "string", "description": "Кратко суть запроса для менеджера, 1–3 предложения"},
                "name": {"type": "string", "description": "Как зовут клиента, если известно"},
                "car": {"type": "string", "description": "Марка/модель/комплектация или класс авто"},
                "year_mileage": {"type": "string", "description": "Год и пробег, если обсуждали"},
                "budget": {"type": "string", "description": "Бюджет, если назван"},
                "city": {"type": "string", "description": "Город получения"},
                "contact": {"type": "string", "description": "Телефон или другой контакт, если клиент дал"},
            },
            "required": ["type", "summary"],
        },
    },
}]


# Готовые ответы Артёма: bot/answers/<имя>.txt. Первая строка — «# когда отправлять», дальше — текст.
# Текст уходит клиенту дословно, нейросеть его не переписывает.
ANSWERS = {}
for f in sorted((HERE / "answers").glob("*.txt")) if (HERE / "answers").exists() else []:
    lines = f.read_text(encoding="utf-8").strip().splitlines()
    if lines and lines[0].startswith("#") and len(lines) > 1:
        ANSWERS[f.stem] = {"when": lines[0].lstrip("# ").strip(), "text": "\n".join(lines[1:]).strip()}

if ANSWERS:
    TOOLS.append({
        "type": "function",
        "function": {
            "name": "send_prepared_answer",
            "description": "Отправить клиенту готовый ответ Артёма дословно. ВСЕГДА используй его, если вопрос "
                           "клиента подходит под тему, вместо того чтобы отвечать своими словами. Темы: " +
                           "; ".join(f"«{k}» — {v['when']}" for k, v in ANSWERS.items()),
            "parameters": {"type": "object",
                           "properties": {"topic": {"type": "string", "enum": list(ANSWERS)}},
                           "required": ["topic"]},
        },
    })


CONTRACT_FILE = HERE / "files" / "dogovor_obrazec.pdf"
CONTRACT_NAME = "Образец договора DirectAI (Китай).pdf"
if CONTRACT_FILE.exists():
    TOOLS.append({
        "type": "function",
        "function": {
            "name": "send_contract_sample",
            "description": "Отправить клиенту файл с образцом агентского договора (Китай). Используй, когда клиент "
                           "просит договор, хочет посмотреть условия, сроки или гарантии по договору.",
            "parameters": {"type": "object", "properties": {}},
        },
    })


async def send_contract(chat_id: int):
    r = await http.post(f"{TG}/sendDocument",
                        data={"chat_id": str(chat_id),
                              "caption": "Образец нашего агентского договора. Цифры и данные заполняются под вас."},
                        files={"document": (CONTRACT_NAME, CONTRACT_FILE.read_bytes(), "application/pdf")})
    if not r.json().get("ok"):
        raise RuntimeError(f"sendDocument: {r.text[:200]}")


class LLMError(Exception):
    pass


async def llm(messages: list) -> dict:
    """Запрос к Groq. При лимите/сбое пробуем следующую модель из списка."""
    errors = []
    for model in MODELS:
        body = {"model": model, "messages": messages, "tools": TOOLS, "tool_choice": "auto",
                "temperature": 0.5, "max_completion_tokens": 2000}
        if "gpt-oss" in model:
            body["reasoning_effort"] = os.environ.get("REASONING_EFFORT", "medium")
        for attempt in range(3):
            try:
                r = await http.post(GROQ_URL, json=body, timeout=60,
                                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"})
            except httpx.HTTPError as e:
                errors.append(f"{model}: сеть: {e!r}")
                await asyncio.sleep(2)
                continue
            if r.status_code == 200:
                event("llm_ok")
                return r.json()["choices"][0]["message"]
            errors.append(f"{model}: HTTP {r.status_code}: {r.text[:250]}")
            if r.status_code == 400 and "reasoning_effort" in body:
                body.pop("reasoning_effort")
                continue
            if r.status_code == 429:
                event("rate_limit")
                try:
                    wait = float(r.headers.get("retry-after", "5"))
                except ValueError:
                    wait = 5
                if wait <= 25 and attempt < 2:
                    await asyncio.sleep(wait + 0.5)
                    continue
                break  # долго ждать — пробуем следующую модель
            if r.status_code >= 500 and attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            break  # следующая модель
    raise LLMError("\n".join(errors[-4:]))


MD_RE = re.compile(r"(\*\*|__|^#+\s*|`)", re.M)


def clean(text: str) -> str:
    return MD_RE.sub("", text or "").strip()


# ---------------------------------------------------------------- заявки

HUMAN_RE = re.compile(r"менеджер|человек|оператор|живо|позвон|созвон|связат|свяжит|оплат|предоплат|реквизит|"
                      r"договор|торг|скидк|сделк|отправьте|передайте", re.I)


async def do_send_lead(chat_id: int, user: dict, args: dict, last_user_text: str = "") -> str:
    if args.get("type") == "вопрос менеджеру" and not HUMAN_RE.search(last_user_text):
        return ("не отправлено: клиент не просил менеджера. Ответь сам; если это вопрос о цене — "
                "объясни, от чего она зависит, и спроси год и бюджет")
    if args.get("type") in ("подбор", "конкретная машина") and not (args.get("car") and args.get("budget")):
        return "не отправлено: для заявки на подбор нужны модель и бюджет — сначала спроси у клиента недостающее"
    cur = db.execute("INSERT INTO leads (chat_id, type, data, ts) VALUES (?, ?, ?, ?)",
                     (chat_id, args.get("type", ""), json.dumps(args, ensure_ascii=False), time.time()))
    db.commit()
    lead_id = cur.lastrowid
    event("lead")
    e = html.escape
    who = e(user.get("name") or "клиент")
    link = f'<a href="tg://user?id={chat_id}">{who}</a>'
    if user.get("username"):
        link += f" (@{e(user['username'])})"
    lines = [f"🆕 Заявка №{lead_id} из бота — {e(args.get('type', ''))}", f"Клиент: {link}"]
    for key, label in [("name", "Имя"), ("car", "Авто"), ("year_mileage", "Год/пробег"), ("budget", "Бюджет"),
                       ("city", "Город"), ("contact", "Контакт")]:
        if args.get(key):
            lines.append(f"{label}: {e(str(args[key]))}")
    lines.append(f"\n{e(args.get('summary', ''))}")
    if chat_id == ADMIN_CHAT_ID:
        lines.insert(0, "🧪 ТЕСТ")
    await send(LEADS_CHAT_ID, "\n".join(lines), parse_mode="HTML")
    return f"ok: заявка №{lead_id} передана менеджеру"


# ---------------------------------------------------------------- диалог

GREETING = ("Здравствуйте! Я ИИ-ассистент ПрямойАвтоИмпорт — помогаю подобрать и привезти автомобиль "
            "из Китая, Кореи или Японии.\n\nНапишите, какую машину ищете или что хотите узнать — "
            "подскажу и при необходимости передам менеджеру.")

buffers: dict[int, list[str]] = {}
timers: dict[int, asyncio.Task] = {}
locks: dict[int, asyncio.Lock] = {}
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


async def reply_with_llm(chat_id: int, user_text: str):
    save_msg(chat_id, "user", user_text)
    if msgs_last_hour(chat_id) > USER_MSGS_PER_HOUR:
        await send(chat_id, "Давайте сделаем небольшую паузу — я передам ваш вопрос менеджеру, он ответит лично.")
        await report("клиент превысил лимит сообщений", chat_id=chat_id)
        return
    await tg("sendChatAction", chat_id=chat_id, action="typing")
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history(chat_id)
    user = users.get(chat_id, {})
    sent_template = False
    for _ in range(3):  # максимум 3 круга вызова инструментов
        msg = await llm(messages)
        calls = msg.get("tool_calls") or []
        if not calls:
            text = clean(msg.get("content"))
            if not text:
                if sent_template:   # готовый ответ уже ушёл — добавлять нечего
                    return
                raise LLMError("пустой ответ модели")
            save_msg(chat_id, "assistant", text)
            await send(chat_id, text)
            return
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
                if name == "send_lead":
                    result = await do_send_lead(chat_id, user, args, user_text)
                    if result.startswith("ok"):
                        save_msg(chat_id, "system", f"Служебно: {result.split(': ', 1)[1]}. Повторно не отправляй.")
                elif name == "send_prepared_answer" and args.get("topic") in ANSWERS and not sent_template:
                    text = ANSWERS[args["topic"]]["text"]
                    await send(chat_id, text)
                    save_msg(chat_id, "assistant", text)
                    event("template")
                    sent_template = True
                    result = ("ok: готовый ответ отправлен клиенту. Не пересказывай его. Если клиент спрашивал "
                              "ещё что-то — ответь коротко; иначе можешь задать один короткий вопрос или "
                              "вернуть пустой ответ.")
                elif name == "send_contract_sample" and CONTRACT_FILE.exists():
                    await send_contract(chat_id)
                    save_msg(chat_id, "system", "Служебно: образец договора уже отправлен клиенту.")
                    event("contract")
                    result = ("ok: файл договора отправлен. Если клиент спрашивал о конкретном условии (сроки и т.п.) "
                              "— коротко процитируй его из базы знаний.")
                else:
                    result = f"нет инструмента {name}"
            except Exception as e:
                result = f"ошибка: {e}"
                await report(f"сбой инструмента {name}", traceback.format_exc(), chat_id)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        if all(c["function"]["name"] in ("send_prepared_answer", "send_contract_sample") for c in calls):
            names = {c["function"]["name"] for c in calls}
            # клиент просил договор, а модель про него забыла — отправляем сами
            if ("send_contract_sample" not in names and CONTRACT_FILE.exists()
                    and re.search(r"договор", user_text, re.I)):
                await send_contract(chat_id)
                save_msg(chat_id, "system", "Служебно: образец договора уже отправлен клиенту.")
                event("contract")
            return  # готовые материалы отправлены — лишний запрос к нейросети не тратим
    if sent_template:
        return
    raise LLMError("слишком много вызовов инструментов подряд")


async def process(chat_id: int):
    await asyncio.sleep(DEBOUNCE_SEC)
    async with locks.setdefault(chat_id, asyncio.Lock()):
        parts = buffers.pop(chat_id, [])
        if not parts:
            return
        sent_before = db.execute("SELECT COUNT(*) FROM messages WHERE chat_id=? AND role='assistant'",
                                 (chat_id,)).fetchone()[0]
        try:
            await reply_with_llm(chat_id, "\n".join(parts))
        except LLMError as e:
            sent_now = db.execute("SELECT COUNT(*) FROM messages WHERE chat_id=? AND role='assistant'",
                                  (chat_id,)).fetchone()[0]
            if sent_now > sent_before:   # клиенту уже что-то ушло в этом ходе — не пугаем его сбоем
                await report("нейросеть не ответила (после отправки материалов)", str(e), chat_id)
                return
            await report("нейросеть не ответила", str(e), chat_id)
            await safe_send(chat_id, "Секунду, у меня технический сбой. Я уже передал ваш вопрос менеджеру — "
                                     "он ответит в ближайшее время.")
            await notify_fallback(chat_id, "\n".join(parts))
        except Exception:
            await report("сбой обработки сообщения", traceback.format_exc(), chat_id)
            await safe_send(chat_id, "Извините, что-то пошло не так. Передаю ваш вопрос менеджеру.")
            await notify_fallback(chat_id, "\n".join(parts))


async def safe_send(chat_id, text):
    try:
        await send(chat_id, text)
    except Exception as e:
        log.error("safe_send: %s", e)


async def notify_fallback(chat_id: int, text: str):
    u = users.get(chat_id, {})
    who = html.escape(u.get("name") or "клиент")
    link = f'<a href="tg://user?id={chat_id}">{who}</a>' + (f" (@{html.escape(u['username'])})" if u.get("username") else "")
    try:
        await send(LEADS_CHAT_ID, f"📩 Бот не смог ответить, ответьте вручную.\nКлиент: {link}\n\n{html.escape(text)}",
                   parse_mode="HTML")
    except Exception as e:
        log.error("fallback notify: %s", e)


async def on_message(msg: dict):
    chat = msg.get("chat", {})
    if chat.get("type") != "private":
        return
    chat_id = chat["id"]
    remember_user(msg)
    text = msg.get("text") or msg.get("caption") or ""
    if text.startswith("/start"):
        db.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
        db.commit()
        await send(chat_id, GREETING)
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
    if not text:
        await send(chat_id, "Пока я понимаю только текст. Опишите, пожалуйста, словами — или я передам менеджеру.")
        return
    buffers.setdefault(chat_id, []).append(text)
    t = timers.get(chat_id)
    if t and not t.done():
        t.cancel()
    timers[chat_id] = asyncio.create_task(process(chat_id))


# ---------------------------------------------------------------- отчёты

def stats_text(hours: int) -> str:
    since = time.time() - hours * 3600
    cnt = lambda kind: db.execute("SELECT COUNT(*) FROM events WHERE kind=? AND ts>?", (kind, since)).fetchone()[0]
    dialogs = db.execute("SELECT COUNT(DISTINCT chat_id) FROM messages WHERE role='user' AND ts>?",
                         (since,)).fetchone()[0]
    msgs = db.execute("SELECT COUNT(*) FROM messages WHERE role='user' AND ts>?", (since,)).fetchone()[0]
    return (f"📊 Бот за {hours} ч\n"
            f"Новых клиентов: {cnt('new_user')}\n"
            f"Диалогов: {dialogs}, сообщений от клиентов: {msgs}\n"
            f"Заявок менеджеру: {cnt('lead')}\n"
            f"Ответов нейросети: {cnt('llm_ok')}, упёрлись в лимит Groq: {cnt('rate_limit')}\n"
            f"Готовых ответов: {cnt('template')}, договоров отправлено: {cnt('contract')}\n"
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
