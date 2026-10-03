# Website and screenshots

The `docs/` folder is both the screenshot store for the main [README](../../README.md) and the source of the
project website, published with GitHub Pages.

## Publishing

1. Push the branch that contains `docs/` to GitHub.
2. Open **Settings → Pages**.
3. Under **Build and deployment**, choose **Deploy from a branch**, select the branch and the `/docs` folder,
   and save.
4. The site is published at `https://priyankdalal.github.io/rest-tester/` within a minute or two.

`docs/.nojekyll` turns off Jekyll so files are served as-is.

### Changing the site URL

Absolute URLs are needed for SEO, so the base URL `https://priyankdalal.github.io/rest-tester/` appears in:

- each page's `<link rel="canonical">`, `og:url`, `og:image` and `twitter:image` tags, and its JSON-LD;
- `sitemap.xml` and `robots.txt`;
- `404.html`, which uses root-relative `/rest-tester/...` paths because GitHub serves it at any missing URL.

When you fork the project or move to a custom domain, search and replace the base URL in `docs/`. For a custom
domain, also replace `/rest-tester/` in `404.html` with `/` and add a `CNAME` file.

After publishing, submit `sitemap.xml` in Google Search Console and Bing Webmaster Tools. Crawlers only read
`robots.txt` at the root of a host, so for a project site under `github.io` the sitemap must be submitted
manually.

## Site structure

| File | Purpose |
|---|---|
| `index.html` | Home page: hero, gallery and workspaces |
| `features.html` | Feature tour, one section per workspace |
| `docs.html` | Full documentation and FAQ (FAQPage structured data) |
| `ai.html` | Dedicated AI guide: setup, four modes, privacy, tokens, diagnostics and troubleshooting |
| `guides/AI-Implementation-Guide.pdf` | Published copy of the illustrated implementation handbook |
| `download.html` | Install, demo API and standalone build (HowTo structured data) |
| `about.html` | Principles, technology, license and contributing |
| `404.html` | Not-found page (`noindex`) |
| `styles.css`, `script.js` | Shared design system and behaviour; no build step and no framework |
| `assets/` | Favicons, logo, web app icons and the 1200×630 social preview image |
| `images/` | Screenshots used by both the README and the website |

## Screenshot rules

- Use a consistent size per screen family: the refreshed Explorer is 1440×1000, builders 1160×540,
  AI composer 1000×900 and AI Settings/Usage 1100×760. Older workspace captures retain their original sizes.
- Every `<img>` on the site has `width` and `height` attributes matching the file's real pixel size. This
  prevents layout shift. Update them whenever a screenshot is replaced with one of a different size.
- Give every screenshot descriptive `alt` text. The sitemap reuses it as the image title.
- Use `loading="lazy"` and `decoding="async"` on every image below the fold.
- `assets/og-image.png` is the social preview. It is a 1200×630 crop of `api-explorer.png`; regenerate it when
  the API Explorer changes noticeably.

### Recreate the current captures

The 3 October 2026 refresh covers Explorer (light, dark and disabled state), filter/sort builders,
all four AI modes, Activity, named provider settings and usage. AI plans and token counts are scripted
offline examples, not live model output or execution results. Never capture real secrets or confidential data.

From the repository root, with the application's Python environment:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
python -m tools.capture_documentation
python -m tools.update_website_assets --date 2026-10-03
python -m pytest tests/test_website.py -n 4 --dist loadfile
```

The capture tool uses temporary settings/databases, the demo Store catalog and a fake provider.
It also refreshes the 1200×630 social image. The metadata tool updates HTML image dimensions,
regenerates the sitemap and copies the handbook into the Pages-served `docs/guides/` directory.
Use the actual refresh date for later updates. It is not necessary to run a demo server.

The website and repository README intentionally omit the filter/sort grammar section and builder
screenshots. The image files remain as capture assets; do not re-add them to either gallery.

## Keep in sync

When you add, remove or rename a page or screenshot, update:

- `sitemap.xml`, including its `<image:image>` entries;
- the `screenshot` list in the `SoftwareApplication` JSON-LD in `index.html`;
- the README gallery, if the screenshot appears there.

The test `tests/test_website.py` checks all of this: local links and images resolve, image sizes match their
attributes, each page has a title, description, canonical URL, Open Graph tags, valid JSON-LD and exactly one
`<h1>`, and the sitemap lists every indexable page and image.

It also checks AI navigation on every page, coverage of all four workflows, and that the published PDF
matches its source artifact. When changing the base URL, update `tools/update_website_assets.py` and
`tests/test_website.py` as well so regeneration and validation retain the correct project prefix.
