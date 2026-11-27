"""BlogTube · construye site/data/*.json a partir de sources.toml.

Uso:
    python build.py            # incremental: solo lo nuevo de cada fuente
    python build.py --full     # vuelve a listar el historial completo de cada canal
    python build.py --only "Ernesto Castro"   # solo las fuentes cuyo nombre contenga eso

Todo lo descargado se acumula en cache/ (se versiona en git), así que si un día
YouTube o Substack fallan, el sitio sigue funcionando con lo que ya tenía.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
import tomllib
import unicodedata
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
DATA = ROOT / "site" / "data"
ADIR = DATA / "a"
BODIES = CACHE / "bodies"   # textos íntegros: solo en local, nunca se suben al repo público
UA = {"User-Agent": "Mozilla/5.0 (BlogTube personal feed reader)"}
BODY_CAP = 60          # cuerpos de artículo nuevos a descargar por blog y ejecución
BRIEF = 200            # longitud máx. del resumen público de cada artículo
TEXTOS = False         # True → publicar el texto íntegro en site/data/a (solo para uso local)
PERSONAS_RX: list = [] # [(slug, regex)] para detectar menciones en el texto al guardarlo
INCR_VIDEOS = 90       # en modo incremental, cuántos vídeos recientes se miran por canal
SEARCH_N = 30          # resultados por búsqueda de persona
MIN_SEARCH_DUR = 240   # descarta vídeos de búsqueda de menos de 4 min (shorts, clips)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def log(*a):
    print(*a, flush=True)


def load(p: Path, default=None):
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return default


def save(p: Path, obj, compact=False):
    p.parent.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(obj, ensure_ascii=False, separators=(",", ":") if compact else None,
                     indent=None if compact else 1)
    p.write_text(txt, "utf-8")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9'.\- ]+", " ", s)


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", norm(s)).strip("-")[:60]


def short_id(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:10]


# ---------------------------------------------------------------------------
#  Clasificación por núcleos temáticos y personas
# ---------------------------------------------------------------------------
class Tagger:
    def __init__(self, temas, personas):
        self.names = [t["name"] for t in temas]
        self.rx = [self._compile(t.get("keywords", [])) for t in temas]
        self.prx = [self._compile(p.get("match", [])) for p in personas]

    @staticmethod
    def _compile(kws, whole=False):
        parts = []
        for raw in kws:
            exact = raw.endswith("$")             # "dune$" → solo la palabra completa (no "Dunedin")
            k = norm(raw.rstrip("$")).strip()
            if not k:
                continue
            body = re.escape(k)
            end = r"(?![a-z0-9])" if whole or exact or len(k) <= 3 else ""   # palabras cortas: palabra completa
            parts.append(r"(?<![a-z0-9])" + body + end)
        return re.compile("|".join(parts)) if parts else None

    def topics(self, text: str, defaults: list[int]) -> int:
        t = norm(text)
        mask = 0
        for i in defaults:
            mask |= 1 << i
        for i, rx in enumerate(self.rx):
            if rx and rx.search(t):
                mask |= 1 << i
        return mask

    def people(self, text: str, base: int = 0) -> int:
        t = norm(text)
        mask = base
        for i, rx in enumerate(self.prx):
            if rx and rx.search(t):
                mask |= 1 << i
        return mask


# ---------------------------------------------------------------------------
#  YouTube
# ---------------------------------------------------------------------------
def ydl(lang="es", end=None):
    from yt_dlp import YoutubeDL
    opts = {
        "quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True,
        "ignoreerrors": True,
        "extractor_args": {"youtube": {"lang": [lang]}, "youtubetab": {"approximate_date": [""]}},
    }
    if end:
        opts["playlistend"] = end
    return YoutubeDL(opts)


def ymd(e) -> str | None:
    d = e.get("upload_date")
    if d and len(d) == 8:
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    ts = e.get("timestamp")
    if ts:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    return None


def yt_rss(cid: str) -> list[dict]:
    """Últimos ~15 vídeos vía RSS oficial: fechas exactas y respaldo si yt-dlp falla."""
    try:
        r = requests.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}", headers=UA, timeout=20)
        f = feedparser.parse(r.content)
        out = []
        for e in f.entries:
            vid = e.get("yt_videoid")
            if not vid or "/shorts/" in e.get("link", ""):
                continue
            views = None
            st = e.get("media_statistics")
            if st and st.get("views"):
                views = int(st["views"])
            out.append({"id": vid, "t": e.get("title", ""), "date": e.get("published", "")[:10], "v": views})
        return out
    except Exception as ex:
        log("   ! RSS falló:", ex)
        return []


def fetch_channel(ch: dict, full: bool) -> dict:
    cid = ch["id"]
    path = CACHE / "yt" / f"{cid}.json"
    old = load(path, {"order": [], "videos": {}, "meta": {}})
    vids: dict = old["videos"]
    order: list = old["order"]
    before = set(vids)
    do_full = full or not order
    log(f" ▶ {ch['name']} ({'completo' if do_full else 'incremental'})")
    fetched = []
    meta = old.get("meta", {})
    for tab in ch.get("tabs", ["videos"]):
        try:
            info = ydl(ch.get("lang", "es"), None if do_full else INCR_VIDEOS).extract_info(
                f"https://www.youtube.com/channel/{cid}/{tab}", download=False)
        except Exception as ex:
            info = None
            log("   ! yt-dlp falló:", ex)
        if not info:
            continue
        meta = {"desc": (info.get("description") or "").strip()[:600],
                "subs": info.get("channel_follower_count"), "url": info.get("channel_url") or f"https://www.youtube.com/channel/{cid}"}
        for e in info.get("entries") or []:
            if not e or not e.get("id") or len(e["id"]) != 11:
                continue
            fetched.append(e["id"])
            prev = vids.get(e["id"], {})
            vids[e["id"]] = {
                "t": e.get("title") or prev.get("t", ""),
                "d": e.get("duration") or prev.get("d"),
                "v": e.get("view_count") if e.get("view_count") is not None else prev.get("v"),
                "date": prev.get("date") or ymd(e),
            }
    rss = yt_rss(cid)
    for r in rss:
        prev = vids.get(r["id"])
        if prev:
            prev["date"] = r["date"] or prev.get("date")      # la fecha exacta del RSS manda
            if r["v"] is not None:
                prev["v"] = max(prev.get("v") or 0, r["v"])
        elif not fetched:   # yt-dlp falló del todo: usa el RSS como respaldo
            vids[r["id"]] = {"t": r["t"], "d": None, "v": r["v"], "date": r["date"]}
            fetched.append(r["id"])
    if do_full and fetched:
        order = list(dict.fromkeys(fetched))
        vids = {k: vids[k] for k in order}
    else:
        known = set(order)
        new = [v for v in dict.fromkeys(fetched) if v not in known]
        order = new + order
    log(f"   {len(order)} vídeos ({len(set(fetched) - before)} nuevos)")
    data = {"order": order, "videos": vids, "meta": meta, "updated": now()}
    save(path, data)
    return data


def search_person(p: dict) -> dict:
    path = CACHE / "people" / f"{p['slug']}.json"
    old = load(path, {"videos": {}})
    vids = old["videos"]
    for q in p.get("buscar", []):
        try:
            info = ydl("es").extract_info(f"ytsearch{SEARCH_N}:{q}", download=False)
        except Exception as ex:
            log("   ! búsqueda falló:", q, ex)
            continue
        for e in (info or {}).get("entries") or []:
            if not e or not e.get("id") or not e.get("duration") or e["duration"] < MIN_SEARCH_DUR:
                continue
            vids[e["id"]] = {"t": e.get("title", ""), "d": e.get("duration"), "v": e.get("view_count"),
                             "ch": e.get("channel") or e.get("uploader") or "YouTube",
                             "chid": e.get("channel_id"), "date": vids.get(e["id"], {}).get("date") or ymd(e)}
    log(f" ⌕ {p['name']}: {len(vids)} vídeos")
    data = {"videos": vids, "updated": now()}
    save(path, data)
    return data


# ---------------------------------------------------------------------------
#  Blogs / Substack
# ---------------------------------------------------------------------------
SKIP_CONTENT = {"script", "style", "form", "button", "svg", "noscript", "iframe", "input", "select", "textarea", "head", "title"}
ALLOWED = {"p", "br", "h2", "h3", "h4", "h5", "h6", "strong", "b", "em", "i", "u", "s", "blockquote", "ul", "ol",
           "li", "a", "img", "figure", "figcaption", "pre", "code", "hr", "table", "thead", "tbody", "tr", "td", "th",
           "sup", "sub", "span", "div", "small", "mark", "cite", "q", "dl", "dt", "dd"}
REMAP = {"h1": "h2"}
VOID = {"br", "img", "hr"}
JUNK_CLASS = re.compile(r"subscription-widget|subscribe-widget|button-wrapper|captioned-button|share-dialog|"
                        r"footnote-anchor-|post-ufi|image-link-expand|restack|paywall|digest-post-embed", re.I)


class Sanitizer(HTMLParser):
    """Lista blanca de etiquetas/atributos: nada de scripts, estilos ni manejadores."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.stack = [], 0, []

    def handle_starttag(self, tag, attrs):
        tag = REMAP.get(tag, tag)
        a = dict(attrs)
        if self.skip or tag in SKIP_CONTENT or JUNK_CLASS.search(a.get("class") or "") or a.get("role") == "tooltip":
            if tag not in VOID:
                self.skip += 1
            return
        if tag not in ALLOWED:
            return
        keep = []
        if tag == "a":
            href = a.get("href") or ""
            if href.startswith(("http://", "https://", "#", "mailto:")):
                keep.append(f'href="{html.escape(href)}" target="_blank" rel="noopener"')
        elif tag == "img":
            src = a.get("src") or ""
            if not src.startswith(("http://", "https://")):
                return
            keep.append(f'src="{html.escape(src)}" loading="lazy" alt="{html.escape(a.get("alt") or "")}"')
        elif tag in ("td", "th") and a.get("colspan", "").isdigit():
            keep.append(f'colspan="{a["colspan"]}"')
        self.out.append(f"<{tag}{' ' + ' '.join(keep) if keep else ''}>")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        tag = REMAP.get(tag, tag)
        if self.skip:
            if tag not in VOID:
                self.skip -= 1
            return
        if tag in ALLOWED and tag not in VOID and tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))

    def result(self):
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        s = "".join(self.out)
        s = re.sub(r"<(p|div|span|figure)>\s*</\1>", "", s)
        return s


