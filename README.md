[![Generate project pages](https://github.com/mergedeyes/mergedeyes.github.io/actions/workflows/generate-project-pages.yml/badge.svg?event=push)](https://github.com/mergedeyes/mergedeyes.github.io/actions/workflows/generate-project-pages.yml)
[![Deploy Jekyll with GitHub Pages](https://github.com/mergedeyes/mergedeyes.github.io/actions/workflows/jekyll-gh-pages.yml/badge.svg)](https://github.com/mergedeyes/mergedeyes.github.io/actions/workflows/jekyll-gh-pages.yml)
[![Update visitor count](https://github.com/mergedeyes/mergedeyes.github.io/actions/workflows/update-visitor-count.yml/badge.svg)](https://github.com/mergedeyes/mergedeyes.github.io/actions/workflows/update-visitor-count.yml)

# mergedcloud.de

Source for my personal site, **[mergedcloud.de](https://mergedcloud.de/)** — a systems engineer / Rust developer portfolio: projects, work history, skills, and a bit about me.

## Stack

Plain HTML, CSS, and JavaScript. No framework, no build step, no bundler, no npm install. Fonts are system stacks, not web fonts. The only thing that leaves the browser at all is one analytics script (GoatCounter, for the visit counter) — everything else, including that counter's own numbers, is baked into the static HTML at commit time rather than fetched client-side.

## Structure

- `index.html`, `styles.css`, `script.js` — the whole site: hero, projects, work, education, skills, about, contact.
- `skills/*.html` — hand-written pages for a handful of skills, linked from chips in the skills section.
- `visitors.html`, `visitors.js` — a visits-per-day chart (plain inline SVG, no charting library).
- `404.html` — not found page.
- `sitemap.xml`, `robots.txt`, `llms.txt` — SEO and AI-crawler discoverability.
- `projects/*.html` — one page per project card, **auto-generated, never hand-edited** (see below).
- `.github/workflows/`, `.github/scripts/` — the automation that keeps the generated pages and stats current.

## Automation

Three GitHub Actions workflows do the recurring work:

- **Generate project pages** (`generate-project-pages.yml`, on push / daily / on demand) — for every project card in `index.html` with a GitHub "Repository" link, builds a page from that repo's README (fetched via the GitHub API), an optional syntax-highlighted source browser (opt-in per card via `data-source-file="..."`), and a full commit history — one card per commit, linking straight out to GitHub. A small build cache (`.github/data/project-pages-cache.json`) means a repo that hasn't been pushed to since the last run gets skipped entirely rather than re-fetched, and a repo that *did* change only re-fetches the individual files and commits that actually moved.
- **Update visitor count** (`update-visitor-count.yml`, every 30 min) — pulls totals and daily stats from GoatCounter and patches them straight into `index.html`'s footer and `visitors.html`'s chart data.
- **Deploy Jekyll with GitHub Pages** (`jekyll-gh-pages.yml`) — builds and publishes the site once the project-pages workflow finishes.

Running the project-page generator yourself needs Python 3 and `pip install requests beautifulsoup4 pygments`, plus a `GITHUB_TOKEN` in the environment (raises the GitHub API rate limit; the workflow gets this automatically, a local run needs it set by hand).

## Local preview

Serve the folder rather than opening `index.html` directly — some browsers apply strict MIME-type checks to a `file://` stylesheet and silently refuse to apply it:

```sh
python3 -m http.server
```

then open `http://localhost:8000/`.
