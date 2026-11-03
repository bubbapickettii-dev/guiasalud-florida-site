"""Demo-account funding: give every demo (TEST) buyer an explicit, clearly-labeled
setup credit so a fresh demo desk never reads as an overdrafted new buyer.

Why this exists: demo buyers carry only TEST rehearsal charges (LEAD_CHARGE rows,
is_test=1), so an unfunded demo desk shows \"overdrawn by $120\" — an accidental
artefact of past test runs, presented like a real account state as if a new buyer
started with negative credits. This tool fixes presentation WITHOUT touching
ledger history: it only appends one new TEST-mode ledger row per demo buyer
(the same append-only `billing.apply_adjustment` the operator Adjust action uses),
so charge-once/credit-once/ledger integrity are untouched. It is IDEMPOTENT:
a buyer whose ledger already carries the setup-credit note is never credited twice.
Real buyer records are never modified (demo buyers only, TEST rows only).

  python tools/seed_demo_credits.py             # use LEADENGINE_DB / config, default $300
  DEMO_SETUP_CREDITS_USD=600 python tools/seed_demo_credits.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

NOTE = "demo setup credits — seeded TEST data, never real payments"


def main() -> int:
    from engine import billing  # noqa: E402
    import sqlite3  # noqa: E402

    db = os.environ.get("LEADENGINE_DB") or str(ROOT / "data" / "leads.db")
    cfg = Path(os.environ.get("LEADENGINE_CFG") or (ROOT / "config"))
    buyers = None
    for name in ("buyers.json",):
        p = cfg / name
        if p.exists():
            try:
                buyers = __import__("json").loads(p.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                print(f"cannot read {p}: {exc}")
                return 1
    if buyers is None:
        print("no buyers.json in config; nothing to do")
        return 0
    try:
        amount = round(float(os.environ.get("DEMO_SETUP_CREDITS_USD") or 300), 2)
    except ValueError:
        print("DEMO_SETUP_CREDITS_USD must be a number")
        return 1
    if amount <= 0:
        print("setup credit must be positive")
        return 1

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    demo = [b for b in buyers if isinstance(b, dict) and b.get("is_demo")]
    if not demo:
        print("no demo buyers configured; nothing to do")
        return 0
    done = skipped = 0
    for b in demo:
        bid = b.get("id")
        if not bid:
            continue
        existing = con.execute("SELECT rowid FROM buyer_ledger WHERE buyer_id=? AND note LIKE ?",
                               (bid, "demo setup credits%")).fetchone()
        if existing:
            skipped += 1
            continue
        billing.apply_adjustment(con, bid, amount, NOTE, is_test=1)
        done += 1
        bal = billing.balance(con, bid, 1)
        print(f"  {bid}: +${amount:.2f} TEST setup credit -> TEST balance ${bal:.2f}")
    con.commit()
    con.close()
    print(f"done: {done} buyer(s) credited, {skipped} already funded (idempotent)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
