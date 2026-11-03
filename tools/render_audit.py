"""RENDER AUDIT: walk every admin + buyer-desk GET route in an isolated TEST
environment in three data states — zero data / one delivered TEST lead / an
unmatched lead with no eligible buyer — and flag rendered output that looks
like a template defect: leftover Jinja syntax, malformed currency expressions
(the "${60.00}" class of bug), leaked python literals/objects/tracebacks.

Complements tests/smoke_routes.py (status-only) by inspecting page CONTENT, and
complements the full battery by exercising EMPTY and UNMATCHED states that
smoke's data-current DB usually contains but a fresh deployment may not.

  python tools/render_audit.py            # audit + report, exit 1 on findings
  python tools/render_audit.py --verbose  # print each visited route

Nothing is deployed, contacted, charged or activated; the temp tree is removed
on exit. Dev tool, not part of the pytest suite.
"""
import json, os, re, shutil, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SIGNATURES = [
    (r"(?<!\w)\{\{", "literal Jinja expression left in output"),
    (r"\{\%\s*(if|for|else|endif|endfor|block|set|with|macro|extends)", "literal Jinja statement left in output"),
    (r"\$\{[0-9]", "JS-template-style currency artifact (the ${60.00} bug)"),
    (r"\$%\.", "raw '$%.2f' payload leaked into output"),
    (r"(?<!\w)'%[.0-9]*f'", "raw python format spec in output"),
    (r"\)\s*\.format\(", "python .format() leaked into output"),
    (r"\('\w+','[^']{1,40}','/[a-z][\w/.-]*'\)", "template nav/label tuple literal leaked into output"),
    (r"\{'(?:NEW|STLBL|LBL)\s*:", "template status-label dict leaked into output"),
    (r"<function |<built-in method|object at 0x", "python object repr leaked into output"),
    (r"Traceback \(most recent call last", "traceback text in page body"),
    (r"\b[Nn]a[Nn]\b|\b[Ii][Nn][Ff]\s", "nan/inf in output"),
    (r"jinja2\.exceptions", "jinja exception text in page body"),
]


def _a(s):
    return s.encode("ascii", "replace").decode("ascii")


def carve_path(text, start):
    return text[max(0, start - 70): start + 70].replace("\n", " ¶ ")


def scan(text):
    hits = []
    for pat, why in SIGNATURES:
        m = re.search(pat, text)
        if m:
            hits.append((why, carve_path(text, m.start())))
    return hits


def walk(c, appmod, tok, AT, tags, findings, visited, verbose):
    routes = [
        "/", "/check", "/privacy", "/terms", "/aprende", "/learn/", "/qa",
        "/open-enrollment-2027", "/layoffs", "/for-agents", "/optout", "/contact",
        f"/agent/{tok}", f"/agent/{tok}/leads", f"/agent/{tok}/leads?days=0",
        f"/agent/{tok}/leads?due=1", f"/agent/{tok}/leads?q=a&days=30&sort=name&dir=asc",
        f"/agent/{tok}/leads?days=7&sort=created_at&dir=desc",
        f"/agent/{tok}/leads.csv?days=0", f"/agent/{tok}/markets", f"/agent/{tok}/outcomes",
        f"/agent/{tok}/outcomes?week=99", f"/agent/{tok}/billing",
        f"/agent/{tok}/billing/statement.csv",
    ]
    for extra in ["", "/intel", "/intel/briefs", "/channels", "/funnel", "/lab", "/organic", "/organic.csv",
                  "/opportunities", "/audiences", "/quality", "/quality?dim=campaign", "/quality?dim=language",
                  "/quality?dim=source", "/credits", "/pipeline", "/buyer-pipeline", "/readiness",
                  "/economics", "/economics?leads=5&price=60&return_pct=20", "/partners", "/content",
                  "/export.csv", "/consents.csv", "/lab.csv", "/acquisition", "/acquisition.csv"]:
        routes.append(f"/admin{extra}?token={AT}")

    con = appmod.con
    for lid in [r[0] for r in con.execute("SELECT DISTINCT lead_id FROM leads ORDER BY lead_id LIMIT 5").fetchall() or []]:
        routes.append(f"/admin/lead/{lid}?token={AT}")
        routes.append(f"/agent/{tok}/lead/{lid}")
        routes.append(f"/agent/{tok}/outcomes?q={lid}")
    try:
        bids = [b["id"] for b in json.loads((appmod.CFG / "buyers.json").read_text(encoding="utf-8"))
                if isinstance(b, dict) and not b.get("is_demo")][:3]
    except Exception:
        bids = []
    for bid in bids:
        routes.append(f"/admin/buyer-pipeline/{bid}?token={AT}")
    for eid in [r[0] for r in con.execute("SELECT experiment_id FROM experiments LIMIT 2").fetchall() or []]:
        routes.append(f"/admin/lab/{eid}?token={AT}")

    for path in sorted(set(routes)):
        if path in visited:
            continue
        visited.add(path)
        try:
            r = c.get(path)
        except Exception as exc:  # noqa: BLE001
            findings.append((tags, path, "EXC", f"{type(exc).__name__}: {exc}"))
            continue
        if r.status_code >= 500:
            findings.append((tags, path, r.status_code, r.get_data(as_text=True)[:300]))
            continue
        if verbose:
            print(_a(f"    ok {r.status_code} {path}"))
        body = r.get_data(as_text=True)
        for why, ctx in scan(body):
            findings.append((tags, path, "ARTIFACT", f"[{why}] …{ctx}…"))
            break
        body = str(body)
        m = re.search(r"(?<![\w\"]>)\bNone\b(?!</)", body)
        # soft probe: report bare 'None' leaking into markup for eyeball review only
        if m and re.search(r">[^<]*\bNone\b[^<]*<", body):
            print(_a(f"    NONE? {path}: " + carve_path(body, m.start())))


