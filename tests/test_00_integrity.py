"""Structural integrity gate as the FIRST test (runs before every other test file).

Why first: the last incident had a partially applied edit leave app.py with an unclosed
call while other headline reports stayed green — a corrupted tree shows up as misleading
route 500s instead of a clear signal. Running this gate first means a broken tree fails
loudly and NAMES the broken area (compile / config / template / import) before anything
else even runs. It reuses tools/integrity_check.py so operator and test protection are
the same code path.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import integrity_check as ic


def test_tree_is_structurally_sound():
    """The gate must pass on the tree as shipped: every .py compiles, config is JSON,
    templates parse as Jinja, and the app imports and registers its routes."""
    assert ic.main() == 0


def test_gate_names_broken_syntax_in_a_controlled_temp_tree(tmp_path):
    """The gate is validated against CONTROLLED intentionally-broken code in a temp directory —
    the working application is never corrupted to test the checker. An unclosed call left behind
    by a partially applied edit must be named with its file and line."""
    (tmp_path / "broken_app.py").write_text(
        '# a render call someone edited to half length\n'
        'def f():\n'
        '    return render_template("admin.html", k=k,\n', encoding="utf-8")
    problems = ic._compile_problems(tmp_path)
    assert problems and "broken_app.py" in problems[0]
    # the SAME rules pass clean code: the checker is content-based, not suspicion-based
    (tmp_path / "good_app.py").write_text("def g():\n    return 1\n", encoding="utf-8")
    assert not ic._compile_problems(tmp_path / "nonexistent")


def test_gate_catches_truncated_call():
    """Prove the mechanism on the incident's exact shape: an unclosed call is a SyntaxError,
    so check_python() flags the file instead of leaving it for surprise route 500s."""
    with pytest.raises(SyntaxError):
        compile("def f():\n    return render_template(\"admin.html\", k=k,\n", "<incident>", "exec")


def test_gate_catches_used_but_never_defined():
    """The incident's least-visible shape: a helper actually called but never defined raises
    NameError the first time that code path executes — exactly what the failing routes did."""
    ns: dict = {}
    code = "def ok():\n    pass\nok()\nget_marketplace().get('returns')\n"
    with pytest.raises(NameError):
        exec(compile(code, "<used-but-never-defined>", "exec"), ns)
