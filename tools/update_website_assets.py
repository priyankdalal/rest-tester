"""Refresh generated screenshot dimensions, sitemap and published handbook."""

import argparse
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import struct
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
BASE = "https://priyankdalal.github.io/rest-tester/"
SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
IMAGE_NS = "http://www.google.com/schemas/sitemap-image/1.1"


class Images(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.images: list[dict[str, str]] = []

    def handle_starttag(self, tag, attrs) -> None:
        if tag == "img":
            self.images.append({key: value or "" for key, value in attrs})


def refresh_dimensions(match: re.Match[str]) -> str:
    tag = match.group()
    parser = Images()
    parser.feed(tag)
    src = parser.images[0].get("src", "")
    if not src.startswith("images/") or not src.endswith(".png"):
        return tag
    with (DOCS / src).open("rb") as stream:
        header = stream.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Not a PNG: {src}")
    width, height = struct.unpack(">II", header[16:24])
    for attribute, value in (("width", width), ("height", height)):
        if not re.search(rf'\b{attribute}="\d+"', tag):
            raise ValueError(f"Missing {attribute}: {src}")
        tag = re.sub(rf'\b{attribute}="\d+"', f'{attribute}="{value}"', tag)
    return tag


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    pages = sorted(path for path in DOCS.glob("*.html") if path.name != "404.html")
    ET.register_namespace("", SITEMAP_NS)
    ET.register_namespace("image", IMAGE_NS)
    sitemap = ET.Element(f"{{{SITEMAP_NS}}}urlset")
    priorities = {"index.html": "1.0", "download.html": "0.8", "about.html": "0.6"}
    for page in pages:
        original = page.read_text(encoding="utf-8")
        html = re.sub(r"<img\b[^>]*>", refresh_dimensions, original)
        if html != original:
            page.write_text(html, encoding="utf-8")
        url = ET.SubElement(sitemap, f"{{{SITEMAP_NS}}}url")
        loc = BASE + ("" if page.name == "index.html" else page.name)
        for key, value in (
            ("loc", loc), ("lastmod", args.date.isoformat()),
            ("changefreq", "yearly" if page.name == "about.html" else "monthly"),
            ("priority", priorities.get(page.name, "0.9")),
        ):
            ET.SubElement(url, f"{{{SITEMAP_NS}}}{key}").text = value
        images = Images()
        images.feed(html)
        for image in images.images:
            if not image.get("src", "").startswith("images/"):
                continue
            element = ET.SubElement(url, f"{{{IMAGE_NS}}}image")
            ET.SubElement(element, f"{{{IMAGE_NS}}}loc").text = BASE + image["src"]
            ET.SubElement(element, f"{{{IMAGE_NS}}}title").text = image["alt"]
    ET.indent(sitemap, space="  ")
    ET.ElementTree(sitemap).write(DOCS / "sitemap.xml", encoding="UTF-8", xml_declaration=True)
    guides = DOCS / "guides"
    guides.mkdir(exist_ok=True)
    shutil.copyfile(
        ROOT / "roadmaps" / "ai-integration" / "AI-Implementation-Guide.pdf",
        guides / "AI-Implementation-Guide.pdf",
    )
    print(f"Updated {len(pages)} pages, sitemap and published handbook.")


if __name__ == "__main__":
    main()
