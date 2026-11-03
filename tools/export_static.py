"""Export the no-buyer public funnel as static pages + data for the Cloudflare Worker host (Higgsfield website).

Renders every public page with the real Flask app (production, no-buyer mode) so the hosted copy is byte-for-byte the
tested site, then writes:
  <out>/worker/pages.generated.ts   PAGES{key: html}, pre-rendered pages (keys: path, '/check|<angle>', OEP states)
  <out>/worker/data.generated.ts    LINKS (tracking redirects), CAMPAIGNS, ANGLES, PAGE_EXPERIMENTS, FACTS, texts
  <out>/public/static/*             css/js assets
Run:  python tools/export_static.py <out_dir> <base_url> [index=0|1]
Nothing here contacts anyone; it only renders local pages.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
out = Path(sys.argv[1]).resolve()
base = sys.argv[2].rstrip("/")
index = (sys.argv[3] if len(sys.argv) > 3 else "0") == "1"

tmp = tempfile.mkdtemp()
os.environ.update({
    "LEADENGINE_ENV": "production", "LEADENGINE_DB": os.path.join(tmp, "x.db"), "BASE_URL": base,
    "ADMIN_TOKEN": secrets.token_urlsafe(24), "ALLOW_INDEXING": "1" if index else "0", "LEADENGINE_LIVE_DELIVERY": "0",
    "BRAND": os.environ.get("BRAND", "Guía Salud"), "LEGAL_ENTITY": os.environ.get("LEGAL_ENTITY", "[por confirmar]"),
    "CONTACT_EMAIL": "contact form", "MAILING_ADDRESS": os.environ.get("MAILING_ADDRESS", ""), "CONTACT_FORM": "1",
})
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
import app as A  # noqa: E402

c = A.app.test_client()
pages: dict[str, str] = {}


def get(path, key=None, headers=None, expect=200):
    r = c.get(path, headers=headers or {})
    assert r.status_code == expect, (path, r.status_code)
    pages[key or path.split("?")[0]] = r.get_data(as_text=True)
    return r


assert A.PRODUCTION and not A._buyers(False), "must export in production, no-buyer mode"

get("/")
get("/learn"); get("/aprende")
life = re.compile(r"life|vida")
slugs = []
for p in A.CONTENT["pages"]:
    if life.search(p["slug"]):
        continue
    pre = "/aprende/" if p["lang"] == "es" else "/learn/"
    get(pre + p["slug"]); slugs.append(pre + p["slug"])
for a in A.HEALTH_FLOW["angles"]:
    get(f"/check?a={a}", f"/check|{a}")
get("/check", "/check|health_check")
for pth in ("/privacy", "/privacidad", "/terms", "/terminos", "/contact"):
    get(pth)
get("/optout")
pages["/optout|done"] = c.post("/optout", data={"email": "none@example.invalid"}).get_data(as_text=True)
pages["/contact|done"] = c.post("/contact", data={"message": "", "website": "x"}).get_data(as_text=True)
pages["/qa|on"] = c.get("/qa?on=1").get_data(as_text=True)
pages["/qa|off"] = c.get("/qa?on=0").get_data(as_text=True)

# Date-dependent Open Enrollment status page: 4 states, numbers/dates substituted at request time by the Worker.
F = A.HEALTH_FLOW["facts"]
probe = {"pre": "2026-10-09", "open": "2026-11-02", "grace": "2027-01-02", "over": "2027-02-01"}
meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
from datetime import date  # noqa: E402
for pth, es in (("/open-enrollment-2027", False), ("/inscripcion-abierta-2027", True)):
    for st, d in probe.items():
        t = date.fromisoformat(d)
        html = c.get(f"{pth}?test=1&today={d}").get_data(as_text=True)
        pretty = f"{t.day} de {meses[t.month - 1]} de {t.year}" if es else t.strftime("%B %-d, %Y")
        assert pretty in html and d in html, (pth, st)
        html = html.replace(pretty, "__PRETTY__").replace(d, "__ISO__")
        html = re.sub(r"<b>\d+</b>( (?:days|días))", r"<b>__DAYS__</b>\1", html)
        pages[f"{pth}|{st}"] = html

# 404
pages["404"] = c.get("/__nope__").get_data(as_text=True)

# Tracking redirects: export the links table exactly as the app loaded it.
links = {}
for r in A.con.execute("SELECT * FROM links"):
    r = dict(r)
    links[r["code"]] = {k: r.get(k) for k in ("campaign", "utm_source", "utm_medium", "utm_campaign", "utm_content", "partner_id",
                                              "experiment_id", "angle", "lang", "landing_page", "phone_variant")}
campaigns = {cc["slug"]: {"id": cc["id"], "lang": cc.get("lang", "en"), "product": cc["product"]} for cc in A.CAMPAIGNS.values()
             if cc["product"] == "health_aca"}
camp_by_id = {cc["id"]: {"lang": cc.get("lang", "en"), "product": cc["product"]} for cc in A.CAMPAIGNS.values()}
page_exps = []
for r in A.con.execute("SELECT experiment_id, pages FROM experiments WHERE pages IS NOT NULL AND pages != '' AND status NOT IN ('KILLED')"):
    for pat in (x.strip() for x in r["pages"].split(",")):
        if pat:
            page_exps.append([pat, r["experiment_id"]])
data = {
    "BASE": base, "INDEX": index, "LINKS": links, "CAMPAIGNS": campaigns, "CAMPAIGN_BY_ID": camp_by_id,
    "ANGLES": {k: v["lang"] for k, v in A.HEALTH_FLOW["angles"].items()}, "PAGE_EXPERIMENTS": page_exps,
    "FACTS": {"oep_start": F["oep_start"], "dec15": F["dec15"], "oep_end": F["oep_end"]},
    "EMAIL_ONLY": {"en": A._email_text("en"), "es": A._email_text("es")},
    "SITEMAP": sorted(set(["/", "/learn", "/aprende", "/check", "/open-enrollment-2027", "/inscripcion-abierta-2027", "/privacy", "/privacidad",
                            "/terms", "/terminos", "/contact"] + slugs)),
}
(out / "worker").mkdir(parents=True, exist_ok=True)
(out / "worker" / "pages.generated.ts").write_text(
    "// GENERATED by tools/export_static.py - do not edit\nexport const PAGES: Record<string, string> = " + json.dumps(pages, ensure_ascii=False) + ";\n")
(out / "worker" / "data.generated.ts").write_text(
    "// GENERATED by tools/export_static.py - do not edit\nexport const DATA = " + json.dumps(data, ensure_ascii=False) + " as const;\n")
st = out / "public" / "static"
st.mkdir(parents=True, exist_ok=True)
for f in (ROOT / "static").iterdir():
    shutil.copy(f, st / f.name)
print("pages", len(pages), "bytes", sum(len(v) for v in pages.values()), "links", len(links), "slugs", len(slugs))
shutil.rmtree(tmp, ignore_errors=True)
