"""Backup & restore procedure (the session's rehearsal codified as regression tests).

  - backup_db.py hot-copies the LIVE database (LEADENGINE_DB explicit), keeps the newest file in
    the requested destination, and refuses to "back up" a nonexistent source
  - `--restore` codifies the documented restore as a command: it writes the chosen backup to a
    named target, verifies PRAGMA integrity_check on the copy, refuses a MISSING backup file,
    and never overwrites an existing target without --force
  - a run whose environment LOST LEADENGINE_DB resolves to the default local path WITH a loud
    warning — a lost env in a cron must never look like a real production backup (signaled via
    resolve_source(); a subprocess run additionally prints it on stderr)
  - the documented restore (copy backup over a FRESH db path) yields a database that passes
    PRAGMA integrity_check, keeps every sentinel row, keeps the Unicode consent snapshot (the
    arrow character Windows cp1252 once mangled), and opens exactly like production through
    engine.db.connect (WAL mode, the same connection path the app uses)

No production files are touched: every artifact lives in tmp_path (disposable copies only).
"""
import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def _load_backup_mod():
    """Import backup_db.py by path (module name has an underscore, import-safe, and importing
    must NOT run a backup: the script's work lives under main()/__main__)."""
    spec = importlib.util.spec_from_file_location("backup_db", ROOT / "backup_db.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_db(path: Path):
    from engine.db import connect  # the app's own schema + pragmas path
    con = connect(path)
    con.execute("INSERT INTO leads (lead_id, created_at, is_test, campaign, phone, phone_source, phone_valid, "
                "lead_type, contact_scope) VALUES ('BAK-1','2026-10-24T09:00:00',0,'health_aca','3055550111',"
                "'CONSUMER_SUBMITTED',1,'PHONE_LEAD','PHONE_AND_EMAIL')")
    con.execute("INSERT INTO consents (lead_id, ts, consent_version, consent_text, ip, checkbox_checked, scope) "
                "VALUES ('BAK-1','2026-10-24T09:00:05','v5','SPECIAL ARROW → consent text','192.0.2.9',1,'PHONE_AND_EMAIL')")
    con.execute("INSERT INTO buyer_ledger (ts,buyer_id,lead_id,package_id,kind,amount,balance_after,note,is_test) "
                "VALUES ('2026-10-24T09:01:00','BAK-BUYER','BAK-1','','LEAD_CHARGE',-25.0,-25.0,'rehearsal',0)")
    con.commit()
    con.close()


def test_backup_keeps_every_row_unicode_consent_and_restores(tmp_path):
    _build_db(tmp_path / "db" / "leads.db")
    mod = _load_backup_mod()
    out = mod.make_backup(tmp_path / "db" / "leads.db", tmp_path / "backups")
    baks = sorted((tmp_path / "backups").glob("leads_*.db"))
    assert baks == [out]

    # documented restore: stop app -> replace DB file -> start; verified on a disposable copy
    restored = tmp_path / "restored.db"
    shutil.copy(out, restored)
    con = sqlite3.connect(restored)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    consents = con.execute("SELECT consent_version, consent_text, ip FROM consents").fetchall()
    ledger = con.execute("SELECT buyer_id, kind, amount FROM buyer_ledger").fetchall()
    con.close()
    assert consents == [("v5", "SPECIAL ARROW → consent text", "192.0.2.9")]
    assert ledger == [("BAK-BUYER", "LEAD_CHARGE", -25.0)]
    # the restored file opens exactly like production would (WAL, schema intact)
    from engine.db import connect
    c2 = connect(restored)
    assert c2.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1
    c2.close()


def test_source_resolution_warns_when_env_is_lost(tmp_path):
    """The trap hit during this session's rehearsal: without LEADENGINE_DB the script used to
    silently snapshot its default local path. It must resolve loudly instead."""
    mod = _load_backup_mod()
    src, warned = mod.resolve_source("")
    assert warned is True and src == mod.DEFAULT_SRC
    src, warned = mod.resolve_source(str(tmp_path / "explicit.db"))
    assert warned is False and src == tmp_path / "explicit.db"


def test_missing_source_database_is_refused_not_backed_up(tmp_path):
    mod = _load_backup_mod()
    try:
        mod.make_backup(tmp_path / "no-such.db", tmp_path / "out")
        refused = False
    except SystemExit as exc:
        refused = "Refusing to back up" in str(exc)
    assert refused, "a nonexistent source must never become an empty 'backup'"
    assert list((tmp_path / "out").glob("leads_*.db")) == []


def test_subprocess_cli_backup_explicit_env(tmp_path):
    """The real CLI path: same result through subprocess with an explicit env, no fallback warning."""
    _build_db(tmp_path / "db" / "leads.db")
    env = {**os.environ, "LEADENGINE_DB": str(tmp_path / "db" / "leads.db"), "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run([PY, str(ROOT / "backup_db.py"), str(tmp_path / "b2")], capture_output=True,
                       text=True, cwd=str(ROOT), env=env, timeout=120, errors="replace")
    assert r.returncode == 0, r.stderr
    assert "WARNING" not in r.stderr
    assert len(list((tmp_path / "b2").glob("leads_*.db"))) == 1


# ------------------------------------------------------------------ restore

def test_restore_command_rebuilds_a_working_database(tmp_path):
    """`--restore` is the documented copy-verify procedure as one command: fresh target,
    integrity verified, every sentinel row present."""
    _build_db(tmp_path / "db" / "leads.db")
    mod = _load_backup_mod()
    bak = mod.make_backup(tmp_path / "db" / "leads.db", tmp_path / "backups")
    target = tmp_path / "out" / "leads.db"
    out = mod.restore(bak, target)
    assert out == target
    con = sqlite3.connect(target)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("SELECT consent_text FROM consents").fetchone()[0] == "SPECIAL ARROW → consent text"
    assert con.execute("SELECT COUNT(*) FROM buyer_ledger").fetchone()[0] == 1
    con.close()


def test_restore_refuses_a_missing_backup(tmp_path):
    mod = _load_backup_mod()
    try:
        mod.restore(tmp_path / "no-such.db", tmp_path / "out" / "leads.db")
        refused = False
    except SystemExit as exc:
        refused = "Refusing to restore" in str(exc)
    assert refused, "a wrong path must never become an empty 'restored' database"
    assert not (tmp_path / "out").exists()


def test_restore_never_overwrites_an_existing_target_without_force(tmp_path):
    _build_db(tmp_path / "db" / "leads.db")
    mod = _load_backup_mod()
    bak = mod.make_backup(tmp_path / "db" / "leads.db", tmp_path / "backups")
    existing = tmp_path / "db" / "live.db"
    existing.write_bytes(b"KEEP-ME")
    try:
        mod.restore(bak, existing)
        refused = False
    except SystemExit as exc:
        refused = "already exists" in str(exc)
    assert refused and existing.read_bytes() == b"KEEP-ME", "a live DB must not be replaced silently"
    mod.restore(bak, existing, force=True)          # explicit operator intent
    assert existing.read_bytes() != b"KEEP-ME"


def test_restore_cli_refuses_to_guess_a_target(tmp_path):
    """Without a target arg AND without LEADENGINE_DB the restore has nothing sane to write into:
    refuse loudly instead of defaulting into the dev database next to the source code."""
    mod = _load_backup_mod()
    env_backup = os.environ.pop("LEADENGINE_DB", None)
    try:
        bak = tmp_path / "b.db"
        bak.write_bytes(b"")
        try:
            mod.main(["--restore", str(bak)])
            refused = False
        except SystemExit as exc:
            refused = "Refusing to guess" in str(exc)
        assert refused
    finally:
        if env_backup is not None:
            os.environ["LEADENGINE_DB"] = env_backup


def test_restore_cli_uses_leadengine_db_as_target(monkeypatch, tmp_path):
    _build_db(tmp_path / "db" / "leads.db")
    mod = _load_backup_mod()
    bak = mod.make_backup(tmp_path / "db" / "leads.db", tmp_path / "backups")
    monkeypatch.setenv("LEADENGINE_DB", str(tmp_path / "db" / "restored.db"))
    assert mod.main(["--restore", str(bak)]) == 0
    con = sqlite3.connect(tmp_path / "db" / "restored.db")
    assert con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1
    con.close()
