"""Smoke every GET route in TEST mode and report any 500 / template errors.

Not a feature test: it just catches undefined template variables, broken filters,
import errors and route config mistakes. Run from the project root:

    python tests/smoke_routes.py
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
os.chdir(HERE)
sys.path.insert(0, str(HERE))

os.environ["ADMIN_TOKEN"] = "smoke-admin-token-0123456789"
os.environ.setdefault("LEADENGINE_DB", str(HERE / "data" / "smoke_leads.db"))
os.environ.setdefault("LEADENGINE_OUTBOX", str(HERE / "outbox_smoke"))
os.environ.setdefault("LEADENGINE_CFG", str(HERE / "config"))
os.environ.setdefault("LEADENGINE_LIVE_DELIVERY", "0")

import importlib  # noqa: E402

appmod = importlib.import_module("app")
app = appmod.app
client = app.test_client()

TOKEN = os.environ["ADMIN_TOKEN"]

# A demo buyer's portal token (demo buyers are safe: dry-run delivery, no real data)
buyers = appmod.route.load_buyers(appmod.CFG)
buyer = next(b for b in buyers if b.get("is_demo"))
BUY_TOKEN = buyer["portal_token"]

GETS = [
    # public surface (EN + ES)
    "/", "/check", "/check?a=savings_es", "/aprende", "/aprende/que-es-un-deducible",
    "/learn/", "/learn/what-is-a-deductible", "/privacy", "/terms", "/privacidad",
    "/terminos", "/optout", "/contact", "/for-agents", "/healthz", "/robots.txt",
    "/sitemap.xml", "/open-enrollment-2027", "/inscripcion-abierta-2027",
    "/layoffs", "/despidos", "/qa",
    # buyer portal (demo buyer)
    f"/agent/{BUY_TOKEN}", f"/agent/{BUY_TOKEN}/billing",
    f"/agent/{BUY_TOKEN}/billing/statement.csv",
    f"/agent/{BUY_TOKEN}/leads",
    f"/agent/{BUY_TOKEN}/leads.csv?days=0", f"/agent/{BUY_TOKEN}/markets",
    f"/agent/{BUY_TOKEN}/outcomes",
    f"/agent/{BUY_TOKEN}/outcomes.csv?week=4", f"/agent/{BUY_TOKEN}/outcomes.csv?week=0&q=x",
    f"/agent/{BUY_TOKEN}/leads?q=x&days=30&sort=name&dir=asc",
    f"/agent/{BUY_TOKEN}/outcomes?week=0",
    # admin surface
    "/admin", "/admin/leads/new?token=" + TOKEN, "/admin/intel", "/admin/channels", "/admin/funnel", "/admin/lab",
    "/admin/organic", "/admin/organic.csv", "/admin/opportunities",
    "/admin/audiences", "/admin/quality", "/admin/quality?dim=campaign",
    "/admin/quality?dim=language", "/admin/partners", "/admin/content",
    "/admin/intel/briefs", "/admin/export.csv", "/admin/consents.csv",
    "/admin/lab.csv", "/admin/credits", "/admin/pipeline", f"/admin?token={TOKEN}", f"/admin/intel?token={TOKEN}",
    f"/admin/channels?token={TOKEN}", f"/admin/funnel?token={TOKEN}",
    f"/admin/lab?token={TOKEN}", f"/admin/organic?token={TOKEN}",
    f"/admin/opportunities?token={TOKEN}", f"/admin/audiences?token={TOKEN}",
    f"/admin/quality?token={TOKEN}", f"/admin/quality?token={TOKEN}&dim=source&test=test",
    f"/admin/partners?token={TOKEN}", f"/admin/content?token={TOKEN}",
    f"/admin/credits?token={TOKEN}", f"/admin/pipeline?token={TOKEN}",
    f"/admin/intel/briefs?token={TOKEN}",    f"/admin/export.csv?token={TOKEN}", f"/admin/consents.csv?token={TOKEN}", f"/admin/lab.csv?token={TOKEN}",
    f"/admin/readiness?token={TOKEN}",
    f"/admin/integrations?token={TOKEN}", f"/admin/intel/signals.csv?token={TOKEN}",
    f"/admin/intel/signals?token={TOKEN}&source=reddit&lang=&topic=&min_intent=&days=",
    f"/admin/intel/signals?token={TOKEN}", f"/admin/intel/signals?token={TOKEN}&status=ACTIONED",
    f"/admin/intel/supply?token={TOKEN}",
    f"/admin/prospects?token={TOKEN}", f"/admin/prospects?token={TOKEN}&decision=NEW&verify_state=UNVERIFIED",
    f"/admin/prospects.csv?token={TOKEN}",
    f"/admin/economics?token={TOKEN}", f"/admin/economics?token={TOKEN}&leads=5&price=60&return_pct=20",
    f"/admin/buyer-pipeline?token={TOKEN}", f"/admin/buyer-pipeline.csv?token={TOKEN}",
]
# experiment + lead pages if any exist
exp = appmod.con.execute("SELECT experiment_id FROM experiments LIMIT 1").fetchone()
if exp:
    GETS += [f"/admin/lab/{exp[0]}?token={TOKEN}", f"/admin/lab/{exp[0]}"]
lead = appmod.con.execute("SELECT lead_id FROM leads LIMIT 1").fetchone()
if lead:
    GETS += [f"/admin/lead/{lead[0]}?token={TOKEN}", f"/agent/{BUY_TOKEN}/lead/{lead[0]}",
             f"/agent/{BUY_TOKEN}/outcomes?q={lead[0]}"]
# sandbox TEST checkout confirmation page (smoke DB only; no money, TEST lane)
_sandbox_pkg = appmod.MARKETPLACE.get("packages", [{}])[0]
_sandbox_row = appmod.payments.create_sandbox_checkout(appmod.con, buyer, _sandbox_pkg)
GETS += [f"/agent/{BUY_TOKEN}/billing/sandbox/{_sandbox_row}",
         f"/agent/{BUY_TOKEN}/billing/sandbox/999999"]   # unknown row renders a clean 404
partner = appmod.con.execute("SELECT partner_id FROM partners LIMIT 1").fetchone()
if partner:
    GETS += [f"/admin/pipeline/{partner[0]}?token={TOKEN}"]
# prospect-buyer detail pages if any non-demo buyer is configured (covers the pipeline columns)
for _b in buyers:
    if not _b.get("is_demo"):
        GETS += [f"/admin/buyer-pipeline/{_b['id']}?token={TOKEN}"]

bad = []
for path in GETS:
    try:
        r = client.get(path)
        if r.status_code >= 500:
            bad.append((path, r.status_code, r.get_data(as_text=True)[:400]))
    except Exception as exc:  # noqa: BLE001
        bad.append((path, "EXC", f"{type(exc).__name__}: {exc}"))

print(f"{len(GETS) - len(bad)}/{len(GETS)} OK")
for path, status, detail in bad:
    print(f"---- {path} -> {status}\n{detail}\n")
sys.exit(1 if bad else 0)
