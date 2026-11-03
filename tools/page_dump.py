"""DEV TOOL: run the pilot rehearsal inside an isolated temp app, then dump the real
buyer/admin/consumer pages it produced for human gap review. Nothing is sent, spent,
activated or deployed; the temp tree is removed on exit. NOT part of the test suite.

  python tools/page_dump.py out/page_dump
"""
import json, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STEPS = [  # (get_path, publish)
    ("/agent/{tok}", "agent_desk.html"),
    ("/agent/{tok}/billing", "agent_billing.html"),
    ("/agent/{tok}/leads", "agent_leads.html"),
    ("/agent/{tok}/outcomes", "agent_outcomes.html"),
    ("/agent/{tok}/lead/FIRST", "agent_lead.html"),
    ("/admin?token=t", "admin.html"),
    ("/admin/lead/FIRST?token=t", "admin_lead.html"),
    ("/admin/credits?token=t", "admin_credits.html"),
    ("/admin/funnel?token=t", "admin_funnel.html"),
    ("/admin/pipeline?token=t", "admin_pipeline.html"),
    ("/admin/export.csv?token=t", "admin_export.csv"),
    ("/admin/consents.csv?token=t", "admin_consents.csv"),
    ("/admin/quality?token=t", "admin_quality.html"),
    ("/admin/readiness?token=t", "admin_readiness.html"),
    ("/admin/buyers?token=t", "admin_buyers.html"),
    ("/open-enrollment-2027", "open-enrollment-2027.html"),
    ("/aprende", "aprende.html"),
    ("/check?a=es_check", "es_check.html"),
    ("/learn", "learn.html"),
]


def dump(outdir: str) -> int:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="page_dump_"))
    os.environ.update({"LEADENGINE_DB": str(tmp / "dump.db"), "ADMIN_TOKEN": "t",
                       "INTEL_DB": str(tmp / "none.db"), "LEADENGINE_OUTBOX": str(tmp / "outbox"),
                       "LEADENGINE_LIVE_DELIVERY": "0"})
    for k in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"):
        os.environ.pop(k, None)
    import importlib
    import app as appmod
    importlib.reload(appmod)
    import tools.pilot_rehearsal as pr
    importlib.reload(pr)
    # point the rehearsal machinery at the same temp cfg/DB so pages render REAL rehearsed state
    import shutil, json
    d = tmp / "cfg"
    shutil.copytree(appmod.CFG, d)
    buyers = json.loads((d / "buyers.json").read_text(encoding="utf-8"))
    for b in buyers:
        if b.get("is_demo"):
            b["active"] = False
    (d / "buyers.json").write_text(json.dumps(buyers, indent=1), encoding="utf-8")
    appmod.CFG = d
    appmod.OUTBOX = tmp / "outbox"
    c = appmod.app.test_client()
    tok = pr._rehearse(c, appmod, __import__("engine.billing", fromlist=["billing"]), __import__("engine.route", fromlist=["route"]))
    first_lead = appmod.con.execute("SELECT lead_id FROM leads ORDER BY created_at LIMIT 1").fetchone()
    for path, name in STEPS:
        r = c.get(path.replace("{tok}", tok).replace("FIRST", first_lead["lead_id"] if first_lead else "none"))
        (out / name).write_bytes(r.data)
    shutil.rmtree(tmp, ignore_errors=True)
    print("dumped", len(STEPS), "pages to", out.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(dump(sys.argv[1] if len(sys.argv) > 1 else "out/page_dump"))
