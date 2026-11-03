# Go live for $0 today, $10/month with a domain later

| Option | Cost | HTTPS | Keeps leads (SQLite) | Custom domain | Verdict |
|---|---|---|---|---|---|
| **PythonAnywhere free** | $0 | yes (`USERNAME.pythonanywhere.com`) | yes (home directory persists) | no | **Start here.** Real submissions possible today. Click "Run until 1 month from today" on the Web tab monthly or the app expires. |
| PythonAnywhere Developer | $10/mo | yes (free Let's Encrypt) | yes | yes | Upgrade once the first channel shows real leads. |
| Render free | $0 | yes | **no**: no persistent disk; sleeps after 15 min | yes | Don't use. It would lose consent records and leads. |
| Render Starter + 1 GB disk | ~$7/mo + disk | yes | yes | yes | Fine alternative (`render.yaml` is ready). |
| **Render Free** | $0 | yes (`NAME.onrender.com`) | **no** (no disk) | no | The site **is** publishable here — canonicals, sitemap, robots and HTTPS included — but the host keeps nothing, so the app runs a read-only public preview and refuses submissions. `render-free.yaml`, explained in `docs/RENDER_DEPLOY.md`. |
| Domain (Cloudflare Registrar, .com) | $10.46/yr | - | - | - | Buy when upgrading. |

Prices checked Oct 8 2026: PythonAnywhere blog (Dec 15 2025), livemy.app Render comparison (Aug 2026), domainoffer.net (Cloudflare). Recheck at signup.

## Steps (about 20 minutes, done by Bubba because it needs an account)
1. Create a free PythonAnywhere account. The username becomes your address (`USERNAME.pythonanywhere.com`), so pick a name you can show the public.
2. **Files** tab: upload `lead_engine_v13.zip` to your home folder (the first page of Files). Built from the project with `python tools/build_deploy_zip.py` (runtime files only — no local databases, snapshots or test artifacts ship).
3. **Web** tab: Add a new web app > your username's free domain > **Manual configuration** > choose Python 3.11 (any 3.10+; remember it).
4. Open a **Bash console** (Consoles tab) and run (fill in your four values):
   `unzip -q lead_engine_v13.zip && bash leadengine/deploy/pa_setup.sh 3.11 "Brand name" "Legal entity name" "contact@email" "Mailing address"`
   It unzips, installs dependencies, creates the data folder, writes the WSGI file with a random admin token, smoke-tests the pages and tells you what is left.
5. **Web** tab: set Source code and Working directory to `/home/USERNAME/leadengine`, turn **Force HTTPS** on, click **Reload**.
6. Open `https://USERNAME.pythonanywhere.com/healthz`, then tell me the URL. I test the public site (EN/ES, mobile, attribution, privacy identity, persistence) before calling it live.
7. On your phone and laptop open `https://USERNAME.pythonanywhere.com/qa?on=1` once so your own visits count as TEST.
8. Later (only after you approve indexing, ALLOW_INDEXING=1): submit `https://USERNAME.pythonanywhere.com/sitemap.xml` in Google Search Console and Bing Webmaster Tools. A buyer entry in `config/buyers.json` is only needed for phone capture.

The script sets `LEADENGINE_JOURNAL=DELETE` (plain SQLite journal) because I do not know if PythonAnywhere's file system supports WAL mode; the app falls back automatically anyway.

Free-tier limits worth knowing (checked Oct 2026, official pythonanywhere.com pages):
- The free web app **expires after 1 month** (changed from 3 months in Jan 2026): click "Run until 1 month from today" on the Web tab monthly, or the app goes dark.
- **512 MB** disk quota — the app package is ~0.5 MB and dependencies ~100-150 MB, but logs and the .local dir count too; keep backup copies pruned (backup_db.py keeps 30).
- **100 CPU-seconds/day applies to consoles and scheduled tasks, NOT web apps.**
- **One web worker**: one request at a time, fine at early volume.
- Outbound internet is restricted to a whitelist. Nothing in the funnel needs it initially (agents read leads in their portal); buyer **webhook OUT-bound delivery and external SMTP later need the paid plan**. Free SMTP is limited to Google servers.
- Inbound requests (Stripe webhook → us) are not rate-limited on free, but the free tier is not the right place to verify payment rails — keep payment tests in staging (TEST keys) or on the paid plan.