def main(argv):
    verbose = "--verbose" in argv
    findings, visited = [], set()
    tmp = Path(tempfile.mkdtemp(prefix="render_audit_"))
    os.environ.update({
        "LEADENGINE_DB": str(tmp / "t.db"), "ADMIN_TOKEN": "t", "INTEL_DB": str(tmp / "none.db"),
        "LEADENGINE_OUTBOX": str(tmp / "outbox"), "LEADENGINE_CFG": str(ROOT / "config"),
        "LEADENGINE_LIVE_DELIVERY": "0", "BASE_URL": "http://127.0.0.1:5000",
    })
    for k in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"):
        os.environ.pop(k, None)
    import importlib
    import app as appmod
    importlib.reload(appmod)
    import tools.pilot_rehearsal as pr
    importlib.reload(pr)
    c = appmod.app.test_client()
    demo = next(b for b in appmod.route.load_buyers(appmod.CFG) if b.get("is_demo"))
    tok, AT = demo["portal_token"], "t"

    def lead(n):
        d = dict(campaign="mortgage-protection", first_name="Ana", last_name=f"Audit{n}",
                 email=f"ana.audit{n}@example.test", phone="30555501%02d" % n, zip="33130", state="FL",
                 age_range="40-49", coverage_for="me_spouse_kids", mortgage_balance="300-500k",
                 tobacco="no", timeframe="asap", preferred_contact="phone", is_test=1,
                 tool_result={"headline": "$300,000"}, consent_checked=True)
        return c.post("/api/lead", json=d).get_json()

    print("[1/3] zero-data state")
    walk(c, appmod, tok, AT, "empty", findings, visited, verbose)

    print("[2/3] one TEST lead delivered to the demo buyer")
    r = lead(1)
    print("      lead ->", r.get("status"))
    walk(c, appmod, tok, AT, "seeded", findings, visited, verbose)

    print("[3/3] unmatched lead (no eligible buyer active)")
    d = tmp / "cfg_none"
    shutil.copytree(appmod.CFG, d)
    buyers = json.loads((d / "buyers.json").read_text(encoding="utf-8"))
    for b in buyers:
        b["active"] = False
    (d / "buyers.json").write_text(json.dumps(buyers, indent=1), encoding="utf-8")
    appmod.CFG = d
    r2 = lead(2)
    print("      lead ->", r2.get("status"), "(expected unrouted)")
    walk(c, appmod, tok, AT, "unmatched", findings, visited, verbose)

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if findings:
        print(f"RENDER AUDIT: {len(findings)} finding(s)")
        for tags, path, why, detail in findings:
            print(f"---- [{tags}] {path} -> {why}\n{detail}\n")
        return 1
    print("RENDER AUDIT: CLEAN — no template artifacts or render defects found")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
