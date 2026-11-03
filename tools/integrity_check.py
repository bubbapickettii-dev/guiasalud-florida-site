"""Structural integrity gate — catches broken application structure BEFORE tests can lie.

  python tools/integrity_check.py        # exit 0 = safe, 1 = something is broken

The last sprint's incident: partially applied edits left app.py with an unclosed call, an
assignment swallowed into a comment, and a helper called but never defined. Those defects
only surfaced as route 500s and surprise test failures. This gate runs the cheap checks
that would have caught every one of them, using only the standard library:

  1. every .py in the project compiles (catches truncated calls, unclosed strings/parens)
  2. config/*.json parses as UTF-8 JSON (catches truncated or mangled config edits)
  3. templates/*.html parses as Jinja (catches broken/blocked tags and bad filters in edits)
  4. the application imports cleanly (catches missing modules, top-level logic errors)

It never loads the real database for writes: the temp DB is isolated, so this is safe to
run at any time, including before every test session (tests/test_00_integrity.py runs it
FIRST so a structurally broken tree fails loudly instead of producing misleading greens
elsewhere).
"""
import json, os, shutil, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FAILS: list[str] = []


def step(name: str, cond: bool, detail: str = ""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def check_python() -> None:
    import compileall
    bad = _compile_problems(ROOT)
    step("every .py compiles", not bad, "; ".join(bad[:5]))


def _compile_problems(root: Path) -> list[str]:
    """Syntax problems under `root` (the same rules the gate applies to the live tree). A separate
    pure function so tests can point it at a CONTROLLED TEMP directory of intentionally broken
    code — the gate is validated without ever corrupting the working application."""
    bad = []
    for p in root.rglob("*.py"):
        rel = p.relative_to(root).as_posix()
        parts = set(rel.split("/"))
        if parts & {"data", "outbox", "__pycache__", ".venv", "partner_kit", "snap", "research"}:
            continue                      # build/runtime outputs, not hand-edited source
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
            compile(src, str(p), "exec")
        except (SyntaxError, ValueError, OSError) as e:
            bad.append(f"{rel}: {e}")
    return bad


def check_config() -> None:
    bad = []
    for p in sorted((ROOT / "config").glob("*.json")):
        try:
            json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            bad.append(f"config/{p.name}: {e}")
    step("config/*.json are valid UTF-8 JSON", not bad, "; ".join(bad[:5]))


def check_templates() -> None:
    bad = []
    tmpl = ROOT / "templates"
    for p in sorted(tmpl.glob("*.html")):
        try:
            src = p.read_text(encoding="utf-8")
            _jinja().parse(src)
        except Exception as e:            # noqa: BLE001 - any parse failure is a defect
            bad.append(f"templates/{p.name}: {e}")
    step("templates/*.html parse as Jinja", not bad, "; ".join(bad[:5]))


def check_import() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="integrity_"))
    try:
        os.environ["LEADENGINE_DB"] = str(tmp / "integrity.db")
        os.environ["ADMIN_TOKEN"] = "integrity"
        os.environ["INTEL_DB"] = str(tmp / "none.db")
        os.environ.setdefault("LEADENGINE_LIVE_DELIVERY", "0")
        sys.path.insert(0, str(ROOT))
        import app  # noqa: F401 - the import itself is the check
        routes = list(app.app.url_map.iter_rules())
        step("application imports cleanly", True)
        need = {"/api/lead", "/agent/<token>", "/agent/<token>/billing", "/admin", "/check",
                "/admin/lead/<lead_id>", "/api/webhooks/stripe"}
        have = {r.rule for r in routes}
        missing = sorted(need - have)
        step(f"route registration sane ({len(routes)} routes)", not missing and len(routes) >= 70,
             f"missing: {missing}" if missing else "")
    except Exception as e:                # noqa: BLE001
        step("application imports cleanly", False, f"{type(e).__name__}: {e}")
    finally:
        sys.path.pop(0) if ROOT in [Path(x).resolve() for x in sys.path] else None
        shutil.rmtree(tmp, ignore_errors=True)
        os.environ.pop("LEADENGINE_DB", None)
        os.environ.pop("ADMIN_TOKEN", None)
        os.environ.pop("INTEL_DB", None)


_JINJA = None


def _jinja():
    global _JINJA
    if _JINJA is None:
        from jinja2 import Environment, select_autoescape
        _JINJA = Environment(autoescape=select_autoescape(["html"]))
    return _JINJA


def main() -> int:
    print("Structural integrity check — compile + config + templates + import\n")
    check_python()
    check_config()
    check_templates()
    check_import()
    if FAILS:
        print(f"\nResult: {len(FAILS)} area(s) NOT SAFE — fix before testing or shipping")
        return 1
    print("\nResult: ALL SAFE — the tree parses, config is JSON, templates compile, the app imports")
    return 0


if __name__ == "__main__":
    sys.exit(main())