def sanitize(h: str) -> str:
    p = Sanitizer()
    try:
        p.feed(h or "")
        p.close()
    except Exception:
        return html.escape(re.sub(r"<[^>]+>", " ", h or ""))
    return p.result()


def text_of(h: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", h or ""))).strip()


def first_img(h: str) -> str | None:
    m = re.search(r'<img[^>]+src="(https?://[^"]+)"', h or "")
    return m.group(1) if m else None


def brief(txt: str, n: int = BRIEF) -> str:
    txt = re.sub(r"\s+", " ", txt or "").strip()
    if len(txt) <= n:
        return txt
    cut = txt[:n].rsplit(" ", 1)[0].rstrip(",;:.-–— ")
    return cut + "…"


def load_blog(path: Path) -> dict | None:
    """Carga los metadatos de un blog y vuelve a unir los textos guardados en cache/bodies."""
    data = load(path)
    if not data:
        return None
    inline = any("body" in p for p in data["posts"].values())
    for p in data["posts"].values():
        if not p.get("body") and p.get("hasbody"):
            f = BODIES / f"{short_id(p['url'])}.html"
            if f.exists():
                p["body"] = f.read_text("utf-8")
    if inline:                       # formato antiguo: migra los textos fuera del JSON
        save_blog(path, data["posts"])
        return load_blog(path)
    return data


def save_blog(path: Path, posts: dict):
    """Guarda metadatos (públicos) y textos íntegros (locales) por separado."""
    BODIES.mkdir(parents=True, exist_ok=True)
    out = {}
    for url, p in posts.items():
        q = {k: v for k, v in p.items() if k != "body"}
        body = p.get("body")
        if body:
            f = BODIES / f"{short_id(url)}.html"
            if not f.exists() or f.stat().st_size != len(body.encode("utf-8")):
                f.write_text(body, "utf-8")
            txt = text_of(body)
            q["hasbody"] = True
            q["words"] = q.get("words") or len(txt.split())
            if not q.get("sub"):
                q["sub"] = brief(txt)
            if PERSONAS_RX:
                nt = norm(txt[:20000])
                q["ppl"] = [slug for slug, rx in PERSONAS_RX if rx.search(nt)]
        out[url] = q
    save(path, {"posts": out, "updated": now()})


def fetch_blog(b: dict, full: bool) -> dict:
    slug = slugify(b["name"])
    path = CACHE / "blogs" / f"{slug}.json"
    old = load_blog(path) or {"posts": {}}
    posts: dict = old["posts"]          # url -> post
    log(f" ✎ {b['name']}")
    # 1) RSS: lo más reciente, normalmente con el texto completo
    try:
        r = requests.get(b["feed"], headers=UA, timeout=25)
        f = feedparser.parse(r.content)
        for e in f.entries:
            url = (e.get("link") or "").split("?")[0]
            if not url:
                continue
            body = ""
            if e.get("content"):
                body = e.content[0].get("value", "")
            elif e.get("summary"):
                body = e.summary
            date = None
            for k in ("published_parsed", "updated_parsed"):
                if e.get(k):
                    date = time.strftime("%Y-%m-%d", e[k])
                    break
            cover = None
            for enc in e.get("enclosures", []) or []:
                if (enc.get("type") or "").startswith("image"):
                    cover = enc.get("href")
            p = posts.get(url, {})
            sub = e.get("summary", "") if e.get("content") else ""
            p.update({"t": html.unescape(e.get("title", "")).strip(), "url": url,
                      "sub": p.get("sub") or text_of(sub)[:300], "date": date or p.get("date"),
                      "cover": p.get("cover") or cover or first_img(body)})
            btxt_new = text_of(body)
            if len(btxt_new) > 400 and len(btxt_new) > len(text_of(p.get("body", ""))):
                p["body"] = body
            posts[url] = p
    except Exception as ex:
        log("   ! RSS falló:", ex)
    # 2) Archivo completo de Substack (solo metadatos)
    if b.get("substack"):
        base = b["feed"].rsplit("/feed", 1)[0]
        cursor, pages = None, 0
        while True:
            params = {"sort": "new", "limit": 25}
            if cursor:
                params["cursor"] = cursor
            try:
                r = requests.get(f"{base}/api/v1/archive", params=params, headers=UA, timeout=25)
                js = r.json() if r.ok else {}
            except Exception as ex:
                log("   ! archivo falló:", ex)
                break
            items = js if isinstance(js, list) else js.get("posts", [])
            cursor = None if isinstance(js, list) else js.get("nextCursor")
            if not items:
                break
            known = 0
            for it in items:
                if it.get("type") not in (None, "newsletter", "podcast"):
                    continue
                url = (it.get("canonical_url") or "").split("?")[0]
                if not url:
                    continue
                if url in posts and posts[url].get("api"):
                    known += 1
                p = posts.get(url, {})
                p.update({"t": (it.get("title") or p.get("t") or "").strip(), "url": url,
                          "sub": (it.get("subtitle") or it.get("description") or p.get("sub") or "")[:300],
                          "date": (it.get("post_date") or "")[:10] or p.get("date"),
                          "cover": it.get("cover_image") or p.get("cover"),
                          "words": it.get("wordcount") or p.get("words"),
                          "paid": it.get("audience") == "only_paid",
                          "slug": it.get("slug"), "api": True})
                if it.get("body_html") and len(it["body_html"]) > len(p.get("body", "")):
                    p["body"] = it["body_html"]
                posts[url] = p
            pages += 1
            if not cursor or (not full and known == len(items)) or pages > 200:
                break
            time.sleep(0.4)
        # 3) Cuerpos que faltan (solo posts abiertos), con tope por ejecución
        missing = [p for p in posts.values() if not p.get("body") and not p.get("hasbody") and not p.get("paid")
                   and p.get("slug") and not p.get("nobody")]
        missing.sort(key=lambda p: p.get("date") or "", reverse=True)
        for i, p in enumerate(missing[:BODY_CAP], 1):
            if i % 25 == 0:
                log(f"   … {i}/{min(len(missing), BODY_CAP)} cuerpos")
            try:
                r = requests.get(f"{base}/api/v1/posts/{p['slug']}", headers=UA, timeout=25)
                js = r.json() if r.ok else {}
                if js.get("body_html"):
                    p["body"] = js["body_html"]
                else:
                    p["nobody"] = True
            except Exception:
                pass
            time.sleep(0.4)
        if missing:
            log(f"   cuerpos descargados: {min(len(missing), BODY_CAP)} de {len(missing)} pendientes")
    log(f"   {len(posts)} artículos")
    save_blog(path, posts)
    return {"posts": posts}


# ---------------------------------------------------------------------------
#  El Turrero Post (hilos de Javier G. Recuenco) — sin RSS: se lee /turras y cada /turra/<id>
#  (robots.txt permite todo salvo /api/, que no se usa)
# ---------------------------------------------------------------------------
TURRA_LI = re.compile(r'<time dateTime="([^"]+)"[^>]*>.*?</time>\s*<div class="min-w-0"><a[^>]*href="(/turra/\d+)"[^>]*>(.*?)</a>'
                      r'(?:<p[^>]*>(.*?)</p>)?', re.S)
QUOTE_CARD = re.compile(r'<a href="([^"]+)"[^>]*class="block mt-4">(.*?)</div></a>', re.S)


def turra_body(page: str) -> tuple[str, dict]:
    meta = {}
    m = re.search(r'application/ld\+json">(.*?)</script>', page, re.S)
    if m:
        try:
            ld = json.loads(m.group(1))
            art = next((x for x in (ld if isinstance(ld, list) else [ld]) if x.get("@type") == "Article"), {})
            meta = {"author": (art.get("author") or {}).get("name", ""), "desc": art.get("description", ""),
                    "date": (art.get("datePublished") or "")[:10], "section": art.get("articleSection", "")}
        except Exception:
            pass
    og = re.search(r'<meta property="og:image" content="([^"]+)"', page)
    if og:
        meta["cover"] = html.unescape(og.group(1))
    a, e = page.find("<article"), page.find("</article>")
    if a < 0 or e < 0:
        return "", meta
    art = page[a:e]
    art = re.sub(r"<svg.*?</svg>", "", art, flags=re.S)
    art = re.sub(r'<div class="not-prose[^"]*">.*?</div></div></div>', "", art, flags=re.S)   # huecos de imágenes

    def card(mm):
        inner = mm.group(2)
        who = re.search(r'<span class="text-sm font-medium[^"]*">([^<]+)</span>', inner)
        txt = re.search(r'<p(?:\s[^>]*)?>(.*?)</p>', inner, re.S)
        return (f'<blockquote><p><strong>{who.group(1) if who else "Tweet citado"}</strong></p>'
                f'<p>{txt.group(1) if txt else ""}</p><p><a href="{mm.group(1)}">Ver en X ↗</a></p></blockquote>')
    art = QUOTE_CARD.sub(card, art)
    return art, meta


def fetch_turrero(b: dict, full: bool) -> dict:
    slug = slugify(b["name"])
    path = CACHE / "blogs" / f"{slug}.json"
    old = load_blog(path) or {"posts": {}}
    posts: dict = old["posts"]
    base = b.get("url", "https://turrero.vercel.app").rstrip("/")
    log(f" ✎ {b['name']} (Turrero)")
    try:
        listing = requests.get(f"{base}/turras", headers=UA, timeout=30).text
    except Exception as ex:
        log("   ! listado falló:", ex)
        listing = ""
    items = TURRA_LI.findall(listing)
    log(f"   {len(items)} hilos en el listado")
    for dt_, href, title, cats in items:
        url = base + href
        p = posts.get(url, {})
        p.update({"t": html.unescape(re.sub(r"<[^>]+>", "", title)).strip(), "url": url, "date": dt_[:10],
                  "cats": html.unescape(re.sub(r"<[^>]+>", "", cats or "")).strip()})
        posts[url] = p
    todo = [p for p in posts.values() if (full or "author" not in p)]
    todo.sort(key=lambda p: p.get("date") or "", reverse=True)
    fetched = 0
    for p in todo[:BODY_CAP]:
        try:
            page = requests.get(p["url"], headers=UA, timeout=30).text
        except Exception:
            continue
        body, meta = turra_body(page)
        p["author"] = meta.get("author", "")
        p["sub"] = meta.get("desc") or p.get("sub", "")
        p["cover"] = meta.get("cover") or p.get("cover")
        if body:
            p["body"] = body
        fetched += 1
        time.sleep(0.6)
    if todo:
        log(f"   hilos descargados: {fetched} de {len(todo)} pendientes")
    autor = b.get("solo_autor")
    if autor:
        for p in posts.values():
            p["skip"] = bool(p.get("author")) and autor.lower() not in p["author"].lower()
    log(f"   {len([p for p in posts.values() if not p.get('skip')])} hilos")
    save_blog(path, posts)
    return {"posts": posts}


# ---------------------------------------------------------------------------
#  Composición de site/data
# ---------------------------------------------------------------------------
def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="re-listar historial completo de los canales")
    ap.add_argument("--only", default="", help="actualizar solo fuentes cuyo nombre contenga este texto")
    ap.add_argument("--offline", action="store_true", help="no descargar nada; solo recomponer desde cache/")
    ap.add_argument("--solo", choices=["videos", "blogs", "personas"], help="descargar solo un tipo de fuente")
    ap.add_argument("--bodies", type=int, default=60, help="máx. cuerpos de artículo nuevos por blog")
    ap.add_argument("--textos", action="store_true",
                    help="incluir el texto íntegro de los artículos en site/data (solo uso local, NO publicar)")
    args = ap.parse_args()
    global BODY_CAP, TEXTOS
    BODY_CAP = args.bodies

    cfg = tomllib.loads((ROOT / "sources.toml").read_text("utf-8"))
    temas, personas = cfg.get("tema", []), cfg.get("persona", [])
    tnames = [t["name"] for t in temas]
    pidx = {p["slug"]: i for i, p in enumerate(personas)}
    tg = Tagger(temas, personas)
    TEXTOS = args.textos or bool(cfg.get("site", {}).get("publicar_textos", False))
    PERSONAS_RX[:] = [(p["slug"], rx) for p, rx in zip(personas, tg.prx) if rx]

    def tmask_defaults(src):
        bad = [t for t in src.get("temas", []) if t not in tnames]
        if bad:
            log(f"   ! temas desconocidos en {src['name']}: {bad}")
        return [tnames.index(t) for t in src.get("temas", []) if t in tnames]

    def want(name, kind):
        if args.offline or (args.solo and args.solo != kind):
            return False
        return not args.only or args.only.lower() in name.lower()

    # ---- vídeos
    site_cfg = cfg.get("site", {})
    min_global = float(site_cfg.get("duracion_minima", 0)) * 60
    excl = Tagger._compile(site_cfg.get("excluir_titulos", []), whole=True)
    dropped = {"cortos": 0, "promo": 0}

    def keep_video(title, dur, min_secs):
        if excl and excl.search(norm(title)):
            dropped["promo"] += 1
            return False
        if min_secs and dur and dur < min_secs:   # sin duración conocida (directos) → se queda
            dropped["cortos"] += 1
            return False
        return True

    channels, videos, seen_ids = [], [], set()
    for ch in cfg.get("canal", []):
        data = fetch_channel(ch, args.full) if want(ch["name"], "videos") else load(CACHE / "yt" / f"{ch['id']}.json")
        if not data:
            log(f"   (sin datos para {ch['name']})")
            continue
        ci = len(channels)
        defaults = tmask_defaults(ch)
        pbase = 1 << pidx[ch["persona"]] if ch.get("persona") in pidx else 0
        min_secs = float(ch.get("duracion_minima", min_global / 60)) * 60
        n = 0
        for vid in data["order"]:
            v = data["videos"].get(vid)
            if not v or vid in seen_ids or not v.get("t") or v["t"] in ("[Private video]", "[Deleted video]"):
                continue
            if not keep_video(v["t"], v.get("d"), min_secs):
                continue
            seen_ids.add(vid)
            videos.append([vid, v["t"], v.get("d") or 0, v.get("v"), ci, tg.topics(v["t"], defaults),
                           tg.people(v["t"], pbase), v.get("date") or ""])
            n += 1
        meta = data.get("meta", {})
        channels.append({"name": ch["name"], "id": ch["id"], "url": meta.get("url") or f"https://www.youtube.com/channel/{ch['id']}",
                         "topics": defaults, "peso": ch.get("peso", 3), "n": n, "blurb": ch.get("blurb") or meta.get("desc", ""),
                         "subs": meta.get("subs"), "persona": ch.get("persona")})

    # vídeos traídos por búsquedas en YouTube (canales externos): de personas y de núcleos temáticos.
    # Las búsquedas traen ruido, así que solo entra un vídeo si su título menciona a la persona o el tema.
    searches = [(p, "personas", lambda t, bit=1 << pidx[p["slug"]]: tg.people(t) & bit) for p in personas]
    for i, t in enumerate(temas):
        if t.get("buscar") and tg.rx[i]:
            pseudo = {"slug": "tema-" + slugify(t["name"]), "name": t["name"], "buscar": t["buscar"]}
            searches.append((pseudo, "personas", lambda txt, rx=tg.rx[i]: rx.search(norm(txt))))
    ext = {}
    for p, kind, accepts in searches:
        data = search_person(p) if want(p["name"], kind) else load(CACHE / "people" / f"{p['slug']}.json")
        if not data:
            continue
        for vid, v in data["videos"].items():
            if vid in seen_ids:
                continue          # ya está (en un canal propio o en otra búsqueda)
            if not accepts(v["t"]) or not keep_video(v["t"], v.get("d"), min_global):
                continue
            pm = tg.people(v["t"])
            key = v.get("chid") or v.get("ch")
            if key not in ext:
                ext[key] = len(channels)
                channels.append({"name": v.get("ch") or "YouTube", "id": v.get("chid"), "ext": True,
                                 "url": f"https://www.youtube.com/channel/{v['chid']}" if v.get("chid") else "",
                                 "topics": [], "peso": 1, "n": 0})
            ci = ext[key]
            channels[ci]["n"] += 1
            seen_ids.add(vid)
            videos.append([vid, v["t"], v.get("d") or 0, v.get("v"), ci, tg.topics(v["t"], []), pm, v.get("date") or ""])

    # ---- artículos
    pubs, articles = [], []
    ADIR.mkdir(parents=True, exist_ok=True)
    live_bodies = set()
    for b in cfg.get("blog", []):
        fetcher = fetch_turrero if b.get("tipo") == "turrero" else fetch_blog
        data = fetcher(b, args.full) if want(b["name"], "blogs") else load_blog(CACHE / "blogs" / f"{slugify(b['name'])}.json")
        if not data:
            log(f"   (sin datos para {b['name']})")
            continue
        pi = len(pubs)
        defaults = tmask_defaults(b)
        pbase = 1 << pidx[b["persona"]] if b.get("persona") in pidx else 0
        posts = sorted(data["posts"].values(), key=lambda p: p.get("date") or "", reverse=True)
        n = 0
        for p in posts:
            if not p.get("t") or p.get("skip"):
                continue
            aid = short_id(p["url"])
            body = p.get("body") or ""
            words = p.get("words") or (len(text_of(body).split()) if body else 0)
            mins = max(1, round(words / 230)) if words else 0
            sub = brief(p.get("sub") or "")
            has = 0
            if body and TEXTOS:
                fn = ADIR / f"{aid}.json"
                clean = sanitize(body)
                if not fn.exists() or load(fn, {}).get("h") != clean:
                    save(fn, {"h": clean}, compact=True)
                live_bodies.add(fn.name)
                has = 1
            txt = p["t"] + " " + sub + " " + p.get("cats", "")
            pm = tg.people(txt, pbase)
            for slug in p.get("ppl", []):
                if slug in pidx:
                    pm |= 1 << pidx[slug]
            articles.append([aid, p["t"], sub, p.get("date") or "", pi, mins, p.get("cover") or "",
                             tg.topics(txt, defaults), pm, p["url"], has,
                             1 if p.get("paid") else 0])
            n += 1
        feed = b.get("feed", b.get("url", ""))
        site = feed.rsplit("/feed", 1)[0] if "/feed" in feed else feed.rsplit("/", 1)[0]
        pubs.append({"name": b["name"], "author": b.get("author", ""), "url": b.get("url") or site, "topics": defaults,
                     "peso": b.get("peso", 3), "n": n, "persona": b.get("persona"), "blurb": b.get("blurb", "")})
    for fn in ADIR.glob("*.json"):            # limpia cuerpos huérfanos
        if fn.name not in live_bodies:
            fn.unlink()

    meta = {
        "title": cfg.get("site", {}).get("title", "BlogTube"),
        "tagline": cfg.get("site", {}).get("tagline", ""),
        "topics": [{"name": t["name"], "color": t.get("color", "#999")} for t in temas],
        "people": [{"slug": p["slug"], "name": p["name"], "bio": p.get("bio", "")} for p in personas],
        "generated": now(),
        "textos": TEXTOS,
    }
    save(DATA / "meta.json", meta)
    save(DATA / "videos.json", {"channels": channels, "videos": videos}, compact=True)
    save(DATA / "articles.json", {"pubs": pubs, "articles": articles}, compact=True)
    log(f"\n  vídeos excluidos: {dropped['cortos']:,} cortos (< {min_global / 60:g} min) · {dropped['promo']:,} promocionales")
    log(f"✓ {len(videos):,} vídeos de {len([c for c in channels if not c.get('ext')])} canales "
        f"(+{len([c for c in channels if c.get('ext')])} externos) · {len(articles):,} artículos de {len(pubs)} blogs")


if __name__ == "__main__":
    main()
