"""The page's files: what index.html and its modules load, and the elements the scripts look up."""
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from adsb_approach.web.app import STATIC_DIR, create_app
from tests.conftest import ROOT

ELEMENT_IDS = ("banner", "stream-state", "coverage-link", "search-link", "live", "live-empty", "filters", "window",
               "attention", "rotorcraft", "ga", "clear-filters", "log", "log-empty", "more", "drawer", "drawer-title",
               "drawer-close", "drawer-attempts", "drawer-tar1090", "drawer-globe", "drawer-external", "drawer-norm",
               "chart-height", "chart-cross", "chart-cross-title", "chart-speed", "drawer-gates", "drawer-metar",
               "approach-kind", "drawer-path", "drawer-path-chip", "drawer-path-source")
COVERAGE_IDS = ("stats-note", "span", "show-quiet", "coverage-charts", "quiet-note", "marks", "outcome-charts",
                "stats-empty", "stats-error", "arrivals-link", "search-link")
# The approach drawer the search page copies from index.html, and the grey-row drawer beside it.
DRAWER_IDS = tuple(i for i in ELEMENT_IDS if i.startswith(("drawer", "chart-"))) + ("drawer-emergency",)
SEARCH_IDS = ("q", "window", "from", "to", "search-form", "counts", "results", "results-empty", "more", *DRAWER_IDS,
              "flight-drawer", "flight-title", "flight-close", "flight-heard", "flight-emergency", "flight-tar1090",
              "flight-globe", "flight-facts")
CONTENT_TYPES = {".js": "text/javascript", ".css": "text/css"}
PAGES = ("/", "/coverage", "/search")
LINKS = {"coverage": "/coverage", "./": "/", "search": "/search"}    # the pages' links to each other: routes, not files
# The footer's ODbL notice: the database and the licence, the only links to another site, and never loaded.
CREDITS = ("https://adsb.lol/", "https://opendatacommons.org/licenses/odbl/1-0/")
PAGE_FILES = {"/": {"static/app.js", "static/logic.js", "static/dom.js", "static/style.css",
                    "static/vendor/uPlot.iife.min.js"},
              "/coverage": {"static/coverage.js", "static/coveragelogic.js", "static/logic.js", "static/style.css",
                            "static/vendor/uPlot.iife.min.js"},
              "/search": {"static/search.js", "static/searchlogic.js", "static/logic.js", "static/dom.js", "static/drawer.js",
                          "static/style.css", "static/vendor/uPlot.iife.min.js"}}


@pytest.fixture(scope="module")
def client(replay_db, example_cfg):
    return TestClient(create_app(example_cfg, replay_db))


@pytest.mark.parametrize("page", PAGES)
def test_the_page_loads_only_its_own_files_and_each_is_served(client, page):
    wanted = [url for url in re.findall(r'(?:src|href)="([^"]+)"', client.get(page).text) if url not in CREDITS]
    assert wanted and not [url for url in wanted if re.match(r"[a-z]+:|/", url)], wanted     # relative: no other site
    seen: set[str] = set()
    while wanted:
        url = wanted.pop()
        if url in seen:
            continue
        seen.add(url)
        if url in LINKS:                          # the other page: a route serving HTML, not a file
            other = client.get(LINKS[url])
            assert other.status_code == 200 and other.headers["content-type"].startswith("text/html"), url
            continue
        r = client.get("/" + url)
        assert r.status_code == 200, url
        suffix = url[url.rfind("."):]
        assert r.headers["content-type"].startswith(CONTENT_TYPES[suffix]), (url, r.headers["content-type"])
        if suffix == ".js" and "/vendor/" not in url:                  # a module's own imports, one per line
            folder = url.rsplit("/", 1)[0]
            wanted += [f"{folder}/{m[2:]}" for m in re.findall(r'^import [^"\n]*"(\./[^"]+)";$', r.text, re.MULTILINE)]
    assert PAGE_FILES[page] <= seen


@pytest.mark.parametrize("page", PAGES)
def test_every_page_carries_the_odbl_notice_for_adsb_lol_data(client, page):
    """ODbL 1.0 §4.3: a public page drawn from adsb.lol's data says so and that the data is available under the
    licence, naming both with a link. The licence's own example wording satisfies it, so the footer uses that."""
    footer = re.search(r'<footer class="credits">(.*?)</footer>', client.get(page).text, re.S)
    assert footer
    assert " ".join(re.sub(r"<[^>]+>", "", footer[1]).split()) == (
        "Contains information from adsb.lol, which is made available here under the Open Database License (ODbL).")
    assert tuple(re.findall(r'href="([^"]+)"', footer[1])) == CREDITS


def test_the_page_has_every_element_the_scripts_look_up(client):
    page = client.get("/").text
    assert [i for i in ELEMENT_IDS if f'id="{i}"' not in page] == []


def test_each_pages_script_only_looks_up_elements_its_page_has(client):
    """app.js and search.js name their elements by id: one that a page lacks is a null the first use of it throws on."""
    for page, script in (("/", "app.js"), ("/search", "search.js")):
        markup = client.get(page).text
        used = set(re.findall(r'\$\("([\w-]+)"\)', (STATIC_DIR / script).read_text()))
        assert used and [i for i in sorted(used) if f'id="{i}"' not in markup] == [], (page, script)


