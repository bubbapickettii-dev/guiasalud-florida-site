"""Build the PythonAnywhere deployment package: lead_engine_v13.zip, with a leadengine/ root.

This is the zip deploy/GO_LIVE_CHEAP.md and deploy/pa_setup.sh expect uploaded to the
home directory. Run from the project root:  python tools/build_deploy_zip.py [dest]

Includes every runtime file (app.py imports build_partner_kit; admin QR/PDF routes
need engine/, config/, static/, templates/). Excludes machine-local state (databases,
outbox, snapshots, partner kits, screenshots, caches) so nothing from staging leaks
into the deployment, and no consent/lead data leaves the machine.
"""
from __future__ import annotations
import sys
import zipfile
from pathlib import Path

ROOTS = ["app.py", "backup_db.py", "build_partner_kit.py", "launch_check.py",
         "requirements-prod.txt", "requirements.txt", ".env.example",
         "Dockerfile", "render.yaml", "render-free.yaml", "README.md", "LAUNCH.md",
         "engine", "config", "static", "templates", "deploy", "docs"]

EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", "screens", "screens_v3", "screens_wave2",
                "screens_engines", "screens_acq", "comprehensive", "outbox_smoke"}

def excluded(p: Path) -> bool:
    if any(part in EXCLUDE_DIRS for part in p.parts):
        return True
    if p.suffix in (".db", ".db.bak-final", ".eml", ".pyc", ".zip", ".xlsx", ".mp4", ".mov"):
        return True
    if p.name.startswith(("leads_", "smoke_", "scout_manual")):
        return True
    return False

def add_tree(zf: zipfile.ZipFile, src: Path, arcroot: str):
    for p in sorted(src.rglob("*")):
        if p.is_file() and not excluded(p):
            zf.write(p, arcroot + "/" + p.relative_to(src).as_posix())

def main():
    base = Path(__file__).resolve().parents[1]
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else base.parent / "lead_engine_v13.zip")
    missing = [n for n in ROOTS if not (base / n).exists()]
    if missing:
        sys.exit(f"ERROR: package source missing: {missing}")
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ROOTS:
            p = base / name
            if p.is_file():
                zf.write(p, "leadengine/" + name)
            else:
                add_tree(zf, p, "leadengine/" + name)
    files = zf.namelist()
    print(f"{dest}\n  {len(files)} files, {dest.stat().st_size / 1e6:.1f} MB")
    for key in ("leadengine/app.py", "leadengine/requirements-prod.txt",
                "leadengine/deploy/pa_setup.sh", "leadengine/config/buyers.json",
                "leadengine/engine/route.py", "leadengine/static/health.js",
                "leadengine/templates/check.html", "leadengine/launch_check.py",
                "leadengine/docs/DEPLOYMENT.md"):
        assert key in files, f"package missing {key}"
    assert not [f for f in files if ".db" in f or "screenshots" in f], "local artifacts leaked"
    print("package OK")

if __name__ == "__main__":
    main()
