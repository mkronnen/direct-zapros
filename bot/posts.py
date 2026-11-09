"""Автопостинг подборок в канал.

Каждый день в 12:00 МСК бот берёт следующую тему из вкладки «Темы» (по порядку №), подбирает под неё
до трёх машин из каталога (только «Готово» по тройному тесту и с оригинальным окрасом, разные модели,
не постившиеся последние 30 дней),
собирает коллаж из главных фото и публикует пост с кнопками на каждую машину.
Если под тему сейчас нет ни одной машины — тема пропускается, берётся следующая.
"""
import csv
import io
import re
import time

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps

CSV_URL = ("https://docs.google.com/spreadsheets/d/e/2PACX-1vQwOaGZlOwyIaH7SphCWmWXkNC1decOw_dpAZcrWMlW8Qqn1L05CPuX2FSN7KvX9rej2Sc7W9Lb1mZi"
           "/pub?gid=2075215146&single=true&output=csv")
IMG_HOST = "https://heyuncar.bebestweb.com/"
META = {"cny": "", "date": ""}   # курс юаня ЦБ и его дата — из первой строки каталога
MAX_CARS = 3
REPEAT_DAYS = 30          # машину не повторяем в подборках месяц…
REPEAT_DAYS_MIN = 10      # …но если иначе тема пустая — можно через 10 дней
# Эмодзи поста по слотам: номер 1, 2, 3, окрас, цена. Артём может заменить их своими (в т.ч. премиум) командой /emoji.
SLOTS = ["номер 1", "номер 2", "номер 3", "окрас", "цена"]
DEFAULT_EMOJI = [{"t": "1️⃣"}, {"t": "2️⃣"}, {"t": "3️⃣"}, {"t": "🎨"}, {"t": "💰"}]
NO_EMOJI = [{"t": "1."}, {"t": "2."}, {"t": "3."}, {"t": ""}, {"t": ""}]
BG = (11, 20, 22)
ACCENT = (0, 160, 230)


def em(e: dict, custom: bool = True) -> str:
    """Эмодзи для HTML-подписи: премиум — тегом tg-emoji (с обычным эмодзи внутри на случай, если не пропустят)."""
    if custom and e.get("id"):
        return f'<tg-emoji emoji-id="{e["id"]}">{e["t"]}</tg-emoji>'
    return e["t"]


