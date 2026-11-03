"""Static, double-click-able preview of the Guía Salud staging site (no server needed).
Renders the real Flask app (production / no-buyer mode, STAGING banner on) and rewrites links to flat relative files.
Nothing is published, nothing is sent; tracking/API calls simply fail silently when opened from disk.
Run:  python tools/export_preview.py <out_dir>"""
import json, os, re, secrets, shutil, sys, tempfile
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl

ROOT = Path(__file__).resolve().parent.parent
out = Path(sys.argv[1]).resolve()
tmp = tempfile.mkdtemp()
os.environ.update({"LEADENGINE_ENV": "production", "LEADENGINE_DB": os.path.join(tmp, "x.db"), "BASE_URL": "http://preview.invalid",
                   "ADMIN_TOKEN": secrets.token_urlsafe(24), "ALLOW_INDEXING": "0", "LEADENGINE_LIVE_DELIVERY": "0", "STAGING": "1",
                   "CONTACT_FORM": "1", "LEGAL_ENTITY": "[por confirmar]", "CONTACT_EMAIL": "contact form", "MAILING_ADDRESS": ""})
sys.path.insert(0, str(ROOT)); os.chdir(ROOT)
import app as A  # noqa: E402
assert A.PRODUCTION and not A._buyers(False), "preview must be built in no-buyer mode"
c = A.app.test_client()

def key(url):
    """canonical (path, relevant-query) -> flat filename"""
    u = urlsplit(url); path = u.path.rstrip("/") or "/"
    q = dict(parse_qsl(u.query))
    if path == "/check":
        a = q.get("a") or ("es_check" if q.get("lang") == "es" else "health_check")
        return f"check-{a}.html"
    if path == "/" : return "index-en.html" if q.get("lang") == "en" else "index.html"
    if path.startswith("/static/"): return None
    return path.strip("/").replace("/", "__") + ".html"

paths = ["/", "/?lang=en", "/learn", "/aprende", "/open-enrollment-2027", "/inscripcion-abierta-2027", "/layoffs", "/privacy", "/privacidad", "/terms", "/terminos", "/optout", "/contact"]
paths += [f"/check?a={a}" for a in A.HEALTH_FLOW["angles"]]
for p in A.CONTENT["pages"]:
    paths.append(("/aprende/" if p["lang"] == "es" else "/learn/") + p["slug"])
paths += [f"/layoffs/{e['slug']}" for e in json.loads((ROOT / "config/layoffs.json").read_text())["events"] if e.get("status") == "ready"]
try: paths.append("/despidos")
except Exception: pass

if out.exists(): shutil.rmtree(out)
(out / "static").mkdir(parents=True)
shutil.copytree(ROOT / "static" / "fonts", out / "static" / "fonts")
for f in ("favicon.svg", "attr.js", "health.js"): shutil.copy(ROOT / "static" / f, out / "static" / f)
_a = (out / "static/attr.js").read_text()
(out / "static/attr.js").write_text("/* static preview: no network calls leave this folder */\nif(location.protocol==='file:'){window.fetch=function(){return Promise.resolve(new Response('{}'))};if(navigator.sendBeacon)navigator.sendBeacon=function(){return true};}\n"+_a)
css = (ROOT / "static/style.css").read_text().replace("url(/static/fonts/", "url(fonts/")
(out / "static" / "style.css").write_text(css)

hjs = (out / "static/health.js").read_text()
hjs = re.sub(r'href="(/(?:aprende|learn|open-enrollment-2027|inscripcion-abierta-2027|privacy|privacidad|terms|terminos|optout)[^"]*)"', lambda m: 'href="' + (key(m.group(1)) or m.group(1)) + '"', hjs)
hjs = hjs.replace('const guide = ({ lost: es_ ? "/aprende/me-despidieron-seguro-medico-florida" : "/learn", plan_change: es_ ? "/aprende/cigna-se-va-de-florida-2027" : "/learn", self_employed: es_ ? "/aprende" : "/learn" })[A.need] || (es_ ? "/inscripcion-abierta-2027" : "/open-enrollment-2027");',
  'const guide = ({ lost: es_ ? "aprende__me-despidieron-seguro-medico-florida.html" : "learn.html", plan_change: es_ ? "aprende__cigna-se-va-de-florida-2027.html" : "learn.html", self_employed: es_ ? "aprende.html" : "learn.html" })[A.need] || (es_ ? "inscripcion-abierta-2027.html" : "open-enrollment-2027.html");')
(out / "static/health.js").write_text(hjs)

missing = set(); written = {}
def rewrite(html):
    def sub(m):
        attr, url = m.group(1), m.group(2)
        if url.startswith(("http", "mailto", "#", "data:", "tel:", "//")): return m.group(0)
        if url.startswith("/static/"): return f'{attr}="{url[1:]}"'
        if url.startswith("/"):
            k = key(url)
            frag = ("#" + url.split("#", 1)[1]) if "#" in url else ""
            if k and k in written_keys: return f'{attr}="{k}{frag}"'
            missing.add(url); return f'{attr}="{(k or url)}{frag}"'
        return m.group(0)
    return re.sub(r'\b(href|src|action)="([^"]*)"', sub, html)

written_keys = {key(p) for p in paths}
for p in paths:
    r = c.get(p)
    if r.status_code != 200: print("SKIP", p, r.status_code); continue
    html = r.get_data(as_text=True)
    html = re.sub(r'<script src="/static/attr.js"></script>', '<script src="static/attr.js"></script>', html)
    html = rewrite(html)
    (out / key(p)).write_text(html); written[key(p)] = p
print("wrote", len(written), "pages;", "links to pages not in preview:", sorted(missing)[:25])
(out / "LEEME.txt").write_text("Vista previa estática de Guía Salud (staging). Abre index.html con doble clic.\nEnglish home: index-en.html. Nada está publicado; el chequeo no recopila datos.\nLos enlaces a páginas fuera de esta vista previa (p. ej. /c/..., /go/...) no funcionan aquí.\n")
