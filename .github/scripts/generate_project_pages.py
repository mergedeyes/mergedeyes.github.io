#!/usr/bin/env python3
"""
Generates a standalone HTML page under /projects for every project card in
index.html that has a "Repository" link pointing at GitHub. Each page gets:
  - the project title
  - its README, fetched pre-rendered as HTML via the GitHub API
  - its tags (languages/stack) and license badge, copied from the card
  - a link back to the repository
  - for opted-in projects (data-source-file="..." on the card), one or more
    syntax-highlighted source files with line numbers. The attribute is a
    comma-separated list; each entry is either a literal file path or a
    "dir/*" wildcard that expands recursively to every file under that
    directory. One file -> a single wide source page, same as before.
    Two or more files -> a file-tree sidebar plus one page per file.
  - the site's own navbar (extracted from index.html, not hand-copied,
    so it can never drift out of sync with the real one)

Run from the repository root (that's how the workflow invokes it):
    python .github/scripts/generate_project_pages.py

Reads GITHUB_TOKEN from the environment to authenticate API calls (raises
the rate limit well past what four repos need, and would be required for
private repos - not the case here, but no reason not to use it).
"""
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_for_filename
from pygments.style import Style
from pygments.util import ClassNotFound
from pygments.token import Comment, Keyword, Name, Operator, Punctuation, Token

# NOTE: this file lives at .github/scripts/generate_project_pages.py, but
# it's invoked with the repo root as the working directory (see the
# workflow) - so SITE_ROOT must be Path.cwd(), NOT Path(__file__).parent
# (which would resolve to .github/scripts/ and never find index.html).
SITE_ROOT = Path.cwd()
INDEX_HTML = SITE_ROOT / "index.html"
PROJECTS_DIR = SITE_ROOT / "projects"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
# GITHUB_SHA is set automatically by every GitHub Actions run (no wiring
# needed) - using it as a cache-busting query string on styles.css means
# every commit that touches it automatically forces browsers to refetch
# the new version, instead of relying on manual hard-refreshes.
ASSET_VERSION = os.environ.get("GITHUB_SHA", "")[:8] or "dev"

AUTH_HEADERS = {"X-GitHub-Api-Version": "2022-11-28"}
if GITHUB_TOKEN:
    AUTH_HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"

API_HEADERS_JSON = {**AUTH_HEADERS, "Accept": "application/vnd.github+json"}
API_HEADERS_HTML = {**AUTH_HEADERS, "Accept": "application/vnd.github.html+json"}
API_HEADERS_RAW = {**AUTH_HEADERS, "Accept": "application/vnd.github.raw+json"}


class InstrumentPanelStyle(Style):
    """Matches the site's own palette (see styles.css :root) instead of
    importing a generic Pygments theme - keyword gold reuses --accent,
    strings/types reuse --signal teal, everything else stays within
    --text/--text-dim/--muted. No new hues introduced."""
    background_color = "#151922"  # --surface
    styles = {
        Token:                  "#AEB6C2",  # --text-dim (default)
        Comment:                "italic #7E8794",  # --muted
        Comment.Preproc:        "italic #7E8794",
        Keyword:                "bold #E3AC57",   # --accent
        Keyword.Constant:       "#E3AC57",
        Keyword.Declaration:    "bold #E3AC57",
        Keyword.Type:           "#5FB8A6",        # --signal
        Token.Literal.String:   "#5FB8A6",        # --signal
        Token.Literal.String.Escape: "bold #5FB8A6",
        Token.Literal.Number:   "#E9EDF2",        # --text
        Name:                   "#E9EDF2",
        Name.Function:          "#E9EDF2",
        Name.Class:             "bold #5FB8A6",
        Name.Builtin:           "#E3AC57",
        Name.Decorator:         "#E3AC57",
        Name.Attribute:         "#AEB6C2",
        Operator:               "#AEB6C2",
        Operator.Word:          "bold #E3AC57",
        Punctuation:            "#7E8794",
    }


def parse_github_repo(url: str):
    """Returns (owner, repo) for a github.com repo URL, or None if the URL
    isn't a github.com repo link (e.g. a live-demo link, which we should
    just ignore)."""
    parsed = urlparse(url)
    if parsed.netloc not in ("github.com", "www.github.com"):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    repo = re.sub(r"\.git$", "", repo)
    return owner, repo


