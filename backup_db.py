"""Hot backup of the lead database (consent records must be kept 10 years under 45 CFR 155.220(j)(2)(iii)).
Usage: python backup_db.py [dest_dir]                                          - hot-copy + rotate (run daily)
       python backup_db.py --restore <backup.db> [target.db] [--force]         - the documented restore as a command
  - run the backup daily (Render cron job / host cron). Keeps the 30 newest local copies.
  - the restore writes the backup to target and verifies PRAGMA integrity_check; the target is
    never overwritten without --force, and restoring into the LIVE file is only legal with the
    app STOPPED (stop -> restore -> set LEADENGINE_DB -> start, per docs/DAY1_RUNBOOK.md).

Safety:
  - the backup is taken from the database LEADENGINE_DB names. A run whose environment LOST
    that variable prints a loud WARNING on stderr before proceeding with the default local
    path — so a backup never silently gets mistaken for the real production copy (a cron or
    shell can drop env variables without anyone noticing).
  - a NONEXISTENT source is refused, not "backed up": an empty/wrong database must never
    become something that looks like a restore point.

Rehearsed + regression-tested in tests/test_backup_restore.py (disposable copies only).
"""
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SRC = Path(__file__).parent / "data" / "leads.db"


def resolve_source(env: str | None) -> tuple[Path, bool]:
    """(source path, warned) — warned=True when LEADENGINE_DB was absent/blank and the default
    local path is being used instead. Callers print the WARNING; tests assert on this flag."""
    explicit = (env or "").strip()
    if not explicit:
        return DEFAULT_SRC, True
    return Path(env), False


def make_backup(src: Path, dest_dir: Path) -> Path:
    if not src.is_file():
        raise SystemExit(f"Refusing to back up: source database not found: {src}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"leads_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.db"
    with sqlite3.connect(src) as s, sqlite3.connect(out) as d:
        s.backup(d)
    for old in sorted(dest_dir.glob("leads_*.db"))[:-30]:
        old.unlink()
    return out


def restore(backup: Path, target: Path, force: bool = False) -> Path:
    """The documented restore (tests/test_backup_restore.py) as a command, made safe:
      - the backup file must EXIST (a wrong path never becomes an empty 'restored' database)
      - the TARGET database file is never overwritten unless --force is given explicitly
        (the documented restore procedure REPLACES a live DB only after the app is stopped)
      - the copied file is verified with PRAGMA integrity_check before being handed back
    Restoring into the LIVE file must only happen with the app stopped; the command itself
    only does the file copy + verification — stopping the app and setting LEADENGINE_DB to
    the restored path stays the operator's documented step (docs/DAY1_RUNBOOK.md)."""
    import shutil
    if not backup.is_file():
        raise SystemExit(f"Refusing to restore: backup file not found: {backup}")
    if target.exists() and not force:
        raise SystemExit(f"Refusing to restore: target database already exists: {target}\n"
                         "Use --force to replace it (and only with the app STOPPED).")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(backup, target)
    with sqlite3.connect(target) as c:
        ok = c.execute("PRAGMA integrity_check").fetchone()[0]
    if ok != "ok":
        raise SystemExit(f"RESTORE FAILED: copied backup failed integrity_check ({ok}); delete {target} and inspect the backup.")
    return target


def main(argv: list[str] | None = None) -> int:
    """CLI: python backup_db.py [dest_dir] | --restore <backup.db> [target.db] [--force]."""
    argv = list(argv if argv is not None else sys.argv[1:])
    if "--restore" in argv:
        rest = argv[argv.index("--restore") + 1:]
        force = "--force" in rest
        args = [a for a in rest if a != "--force"]
        if not args:
            raise SystemExit("Usage: python backup_db.py --restore <backup.db> [target.db] [--force]")
        backup = Path(args[0])
        if len(args) > 1:
            target = Path(args[1])
        elif os.environ.get("LEADENGINE_DB", "").strip():
            target = Path(os.environ["LEADENGINE_DB"])       # the app's configured DB location
        else:
            raise SystemExit("Refusing to guess a restore target: name the file to restore into "
                             "(python backup_db.py --restore <backup.db> <target.db>) or set LEADENGINE_DB.")
        print(restore(backup, target, force=force))
        return 0
    dest_dir = argv[0] if argv else None
    src, warned = resolve_source(os.environ.get("LEADENGINE_DB"))
    if warned:
        print(f"WARNING: LEADENGINE_DB not set; backing up the DEFAULT local path: {src}",
              file=sys.stderr)
    print(make_backup(src, Path(dest_dir if dest_dir else src.parent / "backups")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
