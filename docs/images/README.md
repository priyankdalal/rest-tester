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
| `index.html` | Home page: hero, gallery, workspaces, filter grammar |
| `features.html` | Feature tour, one section per workspace |
| `docs.html` | Full documentation and FAQ (FAQPage structured data) |
| `download.html` | Install, demo API and standalone build (HowTo structured data) |
| `about.html` | Principles, technology, license and contributing |
| `404.html` | Not-found page (`noindex`) |
| `styles.css`, `script.js` | Shared design system and behaviour; no build step and no framework |
| `assets/` | Favicons, logo, web app icons and the 1200×630 social preview image |
| `images/` | Screenshots used by both the README and the website |

## Screenshot rules

- Capture at the same window size as the existing images (most are 1800×1125) so the gallery stays aligned.
- Every `<img>` on the site has `width` and `height` attributes matching the file's real pixel size. This
  prevents layout shift. Update them whenever a screenshot is replaced with one of a different size.
- Give every screenshot descriptive `alt` text. The sitemap reuses it as the image title.
- Use `loading="lazy"` and `decoding="async"` on every image below the fold.
- `assets/og-image.png` is the social preview. It is a 1200×630 crop of `api-explorer.png`; regenerate it when
  the API Explorer changes noticeably.

## Keep in sync

When you add, remove or rename a page or screenshot, update:

- `sitemap.xml`, including its `<image:image>` entries;
- the `screenshot` list in the `SoftwareApplication` JSON-LD in `index.html`;
- the README gallery, if the screenshot appears there.

The test `tests/test_website.py` checks all of this: local links and images resolve, image sizes match their
attributes, each page has a title, description, canonical URL, Open Graph tags, valid JSON-LD and exactly one
`<h1>`, and the sitemap lists every indexable page and image.