def extract_projects(html: str):
    """Parses index.html and returns a list of project dicts, one per
    .card that has a Repository link. Cards without one are skipped
    entirely, per the brief ("every project card where there is a
    github repo link")."""
    soup = BeautifulSoup(html, "html.parser")
    projects = []

    for card in soup.select("#projects .cards > .card"):
        title_el = card.select_one(".card__title")
        title = title_el.get_text(strip=True) if title_el else None

        tags = [t.get_text(strip=True) for t in card.select(".card__head .tag")]

        repo_url = None
        for a in card.select(".card__links a, .card__foot > a"):
            if a.get_text(strip=True).lower() == "repository":
                repo_url = a.get("href")
                break

        if not repo_url:
            continue  # no repo link on this card -> nothing to generate

        parsed = parse_github_repo(repo_url)
        if not parsed:
            print(f"  ! '{title}': Repository link isn't a github.com URL ({repo_url}), skipping")
            continue
        owner, repo = parsed

        license_el = card.select_one(".license-badge")
        license_text = license_el.get_text(strip=True) if license_el else None

        raw_source = card.get("data-source-file")
        source_entries = [e.strip() for e in raw_source.split(",") if e.strip()] if raw_source else []

        projects.append({
            "title": title,
            "tags": tags,
            "owner": owner,
            "repo": repo,
            "repo_url": repo_url,
            "license": license_text,
            "source_entries": source_entries,
        })

    return projects


def fetch_readme_html(owner: str, repo: str) -> str:
    """Fetches the repo's README already rendered to GitHub-flavored HTML
    (no local markdown parser needed at all). Returns a friendly fallback
    message on any failure (404, no README, rate limit, etc.) rather than
    raising, so one broken repo doesn't fail the whole run."""
    url = f"https://api.github.com/repos/{owner}/{repo}/readme"
    try:
        resp = requests.get(url, headers=API_HEADERS_HTML, timeout=15)
    except requests.RequestException as e:
        return f"<p><em>Could not fetch README: {e}</em></p>"

    if resp.status_code == 404:
        return "<p><em>This repository doesn't have a README yet.</em></p>"
    if resp.status_code != 200:
        return f"<p><em>Could not fetch README (HTTP {resp.status_code}).</em></p>"

    return clean_readme_html(resp.text)


def get_default_branch(owner: str, repo: str):
    """Fetches the repo's default branch name. Needed only for the Git
    Trees API (used to resolve dir/* wildcards) - unlike the readme/
    contents endpoints, it has no implicit 'use the default branch'
    behavior and needs an explicit ref."""
    url = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        resp = requests.get(url, headers=API_HEADERS_JSON, timeout=15)
        resp.raise_for_status()
        return resp.json()["default_branch"]
    except (requests.RequestException, KeyError, ValueError):
        return None


