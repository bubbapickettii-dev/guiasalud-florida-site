"""Build and verify the three organic-social tracking links (Instagram, TikTok, Facebook).

The rule this file enforces, because getting it wrong sends strangers to the wrong product:

  a profile link may only ever be   https://<permanent-consumer-domain><consumer path>?utm_*
  and may never be                  a staging/localhost host, the buyer application (/for-agents,
                                    /para-agencias), the buyer portal (/agent), /partner or /admin.

With `config/social.json` `domain` empty the tool REFUSES to invent a host: it writes the links
artifact with status BLOCKED and the exact query string each platform needs, so nothing temporary can
be pasted into a bio by accident.

Usage
    python tools/social_links.py --write                    # write out/social_kit/links.{json,md}
    python tools/social_links.py --domain https://guiasalud.com --write
    python tools/social_links.py --domain https://guiasalud.com --verify   # real GET, follows redirects

`--verify` is the only mode that touches the network; it checks the FINAL url after redirects, so a
misconfigured redirect to /admin or to a staging host fails loudly instead of passing on a 200.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "config" / "social.json"
OUT = ROOT / "out" / "social_kit"

# consumer entry points only. /for-agents and /para-agencias are the BUYER application: a social bio
# that lands an agency there is a different (and wrong) audience, and /agent/<token> is private data.
ALLOWED_PATHS = {"/", "/check", "/learn", "/aprende", "/free-help", "/ayuda-gratis",
                 "/layoffs", "/despidos", "/open-enrollment-2027", "/inscripcion-abierta-2027"}
FORBIDDEN_PREFIXES = ("/admin", "/agent", "/partner", "/for-agents", "/para-agencias", "/api", "/p/", "/go/", "/i/", "/qa")
LOCAL_HOSTS = re.compile(r"(^|\.)(localhost|local|internal|test|staging|preview|ngrok\.io|ngrok-free\.app|"
                         r"loca\.lt|trycloudflare\.com|vercel\.app)$", re.I)
# a staging label can sit anywhere (`staging.example.com`, `preview.example.com`), so check every label
NON_PUBLIC_LABELS = {"localhost", "local", "internal", "test", "tests", "staging", "stage", "preview",
                     "dev", "demo", "sandbox", "qa", "uat"}


class BadDestination(ValueError):
    """A destination that must never be published."""


def validate(url: str) -> str:
    """Return the normalized url, or raise BadDestination with the reason a human needs to see."""
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https":
        raise BadDestination(f"{url}: must be https (a profile link is public and permanent)")
    host = (p.hostname or "").lower()
    if not host or "." not in host:
        raise BadDestination(f"{url}: host is not a real domain")
    if (LOCAL_HOSTS.search(host) or host.startswith("127.") or host == "0.0.0.0"
            or NON_PUBLIC_LABELS & set(host.split("."))):
        raise BadDestination(f"{url}: {host} is a staging/local host, never a permanent profile destination")
    if p.port and p.port != 443:
        raise BadDestination(f"{url}: non-standard port {p.port}")
    if p.path.rstrip("/") and p.path.rstrip("/") not in {x.rstrip("/") for x in ALLOWED_PATHS}:
        raise BadDestination(f"{url}: {p.path} is not a consumer landing page")
    for bad in FORBIDDEN_PREFIXES:
        if p.path.startswith(bad):
            raise BadDestination(f"{url}: {bad} is not a consumer page (buyer app / portal / admin)")
    return urllib.parse.urlunsplit(("https", p.netloc, p.path or "/", p.query, ""))


def build(profile: dict, domain: str, landing_path: str) -> dict:
    base = domain.rstrip("/")
    utm = profile["utm"]
    query = urllib.parse.urlencode({"utm_source": utm["utm_source"],
                                    "utm_medium": utm["utm_medium"],
                                    "utm_campaign": utm["utm_campaign"]})
    url = validate(f"{base}{landing_path}?{query}")
    return {"platform": profile["platform"], "url": url, "utm": utm,
            "handle": profile.get("handle", ""), "status": "READY"}


def blocked(profile: dict, landing_path: str) -> dict:
    utm = profile["utm"]
    query = urllib.parse.urlencode({"utm_source": utm["utm_source"],
                                    "utm_medium": utm["utm_medium"],
                                    "utm_campaign": utm["utm_campaign"]})
    return {"platform": profile["platform"], "url": "", "utm": utm, "handle": profile.get("handle", ""),
            "status": "BLOCKED", "blocked_reason": "no permanent consumer domain configured",
            "template": f"https://<PERMANENT-DOMAIN>{landing_path}?{query}"}


def verify(url: str) -> dict:
    """GET the link like a phone browser and report what the visitor would actually land on."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            final, code = r.geturl(), r.status
    except urllib.error.HTTPError as e:                      # a 4xx/5xx is a real answer, keep it
        final, code = e.geturl() or url, e.code
    except Exception as e:                                   # DNS/TLS/timeout: report, never hide
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "final_url": "", "http_status": 0}
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(final).query)
    problems = []
    try:
        validate(final)
    except BadDestination as e:
        problems.append(str(e))
    if q.get("utm_source", [""])[0] != urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["utm_source"][0]:
        problems.append(f"utm_source lost in redirect (final: {final})")
    return {"ok": not problems, "problems": problems, "final_url": final, "http_status": code}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--domain", default="", help="permanent https origin, e.g. https://guiasalud.com")
    ap.add_argument("--write", action="store_true", help="write out/social_kit/links.json and links.md")
    ap.add_argument("--verify", action="store_true", help="fetch each link and check the final destination")
    ap.add_argument("--cfg", default=str(CFG))
    ap.add_argument("--out", default=str(OUT), help="directory for links.json / links.md (tests use a temp dir)")
    a = ap.parse_args(argv)

    cfg = json.loads(Path(a.cfg).read_text(encoding="utf-8"))
    domain = (a.domain or cfg.get("domain") or "").strip()
    landing = cfg.get("landing_path") or "/"

    if domain:
        try:
            domain = validate(f"{domain.rstrip('/')}{landing}")
        except BadDestination as e:
            print(f"REFUSED: {e}", file=sys.stderr)
            return 2
        domain = urllib.parse.urlsplit(domain)._replace(path="", query="").geturl()

    rows = [build(p, domain, landing) if domain else blocked(p, landing) for p in cfg["profiles"]]

    if a.verify:
        if not domain:
            print("nothing to verify: no permanent domain configured", file=sys.stderr)
            return 2
        failed = 0
        for r in rows:
            v = verify(r["url"])
            r["verification"] = v
            failed += 0 if v["ok"] else 1
            print(f"{r['platform']:10s} {v.get('http_status', 0)}  {v.get('final_url') or v.get('error')}"
                  f"{'' if v['ok'] else '  PROBLEMS: ' + '; '.join(v.get('problems') or [v.get('error', '')])}")
        rows_failed = failed
    else:
        rows_failed = 0

    status = "BLOCKED" if not domain else ("VERIFIED" if a.verify and not rows_failed else "READY")
    payload = {"brand": cfg.get("brand"), "platform_name": cfg.get("platform_name"),
               "domain": domain, "landing_path": landing, "status": status,
               "generated_by": "tools/social_links.py", "links": rows,
               "note": ("No link may be published until the permanent consumer domain exists. "
                        "These are the exact query strings each platform needs.")
               if not domain else "Paste each url into that platform's website field."}
    print(json.dumps(payload, indent=1, ensure_ascii=False))

    if a.write:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "links.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        lines = [f"# Social tracking links — {status}", ""]
        if not domain:
            lines += ["**No permanent consumer domain is configured, so nothing here is publishable.**",
                      "A staging URL must never be pasted into a bio. Fill `domain` in `config/social.json`",
                      "(or pass `--domain https://…`) and re-run `python tools/social_links.py --domain … --write --verify`.", ""]
        for r in rows:
            lines += [f"## {r['platform'].title()}", "",
                      f"- handle: `@{r['handle']}`" if r["handle"] else "",
                      f"- url: `{r['url'] or r['template']}`",
                      f"- utm: `utm_source={r['utm']['utm_source']}&utm_medium={r['utm']['utm_medium']}"
                      f"&utm_campaign={r['utm']['utm_campaign']}`",
                      f"- status: **{r['status']}**" + (f" — {r.get('blocked_reason', '')}" if r.get("blocked_reason") else ""), ""]
            if r.get("verification"):
                v = r["verification"]
                lines.append(f"- verified: {'PASS' if v['ok'] else 'FAIL'} http {v.get('http_status')} → "
                             f"`{v.get('final_url') or v.get('error')}`")
                lines.append("")
        (out / "links.md").write_text("\n".join(x for x in lines if x is not None) + "\n", encoding="utf-8")
        print(f"wrote {out / 'links.json'} and {out / 'links.md'}", file=sys.stderr)
    return 0 if not rows_failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
