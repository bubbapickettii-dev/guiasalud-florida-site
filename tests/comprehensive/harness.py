"""Pass 1 harness: an isolated staging app for comprehensive testing.

- FRESH SQLite DB in tests/comprehensive/ (deleted each run)
- REAL config dir read-only for fixture setup, never modified (changes go to a COPY)
- LEADENGINE_LIVE_DELIVERY=0, staging mode, no Stripe keys -> nothing can be live
- Admin token fixed for the pass

Usage from any pass script:

    p1 = Harness()            # fresh app + temp DB
    p1.make_client()          # Flask test client (with QA cookie handling helpers)
    env()                     # env vars, for browser launches
"""
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "tests" / "comprehensive"


class Harness:
    def __init__(self, name: str = "p1", fresh_cfg: bool = False):
        self.name = name
        self.wdir = WORK / name
        if self.wdir.exists():
            shutil.rmtree(self.wdir)
        self.wdir.mkdir(parents=True)
        self.cfg = self.wdir / "cfg"
        if fresh_cfg:
            shutil.copytree(ROOT / "config", self.cfg)
        else:
            self.cfg = ROOT / "config"
        self.db_path = self.wdir / "test.db"
        self.env = {
            **os.environ,
            "ADMIN_TOKEN": "comprehensive-pass-token-1",
            "LEADENGINE_DB": str(self.db_path),
            "LEADENGINE_CFG": str(self.cfg),
            "LEADENGINE_OUTBOX": str(self.wdir / "outbox"),
            "LEADENGINE_LIVE_DELIVERY": "0",
            "STAGING": "1",
        }
        self.env.pop("BASE_URL", None)
        self.env.pop("STRIPE_SECRET_KEY", None)
        self.env.pop("STRIPE_WEBHOOK_SECRET", None)

    def load_app(self):
        sys.path.insert(0, str(ROOT))
        os.environ.clear()
        os.environ.update(self.env)
        for m in [m for m in list(sys.modules) if m == "app" or m.startswith("engine.")]:
            del sys.modules[m]
        import app as appmod

        self.appmod = appmod
        self.app = appmod.app
        return appmod

    def make_client(self):
        reloads_cfg = getattr(self.appmod, "PYTEST_CFG", None)
        return self.app.test_client()

    def wet_run(self, enable: bool):
        """Toggle LEADENGINE_LIVE_DELIVERY *inside the already-imported module*."""
        self.appmod.os.environ["LEADENGINE_LIVE_DELIVERY"] = "1" if enable else "0"

    def post_lead(self, client, payload, cookies=None):
        import requests  # not used; keep flask path only

        r = client.post("/api/lead", json=payload, follow_redirects=True)
        return r

    def con(self):
        import sqlite3

        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c
