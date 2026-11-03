"""Segmented prospect worklist: every registry row lands in exactly one bucket, segment 1
always carries a named person AND a phone, exclusions always say why, and the written CSVs
add up to the registry. Offline, temp DB only.
"""
import csv
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import prospects as pr            # noqa: E402


def _con():
    return pr.connect(os.path.join(tempfile.mkdtemp(), "prospects.db"))


def _seed(con):
    pr.ingest(con, [
        # owner + direct line, Miami-Dade → priority 1
        {"name": "Alfonso Acupuncture PA", "contact_person": "Justo Ramon Alfonso",
         "phone": "305-224-2772", "city": "Miami", "zip5": "33172", "category": "Acupuncturist"},
        # owner + line shared by 3+ registry rows → priority 4, still segment 1
        {"name": "Bayview Medical Center", "contact_person": "Elena Ruiz",
         "phone": "3055550100", "city": "Miami", "zip5": "33101", "category": "Clinic"},
        {"name": "Bayview Dental Group", "contact_person": "Tom Wu",
         "phone": "3055550100", "city": "Miami", "zip5": "33102", "category": "Dentist"},
        {"name": "Bayview Imaging LLC", "contact_person": "Sara Paz",
         "phone": "3055550100", "city": "Miami", "zip5": "33103", "category": "Radiology"},
        # phone, no owner
        {"name": "Coral Way Electric", "phone": "305-555-0199", "city": "Miami",
         "zip5": "33145", "category": "Electrician"},
        # mailbox only
        {"name": "Gables Accounting", "biz_email": "hello@gables.example",
         "city": "Coral Gables", "zip5": "33134", "category": "Accountant"},
        # registry filing: officer, no channel → excluded incomplete
        {"name": "Pinecrest Holdings", "contact_person": "Mario Diaz", "city": "Pinecrest",
         "zip5": "33156", "category": "Real Estate"},
        # irrelevant POI class (kept in the registry, excluded from the working list)
        {"name": "Iglesia Bethel", "phone": "305-555-0111", "city": "Miami",
         "zip5": "33130", "category": "amenity=place_of_worship"},
    ], "manual")


def test_every_row_lands_in_exactly_one_bucket():
    con = _con()
    _seed(con)
    s = pr.worklist(con)["stats"]
    assert s["reconciles"] is True
    assert sum(s["segments"].values()) + sum(s["exclusions"].values()) == s["examined"]
    assert s["examined"] == 8
    assert s["working_list"] == 6                     # 8 − 1 incomplete − 1 irrelevant


def test_segment_one_always_has_both_a_person_and_a_phone():
    con = _con()
    _seed(con)
    rows = [r for r in pr.worklist(con)["rows"] if r["segment"] == pr.SEG_OWNER]
    assert rows, "expected segment-1 rows"
    for r in rows:
        assert r["contact_person"].strip() and r["public_phone"].strip()
        assert r["audience"] in ("business", "professional", "health_practice")
        assert r["outreach_status"] == "NOT_CONTACTED"
        assert r["verify_state"] == "UNVERIFIED"


def test_phone_evidence_distinguishes_switchboard_from_direct_line():
    con = _con()
    _seed(con)
    rows = {r["name"]: r for r in pr.worklist(con)["rows"]}
    assert rows["Bayview Medical Center"]["phone_evidence"] == "shared"
    assert rows["Bayview Medical Center"]["priority"] == 4
    assert rows["Alfonso Acupuncture PA"]["phone_evidence"] == "direct"
    assert rows["Alfonso Acupuncture PA"]["priority"] == 1
    assert rows["Coral Way Electric"]["segment"] == pr.SEG_PHONE


def test_email_only_and_incomplete_exclusions_are_labelled_not_dropped():
    con = _con()
    _seed(con)
    rows = {r["name"]: r for r in pr.worklist(con)["rows"]}
    assert rows["Gables Accounting"]["segment"] == pr.SEG_EMAIL
    assert rows["Gables Accounting"]["public_phone"] == ""
    assert rows["Pinecrest Holdings"]["segment"] == pr.EX_INCOMPLETE
    # the officer string survives the exclusion — it is evidence, not a fabrication
    assert rows["Pinecrest Holdings"]["contact_person"] == "Mario Diaz"
    assert rows["Iglesia Bethel"]["segment"] == pr.EX_IRRELEVANT


def test_owner_no_channel_is_counted_as_the_join_gap():
    con = _con()
    _seed(con)
    s = pr.worklist(con)["stats"]
    assert s["owner_no_channel"] == 1
    assert s["owner_no_channel_by_source"].get("manual") == 1


