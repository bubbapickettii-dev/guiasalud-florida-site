"""The bilingual contract for every public page.

GS Allude is sold to Florida consumers in Spanish and English, so a visitor must always be able to
cross over from any page to the *same page* in the other language, and search engines must be told
which URL is which language. This file pins that down page by page:

  - <html lang> matches the page's real language
  - the language switch link in the header goes to the other language's version of that page
  - the two versions are reciprocal: the other page's alternate tag points back to this one
  - rel=alternate covers exactly en and es, and the self alternate equals the canonical
    (this is why the English home is /?lang=en: bare / is Spanish-first)
  - canonical is one absolute URL per page, never missing
  - the mobile viewport meta exists (both journeys must work on a phone)
  - no template leaks raw Jinja, "None" or "undefined" into the page body

Nothing here needs a browser: it is the server-rendered contract that the JS layer then uses.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as A                                    # noqa: E402

def fresh():
    """A brand-new visitor. Never share a client between checks: the language choice is a 1-year
    cookie, so one shared client would leak 'en' into pages that are being checked in Spanish."""
    return A.app.test_client()


# Bare / is Spanish-first for Florida traffic that says nothing, and the check defaults to the visitor's
# preference too, so the English entries name their language explicitly.
EN_ONLY = ["/?lang=en", "/check?lang=en", "/check?a=health_check", "/free-help", "/privacy", "/terms",
           "/open-enrollment-2027", "/learn", "/layoffs", "/for-agents"]
ES_ONLY = ["/", "/check", "/check?a=es_check", "/ayuda-gratis", "/privacidad", "/terminos",
           "/inscripcion-abierta-2027", "/aprende", "/despidos", "/para-agencias"]


def public_pages():
    """(path, lang) for every indexable public page, generated from the same data the site renders."""
    out = [(p, "en") for p in EN_ONLY] + [(p, "es") for p in ES_ONLY]
    out += [("/learn/" + p["slug"], "en") for p in A.CONTENT["pages"] if p["lang"] == "en"]
    out += [("/aprende/" + p["slug"], "es") for p in A.CONTENT["pages"] if p["lang"] == "es"]
    for e in A._layoffs():
        out.append(("/layoffs/" + e["slug"], "en"))
        out.append(("/despidos/" + e["slug"], "es"))
    return out


def head(path):
    r = fresh().get(path)
    assert r.status_code == 200, (path, r.status_code)
    html = r.get_data(as_text=True)
    return html.split("</head>")[0], html


def canon(h):
    m = re.search(r'<link rel="canonical" href="([^"]+)"', h)
    assert m, "no canonical"
    return m.group(1)


def alternates(h):
    return dict((lang, url) for lang, url in re.findall(r'<link rel="alternate" hreflang="([\w-]+)" href="([^"]+)"', h))


def test_every_public_page_declares_its_language_and_a_mobile_viewport():
    for path, lang in public_pages():
        h, _ = head(path)
        m = re.search(r'<html[^>]*lang="([^"]+)"', h)
        assert m, f"{path}: <html> without lang"
        got = "es" if m.group(1).lower().startswith("es") else "en"
        assert got == lang, f"{path}: lang={m.group(1)} but page is {lang}"
        assert 'name="viewport"' in h, f"{path}: no mobile viewport"


def test_language_switch_points_at_the_same_page_in_the_other_language():
    for path, lang in public_pages():
        h, html = head(path)
        sw = re.search(r'class="langswitch" href="([^"]+)" hreflang="([\w-]+)"', html)
        assert sw, f"{path}: no header language switch"
        href, target_lang = sw.group(1), sw.group(2)
        assert target_lang != lang, f"{path}: switch offers the language it is already in"
        alt = alternates(h)
        assert target_lang in alt, f"{path}: switch target {target_lang} not declared as alternate"
        assert not href.startswith("http"), f"{path}: switch must be a relative link, got {href}"
        other = fresh().get(href)
        assert other.status_code == 200, f"{path}: switch -> {href} = {other.status_code}"
        other_html = other.get_data(as_text=True)
        other_lang = re.search(r'<html[^>]*lang="([^"]+)"', other_html).group(1)[:2].lower()
        assert other_lang == target_lang, f"{path}: switch -> {href} rendered {other_lang}"
        # reciprocity, verified by following the link back: it must land on this same page in this language
        back = re.search(r'class="langswitch" href="([^"]+)"', other_html)
        assert back, f"{href}: the other language has no way back"
        home = fresh().get(back.group(1)).get_data(as_text=True)
        home_lang = re.search(r'<html[^>]*lang="([^"]+)"', home).group(1)[:2].lower()
        assert home_lang == lang, f"{path}: switch -> {href} -> {back.group(1)} came back in {home_lang}"
        assert back.group(1).split("?")[0] == path.split("?")[0], \
            f"{path}: came back to {back.group(1)}, a different page"


def test_alternates_are_exactly_en_and_es_and_the_self_alternate_is_the_canonical():
    for path, _lang in public_pages():
        h, _ = head(path)
        alt = alternates(h)
        if not alt:
            continue                                   # single-language utility pages (contact, optout)
        assert set(alt) == {"en", "es"}, f"{path}: alternates named {sorted(alt)}"
        self_lang = re.search(r'<html[^>]*lang="([^"]+)"', h).group(1)
        self_lang = self_lang[:2].lower()
        assert alt[self_lang] == canon(h), f"{path}: self alternate {alt[self_lang]} != canonical {canon(h)}"


def test_canonical_is_absolute_and_language_specific_on_the_home_page():
    h_es, _ = head("/")
    h_en, _ = head("/?lang=en")
    assert canon(h_es).endswith("/") and not canon(h_es).endswith("?lang=en")
    assert canon(h_en).endswith("/?lang=en")           # bare / is Spanish-first, so EN must differ
    assert alternates(h_es)["en"] == canon(h_en)
    assert alternates(h_en)["es"] == canon(h_es)


def test_spanish_and_english_pages_are_not_the_same_document():
    """A page that never switched language would pass every href check above, so compare the bodies."""
    for path, lang in public_pages():
        _head, html = head(path)
        alt_href = re.search(r'class="langswitch" href="([^"]+)"', html)
        if not alt_href:
            continue
        other = fresh().get(alt_href.group(1)).get_data(as_text=True)
        body = lambda s: re.sub(r"\s+", " ", s.split("<body", 1)[-1])[:4000]
        assert body(html) != body(other), f"{path} ({lang}) renders identically to its other-language page"


def test_no_template_leaks_into_rendered_output():
    for path, _lang in public_pages():
        _h, html = head(path)
        for bad in ("{{", "{%", "None</", "undefined</", "&lt;script"):
            assert bad not in html, f"{path}: leaked {bad!r}"


def test_language_choice_sticks_across_pages():
    c = fresh()
    c.get("/?lang=en")                                  # explicit choice -> cookie
    h = c.get("/check").get_data(as_text=True)
    assert re.search(r'<html[^>]*lang="en"', h)
    h = c.get("/aprende").get_data(as_text=True)        # a Spanish-path page stays Spanish regardless
    assert re.search(r'<html[^>]*lang="es"', h)


def test_sitemap_lists_both_languages_with_their_twins():
    """A bilingual site that lists only one language in its sitemap is invisible in the other one."""
    xml = fresh().get("/sitemap.xml").get_data(as_text=True)
    locs = re.findall(r"<loc>([^<]+)</loc>", xml)
    assert locs, "empty sitemap"
    assert any(u.endswith("/?lang=en") for u in locs), "English home missing from the sitemap"
    assert any(u.endswith("/check?a=es_check") for u in locs), "Spanish check missing from the sitemap"
    assert "/c/" not in xml, "campaign pages redirect to the check and must not be listed"
    # every entry declares itself and (where a twin exists) the other language
    for block in xml.split("<url>")[1:]:
        self_lang = re.search(r'hreflang="(\w+)" href="([^"]+)"', block)
        assert self_lang, block
        assert self_lang.group(2) in block.split("<xhtml:link")[0], "self alternate must be the loc"
        langs = set(re.findall(r'hreflang="(\w+)"', block))
        assert langs in ({"en"}, {"es"}, {"en", "es"}), (langs, block)


def test_home_switch_follows_the_language_actually_rendered():
    """Bare / is served in the visitor's PREFERRED language, so the header switch has to be built from
    what was rendered, not from the path. An English browser at / was offered '/?lang=en' — the page it
    was already on. It must reach the Spanish home instead, and a Spanish browser the English twin; the
    explicit ?lang= is what beats the sticky one-year le_lang cookie."""
    for header, expect_lang, expect_href in (("en-US,en", "en", "/?lang=es"),
                                             ("es-MX,es", "es", "/?lang=en")):
        body = fresh().get("/", headers={"Accept-Language": header}).get_data(as_text=True)
        assert f'<html lang="{expect_lang}"' in body, header
        assert f'<a class="langswitch" href="{expect_href}"' in body, header
    # an explicit choice wins over the browser, and keeps its canonical twin (never a self-link)
    body = fresh().get("/?lang=en", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert '<html lang="en"' in body and '<a class="langswitch" href="/"' in body
    body = fresh().get("/?lang=es", headers={"Accept-Language": "en"}).get_data(as_text=True)
    assert '<html lang="es"' in body and '<a class="langswitch" href="/?lang=en"' in body
