"""Веб-сервер мини-приложения на нашем сервере (за Caddy с HTTPS).

Зачем: у части клиентов в России плохо открываются GitHub Pages, Google Таблицы, telegram.org и китайский
фотохостинг. Здесь всё отдаётся с одного адреса:
  /                     — само приложение (файлы из папки app репозитория);
  /tg.js                — скрипт Telegram для мини-приложений (свежая копия с telegram.org, если удалось скачать);
  /api/catalog.csv      — каталог: бот сам раз в 5 минут забирает его из Google и держит копию (и на диске);
  /img/<путь>.jpg?w=640 — фото машин: бот скачивает с китайского хостинга один раз, уменьшает и хранит у себя.
"""
import asyncio
import logging
import re
import time
from pathlib import Path

import httpx
from aiohttp import web

import posts

log = logging.getLogger("web")

PORT = 8088
APP_DIR = Path(__file__).parent.parent / "app"
WIDTHS = (160, 640, 1000, 2000)
PATH_RE = re.compile(r"^[A-Za-z0-9_/-]{1,200}$")
REFRESH = 300            # каталог из Google — раз в 5 минут
IMG_MAX_AGE = 45 * 86400  # фото, которые давно не запрашивали, удаляем


class Site:
    def __init__(self, data_dir: Path, http: httpx.AsyncClient, on_log=None):
        self.http = http
        self.on_log = on_log            # сообщения об ошибках с телефонов клиентов → боту
        self.log_hits: dict[str, list[float]] = {}
        self.dir = data_dir / "web"
        self.img_dir = self.dir / "img"
        self.img_dir.mkdir(parents=True, exist_ok=True)
        self.csv_file = self.dir / "catalog.csv"
        self.csv = self.csv_file.read_bytes() if self.csv_file.exists() else b""
        self.csv_ts = self.csv_file.stat().st_mtime if self.csv_file.exists() else 0
        self.tg_js = self.dir / "tg.js"
        self.inflight: dict[str, asyncio.Future] = {}
        self.sem = asyncio.Semaphore(6)

    # ---------------------------------------------------------------- каталог

    async def refresh_catalog(self) -> bool:
        r = await self.http.get(posts.CSV_URL + "&t=" + str(int(time.time() // 60)), follow_redirects=True,
                                timeout=httpx.Timeout(40.0, connect=15.0))
        r.raise_for_status()
        body = r.content
        head = body[:200].decode("utf-8", "replace").lstrip("﻿")
        if not head.startswith("lot,") or body.count(b"\n") < 2:
            raise RuntimeError("Google вернул не каталог: " + head[:80])
        if body != self.csv:
            self.csv = body
            tmp = self.csv_file.with_suffix(".tmp")
            tmp.write_bytes(body)
            tmp.replace(self.csv_file)
        self.csv_ts = time.time()
        return True

    async def catalog(self, request: web.Request):
        if not self.csv:
            try:
                await self.refresh_catalog()
            except Exception as e:
                log.warning("каталог: %s", e)
                raise web.HTTPServiceUnavailable(text="catalog not ready")
        return web.Response(body=self.csv, content_type="text/csv", charset="utf-8",
                            headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})

    # ---------------------------------------------------------------- фото

    def img_path(self, path: str, w: int) -> Path:
        return self.img_dir / str(w) / (path + ".jpg")

    async def fetch_img(self, path: str, w: int) -> Path:
        dst = self.img_path(path, w)
        if dst.exists():
            return dst
        key = f"{w}/{path}"
        if key in self.inflight:          # то же фото уже качается — ждём
            return await self.inflight[key]
        fut = asyncio.get_running_loop().create_future()
        self.inflight[key] = fut
        try:
            async with self.sem:
                url = f"{posts.IMG_HOST}{path}.jpg?x-oss-process=image/resize,w_{w}/quality,q_80"
                r = await self.http.get(url, timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True)
                r.raise_for_status()
                if not r.headers.get("content-type", "").startswith("image/"):
                    raise RuntimeError("не картинка")
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_suffix(".tmp")
            tmp.write_bytes(r.content)
            tmp.replace(dst)
            fut.set_result(dst)
            return dst
        except Exception as e:
            fut.set_exception(e)
            fut.exception()   # чтобы не было предупреждения «исключение не прочитано»
            raise
        finally:
            self.inflight.pop(key, None)

    async def image(self, request: web.Request):
        path = request.match_info["path"]
        if path.endswith(".jpg"):
            path = path[:-4]
        try:
            w = int(request.query.get("w", "640"))
        except ValueError:
            w = 640
        w = min(WIDTHS, key=lambda x: abs(x - w))
        if not PATH_RE.match(path) or ".." in path:
            raise web.HTTPNotFound()
        try:
            f = await self.fetch_img(path, w)
        except Exception as e:
            log.warning("фото %s: %s", path, e)
            # не смогли скачать — отправляем клиента напрямую на хостинг
            raise web.HTTPFound(f"{posts.IMG_HOST}{path}.jpg?x-oss-process=image/resize,w_{w}")
        return web.FileResponse(f, headers={"Cache-Control": "public, max-age=2592000, immutable",
                                            "Content-Type": "image/jpeg"})

    async def prewarm(self):
        """После обновления каталога заранее скачиваем фото: главные — для плиток, остальные — для карточек."""
        try:
            cars = self.parse_cars()
        except Exception:
            return
        jobs = []
        for c in cars:
            for k, u in enumerate(c["ph"]):
                path = u[len(posts.IMG_HOST):-4]
                jobs.append((0 if k == 0 else 1, path, 640))
        jobs.sort()
        for _, path, w in jobs:
            if not self.img_path(path, w).exists():
                try:
                    await self.fetch_img(path, w)
                except Exception:
                    pass
        self.cleanup()

    def parse_cars(self) -> list[dict]:
        import csv
        import io
        rows = csv.DictReader(io.StringIO(self.csv.decode("utf-8", "replace").lstrip("﻿")))
        return [{"ph": posts.photo_list(r.get("photos", ""))} for r in rows if r.get("lot")]

    def cleanup(self):
        now = time.time()
        for f in self.img_dir.rglob("*.jpg"):
            try:
                if now - f.stat().st_atime > IMG_MAX_AGE and now - f.stat().st_mtime > IMG_MAX_AGE:
                    f.unlink()
            except Exception:
                pass

    # ---------------------------------------------------------------- скрипт Telegram и файлы приложения

    async def refresh_tg_js(self):
        r = await self.http.get("https://telegram.org/js/telegram-web-app.js", timeout=30, follow_redirects=True)
        r.raise_for_status()
        if b"Telegram" in r.content and len(r.content) > 20000:
            self.tg_js.write_bytes(r.content)

    async def tg_script(self, request: web.Request):
        f = self.tg_js if self.tg_js.exists() else APP_DIR / "tg.js"
        return web.FileResponse(f, headers={"Cache-Control": "public, max-age=3600",
                                            "Content-Type": "application/javascript; charset=utf-8"})

    async def static(self, request: web.Request):
        name = request.match_info.get("name") or "index.html"
        if not re.match(r"^[A-Za-z0-9_.-]+$", name) or name.startswith("."):
            raise web.HTTPNotFound()
        f = APP_DIR / name
        if not f.is_file():
            raise web.HTTPNotFound()
        cache = "no-cache" if name.endswith(".html") else "public, max-age=86400"
        return web.FileResponse(f, headers={"Cache-Control": cache})

    async def client_log(self, request: web.Request):
        """Приложение у клиента сообщает об ошибке (или что каталог загрузился). Не чаще 20 раз в час с одного IP."""
        hdr = {"Access-Control-Allow-Origin": "*"}
        ip = request.headers.get("X-Forwarded-For", request.remote or "").split(",")[0].strip()
        now = time.time()
        hits = [t for t in self.log_hits.get(ip, []) if now - t < 3600]
        if len(hits) >= 20:
            return web.Response(status=204, headers=hdr)
        self.log_hits[ip] = hits + [now]
        if len(self.log_hits) > 5000:
            self.log_hits.clear()
        try:
            raw = await request.content.read(4096)
            import json
            d = json.loads(raw.decode("utf-8", "replace"))
            d = {k: str(d.get(k, ""))[:600] for k in ("k", "m", "ua", "h", "p", "v")}
        except Exception:
            return web.Response(status=204, headers=hdr)
        log.info("клиент %s: %s", d["k"], d)
        with open(self.dir / "client_log.txt", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + json.dumps(d, ensure_ascii=False) + "\n")
        if self.on_log:
            try:
                await self.on_log(d)
            except Exception as e:
                log.warning("on_log: %s", e)
        return web.Response(status=204, headers=hdr)

    async def health(self, request: web.Request):
        return web.json_response({"ok": True, "catalog_age": int(time.time() - self.csv_ts) if self.csv_ts else None,
                                  "catalog_bytes": len(self.csv)})

    # ---------------------------------------------------------------- запуск

    async def loop(self, report):
        fails, n = 0, 0
        while True:
            try:
                await self.refresh_catalog()
                fails = 0
                if n % 12 == 0:           # раз в час — догружаем фото и обновляем скрипт Telegram
                    asyncio.create_task(self.prewarm())
                    try:
                        await self.refresh_tg_js()
                    except Exception as e:
                        log.warning("tg.js: %s", e)
                n += 1
            except Exception as e:
                fails += 1
                log.warning("каталог не обновился (%s): %s", fails, e)
                if fails == 6:   # полчаса подряд — сообщить
                    await report("каталог для приложения не обновляется 30 минут (клиенты видят последнюю копию)",
                                 repr(e)[:500])
            await asyncio.sleep(REFRESH)

    async def start(self, report):
        app = web.Application()
        app.router.add_get("/api/catalog.csv", self.catalog)
        app.router.add_get("/api/health", self.health)
        app.router.add_post("/api/log", self.client_log)
        app.router.add_get("/img/{path:.+}", self.image)
        app.router.add_get("/tg.js", self.tg_script)
        app.router.add_get("/", self.static)
        app.router.add_get("/{name}", self.static)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", PORT).start()
        asyncio.create_task(self.loop(report))
        log.info("веб-сервер на 127.0.0.1:%s", PORT)