def test_the_approach_filter_offers_all_straight_in_and_path(client):
    """The log's Approach filter, beside the GA checkbox and in the filter form."""
    page = client.get("/").text
    form = re.search(r'<form id="filters".*?</form>', page, re.S)[0]
    label = re.search(r'<label[^>]*>\s*Approach\s*<select id="approach-kind">(.*?)</select>\s*</label>', form, re.S)
    assert label, "an Approach label around the select"
    options = re.findall(r'<option value="(\w+)"(?: selected)?>([^<]+)</option>', label[1])
    assert options == [("all", "All"), ("straight", "Straight-in"), ("path", "Path")]
    assert re.search(r'<option value="all" selected>', label[1])                      # the default view shows everything
    assert form.index('id="ga"') < form.index('id="approach-kind"') < form.index('id="clear-filters"')


def test_the_drawer_has_a_note_for_the_path_and_a_title_for_the_cross_track_chart(client):
    for page in ("/", "/search"):
        markup = client.get(page).text
        assert re.search(r'<p id="drawer-path"[^>]*\bhidden\b[^>]*>\s*<span id="drawer-path-chip"[^>]*></span>'
                         r'\s*<span id="drawer-path-source"[^>]*></span>\s*</p>', markup), page
        assert re.search(r'<figcaption id="chart-cross-title">Cross-track from the centreline \(ft\)</figcaption>',
                         markup), page


def test_the_coverage_page_has_every_element_its_script_looks_up(client):
    page = client.get("/coverage").text
    assert [i for i in COVERAGE_IDS if f'id="{i}"' not in page] == []


def test_the_search_page_has_every_element_its_script_looks_up(client):
    page = client.get("/search").text
    assert [i for i in SEARCH_IDS if f'id="{i}"' not in page] == []


def test_page_scripts_never_use_innerhtml():
    """Callsigns, registrations and labels are API strings: the scripts build every node with createElement and
    textContent. The property access is the hazard; comments may still name it."""
    scripts = sorted(p for p in STATIC_DIR.glob("*.js"))
    assert {"app.js", "drawer.js", "live.js", "log.js", "logic.js"} <= {p.name for p in scripts}
    assert [p.name for p in scripts if re.search(r"\.innerHTML", p.read_text())] == []


def test_the_live_cards_fields_are_the_apis_in_the_same_order():
    """logic.js keeps its own list of the fields a live card takes from an event; web/live.py's list is what /api/live
    sends. One missing from the page's list is dropped from every card the event stream builds or updates (`path` and
    `path_label` were, until they were added to both)."""
    from adsb_approach.web.live import CARD_FIELDS
    listed = re.search(r"export const CARD_FIELDS = \[(.*?)\];", (STATIC_DIR / "logic.js").read_text(), re.S)
    assert listed and re.findall(r'"(\w+)"', listed[1]) == list(CARD_FIELDS)


def test_uplot_is_vendored_with_its_licence():
    vendor = STATIC_DIR / "vendor"
    assert (vendor / "uPlot.iife.min.js").stat().st_size > 10_000 and (vendor / "uPlot.min.css").stat().st_size > 500
    assert "MIT License" in (vendor / "uPlot.LICENSE").read_text()


def test_the_javascript_unit_tests_pass():
    """logic.js has no Python tests of its own, so `uv run pytest` runs its coverage here too. A bare directory
    positional argument crashes this machine's Node (v25.2.1: MODULE_NOT_FOUND, treating the directory as the main
    script instead of a test root), so the test files are passed as an explicit glob instead of `tests/js/`."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    files = sorted(str(p) for p in (ROOT / "tests" / "js").glob("*.test.mjs"))
    result = subprocess.run([node, "--test", *files], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


ON_CARD = (".card", ".chip", ".attempt", ".flag", ".stream-state")     # drawn on --card unless they set a background


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})\b", block))


def _colour(value: str, palette: dict[str, str], default: str) -> str:
    """A declaration's colour in a palette: a custom property, a hex literal, or `default` for inherit and none."""
    value = value.strip()
    if var := re.fullmatch(r"var\((--[\w-]+)\)", value):
        return palette[var[1]]
    if re.fullmatch(r"#[0-9a-fA-F]{3}", value):
        return "#" + "".join(c * 2 for c in value[1:])
    return value if re.fullmatch(r"#[0-9a-fA-F]{6}", value) else default


def _contrast(a: str, b: str) -> float:
    def luminance(colour: str) -> float:
        channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_every_text_colour_has_aa_contrast_in_light_and_dark():
    """Each rule that sets a text colour, against what it is drawn on (its own background; a card's for the card,
    chip, attempt, flag and stream-state rules; otherwise the page's), reaches WCAG AA's 4.5:1 in both colour
    schemes. White on the dark scheme's light fills was 1.7:1 on the warning banner."""
    css = re.sub(r"/\*.*?\*/", "", (STATIC_DIR / "style.css").read_text(), flags=re.S)
    light_block = re.search(r"^:root\s*\{([^}]*)\}", css, re.M)
    dark_block = re.search(r"prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}", css)
    assert light_block and dark_block
    light = _tokens(light_block[1])
    dark = light | _tokens(dark_block[1])
    low = []
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        selector, decls = selector.strip(), dict(re.findall(r"([\w-]+):\s*([^;]+);", body))
        if selector == ":root" or "color" not in decls:
            continue
        for scheme, p in (("light", light), ("dark", dark)):
            ground = _colour(decls.get("background", ""), p, p["--card"] if selector.startswith(ON_CARD) else p["--bg"])
            ratio = _contrast(_colour(decls["color"], p, p["--fg"]), ground)
            if ratio < 4.5:
                low.append((scheme, selector, round(ratio, 2)))
    assert low == []