def _export_rows(con):
    """The rows an operator imports: exactly the segment-1..3 projection, as CSV-shaped dicts."""
    return [{"record_id": str(r["id"]), "business_name": r["name"]}
            for r in pr.worklist(con)["rows"] if r["segment"] in pr.SEGMENTS]


def test_worklist_import_persists_classification_without_touching_provenance():
    con = _con()
    _seed(con)
    before = {r["id"]: dict(r) for r in con.execute("SELECT * FROM prospects")}
    rep = pr.import_worklist(con, _export_rows(con))
    assert rep["attempted"] == 6 and rep["imported"] == 6 and rep["updated"] == 6
    assert rep["rejected"] == {"unknown_record_id": 0, "name_mismatch": 0, "invalid_row": 0}
    after = {r["id"]: dict(r) for r in con.execute("SELECT * FROM prospects")}
    on_list = {int(r["record_id"]) for r in _export_rows(con)}
    for rid, row in after.items():
        if rid not in on_list:                       # excluded rows are left untouched
            assert row["segment"] == "" and row["worklist_synced_at"] == ""
        else:
            assert row["segment"] in pr.SEGMENTS
            assert row["priority"] in (1, 2, 3, 4, 5, 6)
            assert row["outreach_status"] == "NOT_CONTACTED"
            assert row["worklist_synced_at"]
        # provenance / contact / operator fields are byte-identical to before the import
        for k in ("name", "public_phone", "biz_email", "website", "address", "city", "zip5",
                  "category", "source", "evidence_url", "created_at", "updated_at", "notes",
                  "decision", "verify_state", "prospect_key"):
            assert row[k] == before[rid][k], f"import changed {k} on #{rid}"


def test_worklist_import_is_idempotent():
    con = _con()
    _seed(con)
    pr.import_worklist(con, _export_rows(con))
    stamp = {r[0]: r[1] for r in con.execute("SELECT id, worklist_synced_at FROM prospects")}
    rep = pr.import_worklist(con, _export_rows(con))
    assert rep["updated"] == 0 and rep["unchanged"] == 6
    assert rep["already_on_list"] == 6
    assert {r[0]: r[1] for r in con.execute("SELECT id, worklist_synced_at FROM prospects")} == stamp


def test_worklist_import_rejects_unknown_id_and_name_mismatch():
    con = _con()
    _seed(con)
    rows = _export_rows(con)
    rows.append({"record_id": "999999", "business_name": "Ghost LLC"})
    rows.append({"record_id": rows[0]["record_id"], "business_name": "A Completely Different Business"})
    rows.append({"record_id": "not-a-number", "business_name": "Broken Row"})
    rep = pr.import_worklist(con, rows)
    assert rep["rejected"] == {"unknown_record_id": 1, "name_mismatch": 1, "invalid_row": 1}
    assert con.execute("SELECT COUNT(*) FROM prospects").fetchone()[0] == 8   # nothing added


def test_worklist_import_never_resets_a_recorded_outreach_status():
    con = _con()
    _seed(con)
    pid = con.execute("SELECT id FROM prospects ORDER BY id LIMIT 1").fetchone()[0]
    con.execute("UPDATE prospects SET outreach_status='ATTEMPTED' WHERE id=?", (pid,))
    con.commit()
    pr.import_worklist(con, _export_rows(con))
    assert con.execute("SELECT outreach_status FROM prospects WHERE id=?", (pid,)).fetchone()[0] == "ATTEMPTED"


def test_export_files_reconcile_with_the_registry(tmp_path):
    con = _con()
    _seed(con)
    res = pr.export_worklist(con, tmp_path)
    written, stats = res["written"], res["stats"]
    assert sum(written.values()) == stats["examined"]
    assert sum(written.values()) == con.execute("SELECT COUNT(*) FROM prospects").fetchone()[0]

    ids = []
    for fname, n in written.items():
        with (tmp_path / fname).open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == n, fname
        for r in rows:
            ids.append(r["record_id"])
            assert r["business_name"].strip(), fname
            assert r["source"].strip() and r["outreach_status"] == "NOT_CONTACTED"
    assert len(set(ids)) == len(ids), "a record id appears in two files — double counted"

    seg1 = list(csv.DictReader((tmp_path / "01_owner_named_phone_businesses.csv").open(encoding="utf-8")))
    assert seg1 and all(r["decision_maker"].strip() and r["professional_phone"].strip() for r in seg1)
    assert seg1[0]["priority"] in ("1", "2", "3", "4")

    saved = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert saved["examined"] == stats["examined"] and saved["reconciles"] is True
    md = (tmp_path / "SUMMARY.md").read_text(encoding="utf-8")
    assert "NOT_CONTACTED" in md and "UNVERIFIED" in md
    assert "biggest obstacle" in md
