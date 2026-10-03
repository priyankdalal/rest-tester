"""Static checks for the GitHub Pages website in ``docs/``."""

from __future__ import annotations

import json
import struct
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urldefrag, urlparse

import pytest

DOCS = Path(__file__).resolve().parent.parent / "docs"
BASE_URL = "https://priyankdalal.github.io/rest-tester/"
SITE_PREFIX = "/rest-tester/"
INDEXABLE = ["index.html", "features.html", "docs.html", "ai.html", "download.html", "about.html"]
ALL_PAGES = INDEXABLE + ["404.html"]


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self.links: list[tuple[str, dict[str, str]]] = []
        self.refs: list[str] = []
        self.images: list[dict[str, str]] = []
        self.ids: set[str] = set()
        self.h1 = 0
        self.json_ld: list[str] = []
        self.html_lang = ""
        self._in_title = False
        self._in_ld = False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if "id" in a:
            self.ids.add(a["id"])
        if tag == "html":
            self.html_lang = a.get("lang", "")
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = a.get("name") or a.get("property")
            if key:
                self.meta[key] = a.get("content", "")
        elif tag == "link":
            self.links.append((a.get("rel", ""), a))
            self.refs.append(a.get("href", ""))
        elif tag == "a":
            self.refs.append(a.get("href", ""))
        elif tag == "script":
            if a.get("type") == "application/ld+json":
                self._in_ld = True
                self.json_ld.append("")
            elif "src" in a:
                self.refs.append(a["src"])
        elif tag == "img":
            self.images.append(a)
            self.refs.append(a.get("src", ""))
        elif tag == "h1":
            self.h1 += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_ld = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_ld:
            self.json_ld[-1] += data


def _parse(name: str) -> _Page:
    page = _Page()
    page.feed((DOCS / name).read_text(encoding="utf-8"))
    return page


def _png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    return struct.unpack(">II", header[16:24])


def _local_target(page: str, ref: str) -> tuple[Path, str] | None:
    """Resolve a site-local reference to (file, fragment); ``None`` for external links."""
    if not ref or ref.startswith(("http://", "https://", "mailto:")):
        return None
    path, fragment = urldefrag(ref)
    if path.startswith(SITE_PREFIX):
        path = path[len(SITE_PREFIX):]
    elif path.startswith("/"):
        pytest.fail(f"{page}: root-relative link {ref!r} breaks on a project site")
    if path in ("", "./"):
        path = page if ref.startswith("#") else "index.html"
    return DOCS / path, fragment


@pytest.mark.parametrize("name", ALL_PAGES)
def test_local_links_and_assets_resolve(name):
    page = _parse(name)
    for ref in page.refs:
        target = _local_target(name, ref)
        if target is None:
            continue
        path, fragment = target
        assert path.is_file(), f"{name}: {ref!r} does not exist"
        if fragment and path.suffix == ".html":
            ids = page.ids if path.name == name else _parse(path.name).ids
            assert fragment in ids, f"{name}: anchor {ref!r} not found"


@pytest.mark.parametrize("name", ALL_PAGES)
def test_images_have_alt_and_real_dimensions(name):
    for img in _parse(name).images:
        src = img.get("src", "")
        assert "alt" in img, f"{name}: {src} has no alt attribute"
        assert img.get("width") and img.get("height"), f"{name}: {src} has no width/height"
        target = _local_target(name, src)
        if target and target[0].suffix == ".png":
            real_w, real_h = _png_size(target[0])
            width, height = int(img["width"]), int(img["height"])
            if src.startswith("images/"):
                assert (real_w, real_h) == (width, height), (
                    f"{name}: {src} width/height attributes do not match the file"
                )
            else:
                assert real_w * height == real_h * width, f"{name}: {src} aspect ratio differs"
                assert real_w >= width, f"{name}: {src} is upscaled"
        if src.startswith("images/"):
            assert img["alt"].strip(), f"{name}: screenshot {src} needs descriptive alt text"
            assert img.get("loading") == "lazy", f"{name}: {src} should be lazy-loaded"


