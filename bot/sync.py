"""Обновление таблицы «Наличие» из Excel-файла китайцев.

Артём присылает боту .xls/.xlsx → бот разбирает файл, по новым машинам тянет с сайта avtomirhrb.ru
комплектацию, двигатель, л.с. и фото → показывает сводку → после «Применить» записывает в Google-таблицу
через Apps Script (веб-приложение, привязанное к таблице).

Что меняется в таблице:
  • машины, которые уже есть: цена, пробег, состояние, тройной тест (фото и комплектацию не трогаем — они выверены);
  • новые машины: добавляются строкой целиком, фото по стандартной схеме съёмки дилера;
  • машин нет в новом файле: скрываются (Показывать = «нет»), строки не удаляются.
"""
import hashlib
import html as htmlmod
import io
import re

import httpx

APPS_URL = "https://script.google.com/macros/s/AKfycbxChw0brlhEpt29yY1_Hk5Vg9YWtziUarbqkE_lcVa8yebI3wqHSGlgXew9GtFq_FQ5/exec"
SITE_TIMEOUT = httpx.Timeout(25.0, connect=10.0)

MON = {'январ': 1, 'феврал': 2, 'март': 3, 'апрел': 4, 'май': 5, 'мая': 5, 'июн': 6, 'июл': 7, 'август': 8,
       'сентябр': 9, 'октябр': 10, 'ноябр': 11, 'декабр': 12}


# ---------------------------------------------------------------- Excel

def read_rows(data: bytes, filename: str) -> list[list]:
    if filename.lower().endswith(".xls"):
        import xlrd
        book = xlrd.open_workbook(file_contents=data)
        sh = book.sheet_by_index(0)
        return [sh.row_values(i) for i in range(sh.nrows)]
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    return [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]


def cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def brand_model(s: str) -> tuple[str, str]:
    s = s.strip().replace('А', 'A')
    for k, v in {'AUDI': 'Audi', 'ŠKODA': 'Škoda', 'SKODA': 'Škoda', 'BMW mini': 'MINI', 'Lixiang': 'Li Auto',
                 'Volkswagen T-ROC': 'Volkswagen T-Roc', 'Mercedes Benz': 'Mercedes-Benz'}.items():
        s = s.replace(k, v)
    if s.startswith('MINI'):
        return 'MINI', 'Countryman' if 'ountryman' in s or s == 'MINI' else s[5:]
    if s.startswith('Li Auto'):
        return 'Li Auto', s[8:]
    m = re.match(r'MG\s*(\d+)$', s)
    if m:
        return 'MG', m.group(1)
    for b in ('Mercedes-Benz', 'Li Auto', 'Land Rover', 'Great Wall'):
        if s.startswith(b + ' '):
            return b, s[len(b) + 1:]
    p = s.split(' ', 1)
    return p[0], (p[1] if len(p) > 1 else '')


def parse_excel(data: bytes, filename: str) -> list[dict]:
    rows = read_rows(data, filename)
    out = []
    for r in rows:
        if len(r) < 11:
            continue
        url = cell_str(r[0])
        if 'avtomir' not in url:
            continue
        brand, model = brand_model(cell_str(r[1]))
        ds = cell_str(r[5]).lower()
        y = re.search(r'20\d\d', ds)
        if not y:
            continue
        mon = next((v for k, v in MON.items() if k in ds), 1)
        hp = re.search(r'\d+', cell_str(r[9]))
        try:
            price = int(float(cell_str(r[10]).replace(' ', '').replace(',', '.')))
        except ValueError:
            price = 0
        try:
            km = int(float(cell_str(r[6]).replace(' ', '')))
        except ValueError:
            km = 0
        out.append(dict(
            lot=cell_str(r[2]), url=url.replace('https://', 'http://'), brand=brand, model=model,
            trim=re.sub(r'\s+', ' ', cell_str(r[4])), color=cell_str(r[3]).replace(' /', '/').replace('Чёрный', 'Черный'),
            made=f"{y.group()}-{mon:02d}-01", km=km, cond=cell_str(r[7]), test=cell_str(r[8]),
            hp=int(hp.group()) if hp else '', price=price))
    return out


# ---------------------------------------------------------------- сайт

def parse_page(html: str) -> dict | None:
    t = re.search(r'<title>(.*?)</title>', html, re.S)
    title = re.sub(r'\s*-\s*(汽车|Cars)\s*-\s*$', '', htmlmod.unescape(t.group(1)).strip()) if t else ''
    if not title or title == '404':
        return None
    text = htmlmod.unescape(re.sub(r'<[^>]+>', ' ', html))
    m = re.search(r'【排\s*量】\s*([^【]+)', text)
    cn = m.group(1).strip() if m else ''
    e = re.search(r'(\d\.\d)\s*([LT])?', cn, re.I)
    eng = ''
    if e and '续航' not in cn:
        letter = (e.group(2) or '').upper() or ('T' if re.search(r'\d\.\dT|TURBO|\d{3}T\b', title.upper()) or 'ALL4' in cn else 'L')
        eng = e.group(1) + letter
    h = re.search(r'(\d+)\s*马力', cn)
    urls = re.findall(r'<img[^>]+src="(https?://heyuncar\.bebestweb\.com/[^"]+)"', html)
    photos = [re.sub(r'^https?://heyuncar\.bebestweb\.com/', '', u).replace('.jpg', '') for u in urls]
    return dict(title=title, cn=cn, eng=eng, hp=int(h.group(1)) if h else '', photos=photos)


