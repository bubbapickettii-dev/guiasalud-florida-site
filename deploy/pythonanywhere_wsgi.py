# PythonAnywhere WSGI file. Paste this into the WSGI configuration file linked on the "Web" tab.
# Replace USERNAME and the values marked CHANGE. Data lives in your home directory, which persists across reloads.
import os
import sys

HOME = "/home/USERNAME"                                   # CHANGE
APP = HOME + "/leadengine"
sys.path.insert(0, APP)
os.chdir(APP)
os.environ.setdefault("LEADENGINE_ENV", "production")
os.environ.setdefault("LEADENGINE_DB", HOME + "/data/leads.db")
os.environ.setdefault("BASE_URL", "https://USERNAME.pythonanywhere.com")   # CHANGE to https://yourdomain.com after the domain is added
os.environ.setdefault("ADMIN_TOKEN", "CHANGE-set-by-pa_setup.sh")
os.environ.setdefault("ALLOW_INDEXING", "0")              # safe staging default; flip to 1 when YOU approve indexing (launch_check group 8)
os.environ.setdefault("LEADENGINE_LIVE_DELIVERY", "0")    # agents read leads in their portal (/agent/<token>); no email needed to start
os.environ.setdefault("LEGAL_ENTITY", "CHANGE: legal name shown in the footer/privacy page")
os.environ.setdefault("CONTACT_EMAIL", "CHANGE: a monitored email")
os.environ.setdefault("MAILING_ADDRESS", "CHANGE: business mailing address")

from app import app as application  # noqa: E402