@pytest.mark.parametrize("name", INDEXABLE)
def test_seo_metadata(name):
    page = _parse(name)
    canonical = BASE_URL + ("" if name == "index.html" else name)
    assert page.html_lang == "en"
    assert page.h1 == 1, f"{name}: expected exactly one <h1>, found {page.h1}"
    assert 20 <= len(page.title.strip()) <= 80, f"{name}: title length {len(page.title)}"
    assert 70 <= len(page.meta.get("description", "")) <= 320
    assert [a["href"] for rel, a in page.links if rel == "canonical"] == [canonical]
    assert page.meta.get("og:url") == canonical
    for key in ("og:title", "og:description", "og:image", "twitter:card", "twitter:image"):
        assert page.meta.get(key), f"{name}: missing {key}"
    assert page.meta["og:image"].startswith(BASE_URL)
    assert "noindex" not in page.meta.get("robots", "")
    assert page.json_ld, f"{name}: no JSON-LD"
    for block in page.json_ld:
        json.loads(block)


def test_titles_and_descriptions_are_unique():
    pages = [_parse(name) for name in INDEXABLE]
    assert len({p.title for p in pages}) == len(pages)
    assert len({p.meta["description"] for p in pages}) == len(pages)


@pytest.mark.parametrize("name", ALL_PAGES)
def test_ai_guide_is_discoverable(name):
    assert any(ref.endswith("ai.html") for ref in _parse(name).refs)


def test_ai_guide_covers_workflows_and_publishes_handbook():
    page = _parse("ai.html")
    assert {"setup", "request", "suite", "data", "load", "privacy", "tokens",
            "activity", "troubleshooting", "developer"} <= page.ids
    published = DOCS / "guides" / "AI-Implementation-Guide.pdf"
    source = DOCS.parent / "roadmaps" / "ai-integration" / published.name
    assert published.read_bytes().startswith(b"%PDF-")
    assert published.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("name", ALL_PAGES)
def test_website_omits_filter_sort_grammar_and_builder_screenshots(name):
    page = _parse(name)
    assert "grammar" not in page.ids
    assert not any(ref.endswith("#grammar") for ref in page.refs)
    assert not any(image.get("src") in {"images/query-builder.png", "images/sort-builder.png"}
                   for image in page.images)


def test_not_found_page_is_not_indexed():
    page = _parse("404.html")
    assert "noindex" in page.meta.get("robots", "")
    assert page.h1 == 1


def test_sitemap_lists_every_page_and_screenshot():
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9",
          "i": "http://www.google.com/schemas/sitemap-image/1.1"}
    root = ET.parse(DOCS / "sitemap.xml").getroot()
    urls = {u.findtext("s:loc", namespaces=ns): u for u in root.findall("s:url", ns)}
    expected = {BASE_URL + ("" if n == "index.html" else n): n for n in INDEXABLE}
    assert set(urls) == set(expected)
    for loc, name in expected.items():
        listed = {i.findtext("i:loc", namespaces=ns) for i in urls[loc].findall("i:image", ns)}
        used = {BASE_URL + img["src"] for img in _parse(name).images if img["src"].startswith("images/")}
        assert listed == used, f"sitemap images for {name} are out of date"
    for loc in (i.text for i in root.iter(f"{{{ns['i']}}}loc")):
        assert (DOCS / urlparse(loc).path.removeprefix(SITE_PREFIX)).is_file(), loc


def test_json_ld_screenshots_exist():
    graph = json.loads(_parse("index.html").json_ld[0])["@graph"]
    app = next(node for node in graph if node["@type"] == "SoftwareApplication")
    shots = app["screenshot"]
    assert shots
    for shot in shots:
        url = shot if isinstance(shot, str) else shot.get("url") or shot.get("contentUrl")
        assert url.startswith(BASE_URL)
        assert (DOCS / url.removeprefix(BASE_URL)).is_file(), url


def test_supporting_files():
    assert (DOCS / ".nojekyll").is_file()
    assert f"Sitemap: {BASE_URL}sitemap.xml" in (DOCS / "robots.txt").read_text(encoding="utf-8")
    manifest = json.loads((DOCS / "site.webmanifest").read_text(encoding="utf-8"))
    for icon in manifest["icons"]:
        size = int(icon["sizes"].split("x")[0])
        assert _png_size(DOCS / icon["src"]) == (size, size)
    assert _png_size(DOCS / "assets" / "og-image.png") == (1200, 630)


def test_site_is_unbranded():
    for path in DOCS.rglob("*"):
        if path.suffix in {".html", ".css", ".js", ".xml", ".txt", ".webmanifest", ".md"}:
            text = path.read_text(encoding="utf-8").lower()
            for word in ("trialwyze", "agtech", "onfarm"):
                assert word not in text, f"{path.name} mentions {word}"