async def fetch_page(client: httpx.AsyncClient, url: str) -> dict | None:
    r = await client.get(url, timeout=SITE_TIMEOUT, follow_redirects=True)
    r.encoding = 'utf-8'
    return parse_page(r.text)


# ---------------------------------------------------------------- обогащение новой машины

CC = {('1.0T', None): 988, ('1.2T', None): 1197, ('1.3T', None): 1332, ('1.4T', None): 1395, ('1.4L', None): 1368,
      ('1.5L', None): 1498, ('1.5T', None): 1497, ('1.6L', None): 1591, ('1.6T', None): 1591, ('1.8L', None): 1798,
      ('1.8T', None): 1798, ('2.0L', None): 1998, ('2.0T', None): 1998, ('2.5L', None): 2487,
      ('1.4T', 'Kia'): 1353, ('1.4T', 'Hyundai'): 1353, ('1.5L', 'Kia'): 1497, ('1.5L', 'Hyundai'): 1497,
      ('1.5L', 'Toyota'): 1496, ('1.5T', 'BMW'): 1499, ('1.5T', 'MINI'): 1499, ('1.4T', 'Geely'): 1398,
      ('1.5T', 'Mercedes-Benz'): 1497, ('2.0L', 'Lexus'): 1986, ('2.0L', 'Subaru'): 1995}


def gearbox(title: str, typ: str) -> str:
    t = ' ' + title.upper() + ' '
    if typ == 'Электро':
        return 'редуктор'
    if 'CVT' in t:
        return 'вариатор'
    if re.search(r'\b(DSG|DCT|WET DCT)\b', t):
        return 'робот'
    if re.search(r'\b(AT|AUTOMATIC|AUTO)\b', t):
        return 'автомат'
    return ''


def drive(title: str) -> str:
    t = title.upper()
    if re.search(r'4WD|AWD|XDRIVE|4MATIC|QUATTRO|四驱|ALL4', t):
        return 'полный'
    if re.search(r'2WD|FWD|两驱', t):
        return 'передний'
    return ''


def std_photos(photos: list[str]) -> list[str]:
    """Стандартная схема съёмки дилера: 1–6 кузов, 7 торпедо, 10 передние сиденья, 11 задние, 12 шильдик.
    Кузов по кругу: 2,1,3,6,4,5. Шильдик и всё после 11-го не берём."""
    if len(photos) >= 11:
        idx = [2, 1, 3, 6, 4, 5, 7, 10, 11]
    else:
        idx = list(range(1, min(len(photos), 10) + 1))
    return [photos[i - 1] for i in idx if i <= len(photos)]


def encode_photos(paths: list[str]) -> str:
    if not paths:
        return ''
    pre = paths[0].rsplit('/', 1)[0]
    if all(p.rsplit('/', 1)[0] == pre for p in paths):
        return pre + ':' + ','.join(p.rsplit('/', 1)[1] for p in paths)
    return ','.join(paths)


def enrich(row: dict, site: dict) -> dict:
    title = site['title']
    m = re.search(r'\b(20\d\d|2\d Model)\b', title)
    trim = title[m.start():] if m else row['trim']
    if row['model'].isdigit() and trim.startswith(row['model'] + ' '):      # «Peugeot 2008 2022 …»
        trim = trim[len(row['model']) + 1:]
    trim = re.sub(r'^(20\d\d)\s*(года)?,?\s*', r'\1 ', re.sub(r'\s+', ' ', trim)).strip()
    eng = site['eng']
    typ = 'ДВС'
    if not eng:
        typ = 'Гибрид' if row['brand'] == 'Li Auto' else 'Электро'
    elif re.search(r'hybrid|гибрид|DM-i|混动', title, re.I):
        typ = 'Гибрид'
    eng_txt = 'электро' if typ == 'Электро' else (eng + (' гибрид' if typ == 'Гибрид' else '') if eng else '')
    cc = CC.get((eng, row['brand'])) or CC.get((eng, None)) or ''
    out = dict(row, trim=trim, eng=eng_txt, type=typ, cc=cc if typ != 'Электро' else '',
               hp=site['hp'] or row['hp'], gb=gearbox(title, typ), drive=drive(title + ' ' + site.get('cn', '')),
               photos=encode_photos(std_photos(site['photos'])), full=True)
    return out


def sheet_key(bot_token: str) -> str:
    return hashlib.sha256((bot_token + ':direct-sheet').encode()).hexdigest()


def norm(lot) -> str:
    return re.sub(r'^0+(?=\d)', '', str(lot).strip().upper())