def fetch_repo_file_tree(owner: str, repo: str):
    """Fetches the full recursive list of file paths (blobs only, no
    directories) in the repo's default branch. Used only to resolve
    dir/* wildcards in data-source-file - not called at all if every
    entry is a literal file path, to avoid the extra API calls."""
    branch = get_default_branch(owner, repo)
    if not branch:
        return []
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
    try:
        resp = requests.get(url, headers=API_HEADERS_JSON, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return []
    if data.get("truncated"):
        print(f"  ! Warning: {owner}/{repo}'s file tree was truncated by GitHub's API "
              f"(repo too large) - a dir/* wildcard may be missing some files.")
    return [item["path"] for item in data.get("tree", []) if item.get("type") == "blob"]


def expand_source_entries(owner: str, repo: str, entries: list) -> list:
    """Resolves data-source-file entries into concrete file paths. An
    entry ending in "/*" expands to every file under that directory,
    recursively; anything else is a literal file path. The first
    literal entry (if any) is left first in the result, so it becomes
    the "primary" file shown on the top-level <slug>-source.html page;
    wildcard-resolved files are appended after. De-duplicates so a file
    matched by both a literal entry and a wildcard only appears once."""
    literal_files = [e for e in entries if not e.endswith("/*")]
    wildcard_prefixes = [e[:-1] for e in entries if e.endswith("/*")]  # "src/*" -> "src/"

    resolved = list(literal_files)
    if wildcard_prefixes:
        all_files = fetch_repo_file_tree(owner, repo)
        for prefix in wildcard_prefixes:
            resolved.extend(f for f in all_files if f.startswith(prefix))

    seen = set()
    ordered = []
    for f in resolved:
        if f not in seen:
            seen.add(f)
            ordered.append(f)
    return ordered


def fetch_highlighted_source(owner: str, repo: str, path: str) -> str:
    """Fetches one file's raw content and returns it pre-highlighted as
    HTML (Pygments, at build time) - no client-side highlighter needed
    on the page. Falls back to plain (unhighlighted) text on any error,
    same graceful-degradation pattern as the README fetch."""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    try:
        resp = requests.get(url, headers=API_HEADERS_RAW, timeout=15)
    except requests.RequestException as e:
        return f"<p><em>Could not fetch {path}: {e}</em></p>"

    if resp.status_code != 200:
        return f"<p><em>Could not fetch {path} (HTTP {resp.status_code}).</em></p>"

    code = resp.text
    try:
        lexer = get_lexer_for_filename(path, code)
    except ClassNotFound:
        lexer = TextLexer()

    formatter = HtmlFormatter(cssclass="highlight", style=InstrumentPanelStyle, linenos="table")
    return highlight(code, lexer, formatter)


def clean_readme_html(raw_html: str) -> str:
    """Strips GitHub-page-only chrome (heading permalink icons, the
    outer #readme/article wrapper) so the markup drops cleanly into our
    own page template and styling instead of expecting GitHub's own CSS.

    Note: GitHub rewrites relative links/images in a rendered README to
    absolute repo URLs, so this doesn't need to handle relative paths
    itself - verified for text links; spot-check if a project's README
    ever adds relative images and they don't show up correctly.
    """
    soup = BeautifulSoup(raw_html, "html.parser")

    # Unwrap heading-permalink wrappers: <div class="markdown-heading">
    # <h2>Text</h2><a class="anchor">...</a></div> -> just <h2>Text</h2>
    for heading_wrap in soup.select("div.markdown-heading"):
        for anchor in heading_wrap.select("a.anchor"):
            anchor.decompose()
        heading_wrap.unwrap()

    # Any remaining stray anchor-permalink links outside that wrapper
    for anchor in soup.select("a.anchor"):
        anchor.decompose()

    # Unwrap the outer GitHub chrome; keep only the actual content
    article = soup.select_one("article.markdown-body") or soup.select_one("#readme") or soup
    return "".join(str(c) for c in article.contents) if hasattr(article, "contents") else str(article)


def extract_rail(html: str, up: str = "../") -> str:
    """Pulls the whole <header class="rail" id="rail"> nav block out of
    index.html so project pages share the exact same navigation - single
    source of truth, nothing to remember to keep in sync by hand.

    In-page anchor links (#projects, #top, etc.) only make sense on
    index.html itself; from a generated page they're rewritten to point
    back at index.html instead. `up` is how many directory levels to
    climb to reach the site root - "../" for pages directly under
    /projects/, "../../" for per-file pages nested under
    /projects/<slug>-source/.
    """
    soup = BeautifulSoup(html, "html.parser")
    rail = soup.select_one("header.rail#rail")
    if rail is None:
        return ""

    for a in rail.find_all("a", href=True):
        if a["href"].startswith("#"):
            a["href"] = f"{up}index.html{a['href']}"

    return str(rail)


def build_tree(paths: list) -> dict:
    """Turns a flat list of file paths into a nested dict tree, e.g.
    ["src/main.rs", "src/utils/a.rs"] ->
    {"src": {"main.rs": None, "utils": {"a.rs": None}}}.
    A leaf (file) maps to None; a directory maps to a dict of children."""
    root = {}
    for path in paths:
        parts = path.split("/")
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = None
    return root


def safe_filename(path: str) -> str:
    """Converts a repo-relative file path into a filesystem-safe filename
    for its generated page, e.g. "src/handlers/auth.rs" -> "src--handlers--auth.rs"."""
    return path.replace("/", "--")


def render_tree_html(tree: dict, prefix: str, current_path: str, base: str) -> str:
    """Recursively renders a file tree dict as nested <ul> HTML, with a
    link to each file's generated page. `base` is the relative path
    prefix needed to reach the <slug>-source/ directory from whichever
    page this call renders onto (see generate_source_pages)."""
    items = []
    dirs = sorted(k for k, v in tree.items() if isinstance(v, dict))
    files = sorted(k for k, v in tree.items() if v is None)

    for name in dirs:
        full = f"{prefix}{name}/"
        children_html = render_tree_html(tree[name], full, current_path, base)
        items.append(f'<li class="tree-dir"><span class="tree-dir__name">{name}/</span>{children_html}</li>')

    for name in files:
        full = f"{prefix}{name}"
        file_page = f"{base}{safe_filename(full)}.html"
        active = " tree-file--active" if full == current_path else ""
        items.append(f'<li class="tree-file{active}"><a href="{file_page}">{name}</a></li>')

    return f'<ul class="tree">{"".join(items)}</ul>'


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{title} — Jan</title>
  <link rel="stylesheet" href="../styles.css?v={asset_version}" />
</head>
<body>
  <a class="skip-link" href="#main">Skip to content</a>
  <div class="layout">
    {rail_html}
    <main class="main" id="main">
      <section class="section project-page">
        <p class="section__path"><a href="../index.html#projects" class="link">~/projects</a> / {repo}</p>
        <div class="card__head project-page__head">
          <h1 class="section__title">{title}</h1>
          {tags_html}
        </div>
        <div class="card__foot project-page__foot">
          <div class="card__links">
            <a href="{repo_url}" class="link">Repository</a>
            {source_link_html}
          </div>
          {license_html}
        </div>
        <div class="readme-content">
{readme_html}
        </div>
      </section>
    </main>
  </div>
</body>
</html>
"""

# Shared by both the top-level <slug>-source.html page and each per-file
# page under <slug>-source/ - {up}, {tree_html}, {project_href} differ by
# how deep the page sits (0 vs 1 extra directory level).
SOURCE_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{file_path} — {title} — Jan</title>
  <link rel="stylesheet" href="{up}styles.css?v={asset_version}" />
</head>
<body>
  <a class="skip-link" href="#main">Skip to content</a>
  <div class="layout">
    {rail_html}
    {tree_html}
    <main class="main" id="main">
      <section class="section source-page">
        <p class="section__path">
          <a href="{up}index.html#projects" class="link">~/projects</a> /
          <a href="{project_href}" class="link">{repo}</a> / source
        </p>
        <div class="source-page__head">
          <h1 class="section__title">{file_path}</h1>
          <a href="{project_href}" class="link">← Back to {title}</a>
        </div>
        {highlighted}
      </section>
    </main>
  </div>
</body>
</html>
"""


def render_page(project: dict, rail_html: str, has_source: bool) -> str:
    tags_html = "\n          ".join(f'<span class="tag">{t}</span>' for t in project["tags"])
    license_html = (
        f'<span class="license-badge">{project["license"]}</span>'
        if project["license"] else ""
    )
    readme_html = fetch_readme_html(project["owner"], project["repo"])

    source_link_html = ""
    if has_source:
        slug = project["repo"].lower()
        files = project["_resolved_files"]
        label = f"View source ({files[0]})" if len(files) == 1 else f"Browse source ({len(files)} files)"
        source_link_html = f'<a href="{slug}-source.html" class="link">{label}</a>'

    return PAGE_TEMPLATE.format(
        title=project["title"],
        repo=project["repo"],
        tags_html=tags_html,
        repo_url=project["repo_url"],
        license_html=license_html,
        readme_html=readme_html,
        rail_html=rail_html,
        source_link_html=source_link_html,
        asset_version=ASSET_VERSION,
    )


def render_source_file_page(project: dict, rail_html_for: dict, file_path: str, tree, up: str, project_href: str) -> str:
    """Renders one file's page - used both for the single-file case
    (up="../", project_href="<slug>.html") and for each file in the
    multi-file case (index page: up="../", tree base includes the
    subfolder; per-file pages: up="../../", tree base is empty since
    they're siblings)."""
    slug = project["repo"].lower()
    tree_base = "" if up == "../../" else f"{slug}-source/"
    tree_html = ""
    if tree is not None:
        rendered = render_tree_html(tree, "", file_path, base=tree_base)
        tree_html = f'<nav class="file-tree"><p class="file-tree__title">Files</p>{rendered}</nav>'

    highlighted = fetch_highlighted_source(project["owner"], project["repo"], file_path)
    return SOURCE_PAGE_TEMPLATE.format(
        title=project["title"],
        repo=project["repo"],
        file_path=file_path,
        rail_html=rail_html_for[up],
        tree_html=tree_html,
        up=up,
        project_href=project_href,
        highlighted=highlighted,
        asset_version=ASSET_VERSION,
    )


def generate_source_pages(project: dict, files: list, rail_html_top: str, rail_html_nested: str, out_dir: Path, site_root: Path) -> set:
    """Writes <slug>-source.html (and, for multiple files, <slug>-source/
    per-file pages plus a file-tree sidebar on every one of them). Returns
    the set of resolved paths it wrote, so main() can tell cleanup_stale_files
    which files in projects/ are still current and which are leftovers from
    a data-source-file that's since been removed or narrowed."""
    slug = project["repo"].lower()
    rail_html_for = {"../": rail_html_top, "../../": rail_html_nested}
    written = set()

    if len(files) == 1:
        page = render_source_file_page(
            project, rail_html_for, files[0], tree=None,
            up="../", project_href=f"{slug}.html",
        )
        out_path = out_dir / f"{slug}-source.html"
        out_path.write_text(page, encoding="utf-8")
        written.add(out_path.resolve())
        print(f"     -> source page -> {out_path.relative_to(site_root)}")
        return written

    tree = build_tree(files)
    primary = files[0]

    index_page = render_source_file_page(
        project, rail_html_for, primary, tree=tree,
        up="../", project_href=f"{slug}.html",
    )
    index_path = out_dir / f"{slug}-source.html"
    index_path.write_text(index_page, encoding="utf-8")
    written.add(index_path.resolve())
    print(f"     -> source index -> {index_path.relative_to(site_root)} ({len(files)} files)")

    sub_dir = out_dir / f"{slug}-source"
    sub_dir.mkdir(exist_ok=True)
    for file_path in files:
        page = render_source_file_page(
            project, rail_html_for, file_path, tree=tree,
            up="../../", project_href=f"../{slug}.html",
        )
        file_out = sub_dir / f"{safe_filename(file_path)}.html"
        file_out.write_text(page, encoding="utf-8")
        written.add(file_out.resolve())
    print(f"        {len(files)} file page(s) -> {sub_dir.relative_to(site_root)}/")
    return written


def cleanup_stale_files(projects_dir: Path, written: set, site_root: Path):
    """Removes any *.html file under projects/ that this run didn't (re)write
    - i.e. leftovers from a project card that got removed, or a
    data-source-file attribute that got removed or narrowed to fewer files.
    Nothing under projects/ is hand-authored (see repo-structure notes), so
    anything not in `written` is safe to delete. Also removes any directory
    (e.g. a now-empty <slug>-source/) left empty afterwards."""
    if not projects_dir.exists():
        return

    removed = []
    for path in sorted(projects_dir.rglob("*.html")):
        if path.resolve() not in written:
            path.unlink()
            removed.append(path)

    # Deepest directories first, so a parent that's only empty because we
    # just removed its last child also gets cleaned up in the same pass.
    for d in sorted((p for p in projects_dir.rglob("*") if p.is_dir()),
                     key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()  # no-op unless truly empty
        except OSError:
            pass

    if removed:
        print(f"Removed {len(removed)} stale generated page(s) (no longer referenced from index.html):")
        for path in removed:
            print(f"  - {path.relative_to(site_root)}")
    else:
        print("No stale generated pages to remove.")


def main():
    html = INDEX_HTML.read_text(encoding="utf-8")
    projects = extract_projects(html)
    rail_html_top = extract_rail(html, up="../")
    rail_html_nested = extract_rail(html, up="../../")
    if not rail_html_top:
        print("  ! Warning: couldn't find <header class=\"rail\" id=\"rail\"> in index.html - generated pages will have no navbar.")

    if not projects:
        print("No project cards with a GitHub Repository link found.")
        return

    PROJECTS_DIR.mkdir(exist_ok=True)
    written = set()
    for project in projects:
        slug = project["repo"].lower()

        files = []
        if project["source_entries"]:
            files = expand_source_entries(project["owner"], project["repo"], project["source_entries"])
            if not files:
                print(f"  ! '{project['title']}': data-source-file entries resolved to zero files, skipping source page(s)")
        project["_resolved_files"] = files

        out_path = PROJECTS_DIR / f"{slug}.html"
        print(f"  -> {project['title']} ({project['owner']}/{project['repo']}) -> {out_path.relative_to(SITE_ROOT)}")
        page = render_page(project, rail_html_top, has_source=bool(files))
        out_path.write_text(page, encoding="utf-8")
        written.add(out_path.resolve())

        if files:
            written |= generate_source_pages(project, files, rail_html_top, rail_html_nested, PROJECTS_DIR, SITE_ROOT)

    print(f"Generated {len(projects)} project page(s).")
    cleanup_stale_files(PROJECTS_DIR, written, SITE_ROOT)


if __name__ == "__main__":
    main()