def parse_emoji_message(text: str, entities: list[dict]) -> list[dict]:
    """Эмодзи из сообщения Артёма. Премиум-эмодзи приходят как entity custom_emoji (смещения в UTF-16),
    их можно слать подряд без пробелов; обычные — через пробел."""
    u16 = text.encode("utf-16-le")
    sub = lambda a, b: u16[a * 2:b * 2].decode("utf-16-le")
    ents = sorted((e for e in entities or [] if e.get("type") == "custom_emoji"), key=lambda e: e["offset"])
    out, pos = [], 0
    for e in ents + [{"offset": len(u16) // 2, "length": 0}]:
        out += [{"t": tok} for tok in sub(pos, e["offset"]).split()]
        if e.get("custom_emoji_id"):
            out.append({"t": sub(e["offset"], e["offset"] + e["length"]), "id": e["custom_emoji_id"]})
        pos = e["offset"] + e["length"]
    return out


# ---------------------------------------------------------------- данные

def num(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[\s  ]", "", str(v or "")).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def lst(v) -> list[str]:
    return [x.strip() for x in str(v or "").split(",") if x.strip()]


def photo_list(s: str) -> list[str]:
    s = (s or "").strip()
    if not s:
        return []
    if ":" in s:
        pre, ids = s.split(":", 1)
        paths = [pre + "/" + i.strip() for i in ids.split(",") if i.strip()]
    else:
        paths = [p.strip() for p in s.split(",") if p.strip()]
    return [IMG_HOST + p + ".jpg" for p in paths]


async def load_catalog(http: httpx.AsyncClient) -> list[dict]:
    r = await http.get(CSV_URL + "&t=" + str(int(time.time())), follow_redirects=True,
                       timeout=httpx.Timeout(40.0, connect=15.0))
    r.raise_for_status()
    r.encoding = "utf-8"
    rows = list(csv.DictReader(io.StringIO(r.text)))
    if rows:
        META["cny"] = (rows[0].get("cny") or "").strip()
        META["date"] = (rows[0].get("date") or "").strip()
    cars = []
    for o in rows:
        o = {k.strip(): (v or "").strip() for k, v in o.items() if k}
        if not o.get("lot"):
            continue
        made = o.get("made", "")
        m = re.match(r"(\d{1,2})\.(\d{4})", made)
        o["year"] = int(m.group(2)) if m else 0
        o["ym"] = (int(m.group(2)), int(m.group(1))) if m else (0, 0)
        o["priceN"], o["kmN"], o["hpN"] = num(o.get("price")), num(o.get("km")), num(o.get("hp"))
        o["ph"] = photo_list(o.get("photos", ""))
        cars.append(o)
    return cars


def parse_topics(raw: list[list]) -> list[dict]:
    out = []
    for r in raw:
        r = list(r) + [""] * (16 - len(r))
        n = int(num(r[0]))
        if not n or not str(r[2]).strip():
            continue
        out.append(dict(n=n, on=str(r[1]).strip().lower() in ("да", "yes", "1", "true"), title=str(r[2]).strip(),
                        intro=str(r[3]).strip(), body=lst(r[4]), origin=lst(r[5]), brands=lst(r[6]), models=lst(r[7]),
                        pmin=num(r[8]), pmax=num(r[9]), ymin=num(r[10]), kmax=num(r[11]), type=str(r[12]).strip(),
                        drive=str(r[13]).strip(), clean=str(r[14]).strip().lower() == "да", sort=str(r[15]).strip().lower()))
    return sorted(out, key=lambda t: t["n"])


# ---------------------------------------------------------------- подбор машин

def matches(c: dict, t: dict) -> bool:
    low = lambda xs: [x.lower() for x in xs]
    if c.get("test") != "Готово" or not c["priceN"] or not c["ph"]:
        return False
    if c.get("cond", "").strip() != "Оригинальный окрас":   # в подборки — только машины без окрасов
        return False
    if t["body"] and c.get("body", "").lower() not in low(t["body"]):
        return False
    if t["origin"] and c.get("origin", "").lower() not in low(t["origin"]):
        return False
    if t["brands"] and c.get("brand", "").lower() not in low(t["brands"]):
        return False
    if t["models"] and c.get("model", "").lower() not in low(t["models"]):
        return False
    if t["pmin"] and c["priceN"] < t["pmin"]:
        return False
    if t["pmax"] and c["priceN"] > t["pmax"]:
        return False
    if t["ymin"] and c["year"] < t["ymin"]:
        return False
    if t["kmax"] and c["kmN"] > t["kmax"]:
        return False
    if t["type"] and c.get("type", "").lower() != t["type"].lower():
        return False
    if t["drive"] and c.get("drive", "").lower() != t["drive"].lower():
        return False
    if t["clean"] and c.get("cond", "") != "Оригинальный окрас":
        return False
    return True


def sort_key(t: dict):
    s = t["sort"]
    if "дорож" in s:
        return lambda c: (-c["priceN"], c["kmN"])
    if "свеж" in s:
        return lambda c: (-c["ym"][0], -c["ym"][1], c["priceN"])
    if "пробег" in s:
        return lambda c: (c["kmN"], c["priceN"])
    return lambda c: (c["priceN"], c["kmN"])   # «дешевле» и «разброс по цене»


def pick(cars: list[dict], t: dict, posted: dict[str, float], now: float | None = None) -> list[dict]:
    """posted: лот → когда последний раз был в подборке."""
    now = now or time.time()
    cand = sorted((c for c in cars if matches(c, t)), key=sort_key(t))
    for days in (REPEAT_DAYS, REPEAT_DAYS_MIN):
        pool = [c for c in cand if now - posted.get(c["lot"], 0) > days * 86400]
        uniq, seen = [], set()   # разные модели
        for c in pool:
            k = (c["brand"].lower(), c["model"].lower())
            if k not in seen:
                seen.add(k)
                uniq.append(c)
        if not uniq:
            continue
        if "разброс" in t["sort"] and len(uniq) > MAX_CARS:
            # по всему ценовому диапазону темы: самая доступная, середина, самая дорогая
            idx = sorted({round(i * (len(uniq) - 1) / (MAX_CARS - 1)) for i in range(MAX_CARS)})
            return [uniq[i] for i in idx]
        return uniq[:MAX_CARS]
    return []


def next_topic(topics: list[dict], cars: list[dict], posted: dict, start: int, skip: set[int] = frozenset()):
    """Первая включённая тема начиная с № start (по кругу), под которую есть хоть одна машина."""
    on = [t for t in topics if t["on"] and t["n"] not in skip]
    if not on:
        return None, []
    order = [t for t in on if t["n"] >= start] + [t for t in on if t["n"] < start]
    for t in order:
        sel = pick(cars, t, posted)
        if sel:
            return t, sel
    return None, []


# ---------------------------------------------------------------- текст поста

def fmt(n: float) -> str:
    return f"{int(round(n)):,}".replace(",", " ")


def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def short_cond(cond: str) -> str:
    if cond == "Оригинальный окрас":
        return "родная краска"
    return cond[:1].lower() + cond[1:] if cond else ""


def car_lines(i: int, c: dict, with_cond: bool = True, emoji: list[dict] = DEFAULT_EMOJI, custom: bool = True) -> str:
    trim = c.get("trim", "")
    if len(trim) > 45:
        trim = trim[:44].rstrip() + "…"
    specs = [c.get("made", ""), fmt(c["kmN"]) + " км"]
    eng = "электро" if c.get("type") == "Электро" else c.get("eng", "")
    if eng:
        specs.append(eng + (f" {int(c['hpN'])} л.с." if c["hpN"] else ""))
    if c.get("gb"):
        specs.append(c["gb"])
    if "полн" in c.get("drive", "").lower():
        specs.append("полный привод")
    pre = lambda k: (em(emoji[k], custom) + " ") if emoji[k]["t"] else ""
    s = f"{pre(i)}<b>{esc(c['brand'])} {esc(c['model'])}</b> {esc(trim)}\n{esc(' · '.join(x for x in specs if x))}\n"
    if with_cond and c.get("cond"):
        s += f"{pre(3)}{esc(short_cond(c['cond']))}\n"
    s += f"{pre(4)}<b>≈ {fmt(c['priceN'])} ₽</b>\n"
    return s


def footer() -> str:
    rate = ""
    if META["date"]:
        cny = num(META["cny"])
        rate = f" на {META['date'][:5]}" + (f" ({cny:.2f} ₽ за юань)".replace(".", ",") if cny else "")
    return (f"<i>Цена — ориентировочно до Уссурийска, с растаможкой, по курсу ЦБ{esc(rate)}. "
            "Нажмите на машину под постом — бот покажет все фото и примет заявку.</i>")


def visible_len(html_text: str) -> int:
    import html as h
    return len(h.unescape(re.sub(r"<[^>]+>", "", html_text)).encode("utf-16-le")) // 2


def caption(t: dict, cars: list[dict], emoji: list[dict] = DEFAULT_EMOJI, custom: bool = True) -> str:
    for with_cond in (True, False):
        txt = f"<b>{esc(t['title'])}</b>\n"
        if t["intro"]:
            txt += esc(t["intro"]) + "\n"
        txt += "\n" + "\n".join(car_lines(i, c, with_cond, emoji, custom) for i, c in enumerate(cars)) + "\n" + footer()
        if visible_len(txt) <= 1024:   # лимит подписи к фото в Telegram
            return txt
    return txt


def buttons(cars: list[dict], bot_username: str, topic_n: int) -> dict:
    base = f"https://t.me/{bot_username}?start="
    rows = [[{"text": f"{i + 1}. {c['brand']} {c['model']} — подробнее", "url": f"{base}lot_{c['lot']}"}]
            for i, c in enumerate(cars)]
    rows.append([{"text": "🚗 Весь каталог и подбор", "url": f"{base}post_{topic_n}"}])
    return {"inline_keyboard": rows}


# ---------------------------------------------------------------- коллаж

def _cover(im: Image.Image, w: int, h: int) -> Image.Image:
    return ImageOps.fit(im.convert("RGB"), (w, h), Image.LANCZOS, centering=(0.5, 0.55))


def _badge(canvas: Image.Image, x: int, y: int, n: int, size: int):
    d = ImageDraw.Draw(canvas)
    d.ellipse((x, y, x + size, y + size), fill=ACCENT, outline=(255, 255, 255), width=max(2, size // 18))
    try:
        font = ImageFont.load_default(size=int(size * 0.58))
    except TypeError:
        font = ImageFont.load_default()
    d.text((x + size / 2, y + size / 2 + 1), str(n), fill=(255, 255, 255), font=font, anchor="mm")


def collage(images: list[Image.Image], logo: Image.Image | None = None) -> bytes:
    g = 8
    if len(images) == 1:
        W, H = 1280, 960
        boxes = [(0, 0, W, H)]
    elif len(images) == 2:
        W, H = 1280, 480
        w = (W - g) // 2
        boxes = [(0, 0, w, H), (w + g, 0, W - w - g, H)]
    else:
        W, H = 1280, 640
        bw = 852
        sw, sh = W - bw - g, (H - g) // 2
        boxes = [(0, 0, bw, H), (bw + g, 0, sw, sh), (bw + g, sh + g, sw, H - sh - g)]
    canvas = Image.new("RGB", (W, H), BG)
    for k, (im, (x, y, w, h)) in enumerate(zip(images, boxes)):
        canvas.paste(_cover(im, w, h), (x, y))
        if len(images) > 1:
            bs = 64 if k == 0 or len(images) == 2 else 52
            _badge(canvas, x + 16, y + 16, k + 1, bs)
    if logo is not None:
        lw = 220   # логотип — в левый нижний угол первого фото
        lg = logo.convert("RGBA").resize((lw, round(logo.height * lw / logo.width)), Image.LANCZOS)
        pad = 12
        plate = Image.new("RGBA", (lg.width + 2 * pad, lg.height + 2 * pad), (11, 20, 22, 185))
        plate.alpha_composite(lg, (pad, pad))
        canvas.paste(plate, (16, H - plate.height - 16), plate)
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=88)
    return out.getvalue()


async def fetch_image(http: httpx.AsyncClient, url: str, w: int = 1000) -> Image.Image:
    r = await http.get(f"{url}?x-oss-process=image/resize,w_{w}", timeout=httpx.Timeout(30.0, connect=10.0),
                       follow_redirects=True)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content))


def card(c: dict) -> str:
    """Подпись к альбому машины, когда клиент нажал её под подборкой."""
    specs = [c.get("made", ""), fmt(c["kmN"]) + " км"]
    eng = "электро" if c.get("type") == "Электро" else c.get("eng", "")
    if eng:
        specs.append(eng + (f" {int(c['hpN'])} л.с." if c["hpN"] else ""))
    for k in ("gb", "drive"):
        if c.get(k):
            specs.append(c[k] + (" привод" if k == "drive" else ""))
    s = f"<b>{esc(c['brand'])} {esc(c['model'])}</b>\n{esc(c.get('trim', ''))}\n{esc(' · '.join(x for x in specs if x))}\n"
    if c.get("color"):
        s += f"Цвет: {esc(c['color'])}\n"
    if c.get("cond"):
        s += f"Состояние: {esc(c['cond'])}\n"
    if c.get("from"):
        s += f"Растаможка возможна с {esc(c['from'])}\n"
    s += f"Лот {esc(c['lot'])}\n\n"
    s += (f"💰 <b>≈ {fmt(c['priceN'])} ₽</b>\n<i>Ориентировочно до Уссурийска, с растаможкой, по курсу ЦБ. "
          "Точный расчёт — у менеджера.</i>") if c["priceN"] else "💰 Цена по запросу"
    return s[:1024]
