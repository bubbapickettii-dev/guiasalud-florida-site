#!/bin/bash
# One-shot PythonAnywhere setup. Run in a PythonAnywhere Bash console, from your home directory,
# AFTER you uploaded lead_engine_v13.zip (Files tab) and created the web app (Web tab > Add a new web app >
# Manual configuration > pick the Python version you pass as PYV below).
#
# Usage:
#   unzip -q lead_engine_v13.zip && bash leadengine/deploy/pa_setup.sh PYV "Brand name" "Legal entity name" "contact@email" "Mailing address"
# Example:
#   unzip -q lead_engine_v13.zip && bash leadengine/deploy/pa_setup.sh 3.11 "Cobertura Florida" "Acme Media LLC" "hello@example.com" "123 Main St, Miami, FL 33101"
#
# What it does: unzips, installs dependencies for that Python, creates the data folder (your database lives there),
# writes the WSGI file with your details and a random admin token, runs the smoke test, prints what to click next.
set -euo pipefail

PYV="${1:?Python version, e.g. 3.11 (must match the version chosen on the Web tab)}"
BRAND="${2:?brand name}"
ENTITY="${3:?legal entity name}"
EMAIL="${4:?contact email}"
ADDR="${5:?mailing address}"

HOME_DIR="${PA_HOME:-$HOME}"
USER_NAME="${PA_USER:-$(whoami)}"
APP="$HOME_DIR/leadengine"
DATA="$HOME_DIR/data"
PY="python$PYV"
BASE="https://${USER_NAME}.pythonanywhere.com"
WSGI="${PA_WSGI:-/var/www/${USER_NAME}_pythonanywhere_com_wsgi.py}"

command -v "$PY" >/dev/null || { echo "ERROR: $PY not found. Use a version PythonAnywhere offers (3.10 or newer)."; exit 1; }

if [ ! -d "$APP" ]; then
  [ -f "$HOME_DIR/lead_engine_v13.zip" ] || { echo "ERROR: upload lead_engine_v13.zip to $HOME_DIR first (Files tab)."; exit 1; }
  (cd "$HOME_DIR" && unzip -q lead_engine_v13.zip)
fi
mkdir -p "$DATA"
"$PY" -m pip install --user -q -r "$APP/requirements-prod.txt"

TOKEN="$("$PY" -c 'import secrets;print(secrets.token_urlsafe(24))')"
OUT="$APP/deploy/pa_wsgi_filled.py"
cat > "$OUT" <<EOF
import os, sys
HOME = "$HOME_DIR"
APP = HOME + "/leadengine"
sys.path.insert(0, APP)
os.chdir(APP)
os.environ["LEADENGINE_ENV"] = "production"
os.environ["LEADENGINE_DB"] = HOME + "/data/leads.db"
os.environ["LEADENGINE_JOURNAL"] = "DELETE"
os.environ["BASE_URL"] = "$BASE"
os.environ["ADMIN_TOKEN"] = "$TOKEN"
os.environ["ALLOW_INDEXING"] = "0"   # indexing is a launch approval (launch_check.py group 8); flip to 1 in the WSGI file when YOU approve
os.environ["LEADENGINE_LIVE_DELIVERY"] = "0"
os.environ["BRAND"] = "$BRAND"
os.environ["LEGAL_ENTITY"] = "$ENTITY"
os.environ["CONTACT_EMAIL"] = "$EMAIL"
os.environ["MAILING_ADDRESS"] = "$ADDR"
from app import app as application  # noqa: E402
EOF
chmod 600 "$OUT"

echo "== Smoke test (same settings, local test client)"
(cd "$APP" && "$PY" - <<EOF
import os, runpy
src = open("$OUT").read().replace('from app import app as application  # noqa: E402', '')
exec(src)
from app import app
c = app.test_client()
bad = [(u, c.get(u).status_code) for u in ["/healthz", "/", "/check", "/check?a=es_check", "/learn", "/aprende", "/privacy", "/privacidad", "/terms", "/sitemap.xml"] if c.get(u).status_code != 200]
print("FAILED:", bad) if bad else print("All key pages return 200")
assert not bad
EOF
)

if [ -f "$WSGI" ]; then
  cp "$WSGI" "$WSGI.bak.$(date +%s)" 2>/dev/null || true
  cp "$OUT" "$WSGI"
  echo "== WSGI file written: $WSGI"
else
  echo "== WSGI file $WSGI not found (create the web app on the Web tab first). Paste the contents of:"
  echo "   $OUT"
  echo "   into the WSGI file linked on the Web tab."
fi

echo
echo "== NEXT (Web tab): set Source code and Working directory to $APP, turn Force HTTPS on, click Reload."
echo "== ALLOW_INDEXING is 0 (safe staging default). When you approve indexing: set it to 1 in the WSGI file and Reload (launch_check group 8)."
echo "== Then open $BASE/healthz  (should show ok) and $BASE/check"
echo "== Your admin token is stored only inside: $OUT   (admin pages: $BASE/admin/organic?token=<that token>)"
echo "== Test devices: open $BASE/qa?on=1 once on every phone/laptop you use so your own visits count as TEST."
