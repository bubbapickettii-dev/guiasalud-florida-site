"""Consent-snapshot encoding regression (the Windows cp1252 HTTP 500).

The consumer journey submits a form_snapshot = the live #app DOM's outerHTML. The rendered
Back button and Spanish copy put genuinely non-cp1252 characters (the left arrow U+2190,
accents like á/é) into the browser page, so the POST body carries them. On Windows the
platform default codec is cp1252, which cannot carry U+2190: an unencoded text write there
used to raise UnicodeEncodeError, returning HTTP 500 on the most important request in the
product. The app must (a) accept the submission unchanged — special glyphs stay in the
page, they are not removed to hide the error — and (b) store the snapshot as exact UTF-8
bytes so the audit copy reads back identically. The brand name rendered into the consent copy
comes from config/site.json, so the stored consent row must round-trip it exactly.
Bookkeeping only: TEST-flagged lead, temp DB, no live delivery.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from test_billing import make_app, post

BRAND = json.loads((ROOT / "config" / "site.json").read_text(encoding="utf-8"))["brand"]

# what the browser actually sends: the DOM as a browser sees it, incl. glyphs cp1252 cannot carry
SNAPSHOT = (
    '<button class="back" type="button" id="back">\u2190 Atrás</button>'
    '<p>Seguro médico — cobertura 2027 · resumen de opciones \u2190</p>'
)

def test_submission_with_non_cp1252_glyphs_succeeds(tmp_path):
    m = make_app(tmp_path); c = m.app.test_client()
    r = post(c, consent_checked=True, form_snapshot=SNAPSHOT)
    assert r.status_code == 200, r.get_data(as_text=True)[:400]
    lead_id = r.get_json()["lead_id"]
    snap_file = m.SNAP / f"{lead_id}.html"
    assert snap_file.exists()
    raw = snap_file.read_bytes()
    assert b"\xe2\x86\x90" in raw and b"Atr\xc3\xa1s" in raw    # UTF-8 bytes, never a cp1252 variant
    # the stored file decodes back to the exact submitted text — accents and arrow intact
    assert raw.decode("utf-8") == SNAPSHOT
    # the consent copy in the audit row keeps its wording, incl. the brand name it was rendered with
    row = m.con.execute("SELECT consent_text FROM consents WHERE lead_id=? AND consent_text LIKE ?",
                        (lead_id, f"%{BRAND}%")).fetchone()
    assert row, "consent copy must keep its real text (no glyph stripping, no mojibake)"
