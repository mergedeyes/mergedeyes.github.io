#!/usr/bin/env python3
"""
Generates a standalone HTML page under /projects for every project card in
index.html that has a "Repository" link pointing at GitHub. Each page gets:
  - the project title
  - its README, fetched pre-rendered as HTML via the GitHub API
  - its tags (languages/stack) and license badge, copied from the card
  - a link back to the repository
  - a "Commit history" page listing every commit (title, short id, date,
    author), each card linking straight out to that commit on GitHub -
    generated for every project with a Repository link, unlike the
    source browser below which is opt-in
  - for opted-in projects (data-source-file="..." on the card), one or more
    syntax-highlighted source files with line numbers. The attribute is a
    comma-separated list; each entry is either a literal file path or a
    "dir/*" wildcard that expands recursively to every file under that
    directory. One file -> a single wide source page, same as before.
    Two or more files -> a file-tree sidebar plus one page per file.
  - the site's own navbar (extracted from index.html, not hand-copied,
    so it can never drift out of sync with the real one)

Change-skip caching (added 2026-08-30): re-fetching everything from GitHub
on every run (every push to main, the daily cron, manual dispatch) got
wasteful once commit history was added on top of the README/source-browser
fetches - most runs touch zero repos. .github/data/project-pages-cache.json
tracks, per project, the repo's last-seen `pushed_at` and (for the source
browser) each file's git blob sha. A project whose repo hasn't been pushed
to since the cache entry was written skips re-fetching its README, source
files, and commits entirely - the content is read back out of the already-
published page for that project instead (see extract_readme_html/
extract_highlighted/extract_commit_cards) rather than duplicated into the
cache file itself, so there's only ever one copy of the actual content.
Two things always still happen even when a repo is unchanged: the page is
still re-rendered (so an index.html-only edit - a retitled card, a changed
tag - is never missed just because the linked repo stayed quiet), and
individual source files are additionally checked file-by-file via blob sha
(so a big repo where one file changed doesn't force every file to be
re-fetched), and the commit history is extended incrementally - only
commits newer than the last known one are fetched, then prepended onto the
previously-rendered cards - instead of re-walking the whole history every
time. See fetch_commits_since()'s docstring for exactly how that degrades
if a repo's history is ever rewritten (force-push/rebase) or an API call
fails outright.

Run from the repository root (that's how the workflow invokes it):
    python .github/scripts/generate_project_pages.py

Reads GITHUB_TOKEN from the environment to authenticate API calls (raises
the rate limit well past what these repos need, and would be required for
private repos - not the case here, but no reason not to use it).
"""
import json
import os
import re
from datetime import datetime
from html import escape as html_escape
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
CACHE_PATH = SITE_ROOT / ".github" / "data" / "project-pages-cache.json"

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


# ============================================================
# Build cache (see the module docstring above)
# ============================================================

def load_cache() -> dict:
    """Loads the per-project build cache. Missing/corrupt/unreadable file
    -> empty cache, which just means every project gets treated as
    changed on this run (same as the very first run ever) - never a hard
    failure."""
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(f"  ! Warning: could not read {CACHE_PATH.relative_to(SITE_ROOT)}, starting fresh (this run will re-fetch everything).")
        return {}


def save_cache(cache: dict):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _extract_between(text: str, start_marker: str, end_marker: str):
    start = text.find(start_marker)
    if start == -1:
        return None
    start += len(start_marker)
    end = text.find(end_marker, start)
    if end == -1 or end < start:
        return None
    return text[start:end]


def extract_readme_html(path: Path):
    """Reads a previously generated <slug>.html back and pulls the inner
    HTML of its .readme-content div out verbatim (plain string slicing
    against the exact template markup, not a parse-and-reserialize round
    trip through BeautifulSoup, so a reused README's bytes come back
    identical to what's already published - a round trip risks subtle
    re-serialization differences, e.g. attribute quoting, that would show
    up as spurious no-op diffs every run). Returns None (falls back to a
    fresh API fetch) if the file doesn't exist or doesn't look like it
    was expected to - always safe, just costs an extra API call."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _extract_between(text, '<div class="readme-content">\n', '\n        </div>\n      </section>')


def extract_highlighted(path: Path):
    """Same idea as extract_readme_html, for a single source file's
    Pygments-highlighted block on a previously generated source page."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    head_idx = text.find('class="source-page__head">')
    if head_idx == -1:
        return None
    close_marker = "</div>\n        "
    close_idx = text.find(close_marker, head_idx)
    if close_idx == -1:
        return None
    content_start = close_idx + len(close_marker)
    end = text.find("\n      </section>", content_start)
    if end == -1 or end < content_start:
        return None
    return text[content_start:end]


def extract_commit_cards(path: Path):
    """Same idea again, for the rendered commit-card HTML on a previously
    generated <slug>-commits.html."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _extract_between(text, '<div class="cards commits-list">\n', '\n        </div>\n      </section>')


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


def fetch_repo_meta(owner: str, repo: str):
    """Fetches the repo's pushed_at timestamp and default branch in one
    cheap call - pushed_at is the change-skip signal (see module
    docstring), default_branch feeds the Git Trees API (which, unlike the
    readme/contents endpoints, has no implicit 'use the default branch'
    behavior). Returns None on any failure; callers treat that the same
    as "definitely changed, fetch for real" since a cache can't be
    trusted to still be valid without a fresh pushed_at to compare."""
    url = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        resp = requests.get(url, headers=API_HEADERS_JSON, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return {"pushed_at": data.get("pushed_at"), "default_branch": data.get("default_branch")}
    except (requests.RequestException, ValueError):
        return None


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


def fetch_repo_file_tree(owner: str, repo: str, branch: str) -> dict:
    """Fetches the full recursive file listing (blobs only, no
    directories) of the given branch as a {path: blob_sha} dict - used
    both to resolve dir/* wildcards in data-source-file and, via the
    blob sha, to know which individual files actually changed since the
    last build (see the module docstring). branch is passed in rather
    than looked up here so a caller who already has it from
    fetch_repo_meta doesn't pay for it twice."""
    if not branch:
        return {}
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
    try:
        resp = requests.get(url, headers=API_HEADERS_JSON, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return {}
    if data.get("truncated"):
        print(f"  ! Warning: {owner}/{repo}'s file tree was truncated by GitHub's API "
              f"(repo too large) - a dir/* wildcard may be missing some files.")
    return {item["path"]: item.get("sha", "") for item in data.get("tree", []) if item.get("type") == "blob"}


def expand_source_entries(entries: list, file_tree: dict) -> list:
    """Resolves data-source-file entries into concrete file paths against
    an already-fetched {path: blob_sha} tree (see fetch_repo_file_tree) -
    this stays a pure function with no API calls of its own; the caller
    decides whether/when the tree is even worth fetching (see main() -
    an unchanged repo with the same data-source-file entries as last
    time skips the tree fetch entirely and reuses the cached file list).
    An entry ending in "/*" expands to every file under that directory,
    recursively; anything else is a literal file path (kept even if it's
    not in file_tree, so a typo'd path still gets attempted and fails
    gracefully at fetch time rather than silently vanishing). The first
    literal entry (if any) is left first in the result, so it becomes
    the "primary" file shown on the top-level <slug>-source.html page;
    wildcard-resolved files are appended after. De-duplicates so a file
    matched by both a literal entry and a wildcard only appears once."""
    literal_files = [e for e in entries if not e.endswith("/*")]
    wildcard_prefixes = [e[:-1] for e in entries if e.endswith("/*")]  # "src/*" -> "src/"

    resolved = list(literal_files)
    for prefix in wildcard_prefixes:
        resolved.extend(f for f in file_tree if f.startswith(prefix))

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


def format_commit_date(iso_date: str) -> str:
    """Formats a GitHub API commit date ("2026-08-29T12:34:56Z") into a
    short human-readable form ("29 Aug 2026"). Falls back to the raw
    string on anything unexpected rather than raising."""
    if not iso_date:
        return ""
    try:
        dt = datetime.strptime(iso_date, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return iso_date
    return dt.strftime("%d %b %Y")


def fetch_commits_since(owner: str, repo: str, known_newest_sha, max_commits: int = 300):
    """Fetches commits newer than known_newest_sha (paginating from the
    top and stopping the moment that sha shows up) instead of always
    re-walking the whole history - once a project is cached, this is
    usually just one API call, since most runs add only a handful of
    commits since the last build. Returns (commits, status):
      - "ok"        - completed cleanly. commits is either the first
                       max_commits (known_newest_sha was None - first
                       time caching this project) or just the new ones
                       on top of it (often empty - "pushed_at changed
                       but nothing new landed on the default branch" is
                       a normal, cheap-to-detect case, e.g. a push to a
                       different branch, or a tag).
      - "rewritten" - known_newest_sha was never found within
                       max_commits worth of history. Either the default
                       branch's history was rewritten (force-push /
                       rebase / squash - none of the new shas match the
                       old one) or the repo has genuinely gained more
                       than max_commits new commits since the last build
                       (essentially never, for a personal project). The
                       previously-cached cards can't be trusted to
                       prepend onto, so commits here is a full, capped
                       fetch meant to replace them outright.
      - "failed"    - a request errored or returned non-200 before this
                       could reach a conclusion, AND known_newest_sha was
                       set (there's a cache worth protecting) - commits
                       may be partial. The caller should keep whatever it
                       already had rather than treat an empty result here
                       as "nothing changed".
    A failure on a first-ever fetch (known_newest_sha is None) reports
    "ok" with a possibly-empty/partial list instead, matching this
    script's usual one-broken-repo-shouldn't-fail-the-run posture - the
    caller just skips writing a commits page for that project.
    """
    commits = []
    page = 1
    per_page = 100
    found_known = known_newest_sha is None

    while len(commits) < max_commits:
        url = f"https://api.github.com/repos/{owner}/{repo}/commits"
        try:
            resp = requests.get(
                url, headers=API_HEADERS_JSON, timeout=15,
                params={"per_page": per_page, "page": page},
            )
        except requests.RequestException as e:
            print(f"  ! Could not fetch commits for {owner}/{repo}: {e}")
            return commits, ("ok" if found_known else "failed")
        if resp.status_code != 200:
            print(f"  ! Could not fetch commits for {owner}/{repo} (HTTP {resp.status_code}).")
            return commits, ("ok" if found_known else "failed")
        try:
            batch = resp.json()
        except ValueError:
            return commits, ("ok" if found_known else "failed")
        if not batch:
            break

        stop = False
        for item in batch:
            sha = item.get("sha", "")
            if known_newest_sha and sha == known_newest_sha:
                found_known = True
                stop = True
                break
            commit = item.get("commit", {}) or {}
            author_info = commit.get("author") or {}
            message = (commit.get("message") or "").strip()
            commits.append({
                "sha": sha,
                "short_sha": sha[:7],
                "title": message.splitlines()[0] if message else "(no commit message)",
                "author": author_info.get("name") or "Unknown",
                "date": format_commit_date(author_info.get("date", "")),
                "html_url": item.get("html_url") or f"https://github.com/{owner}/{repo}/commit/{sha}",
            })
            if len(commits) >= max_commits:
                stop = True
                break

        if stop or len(batch) < per_page:
            break
        page += 1

    return commits, ("ok" if found_known else "rewritten")


def render_commit_card(commit: dict) -> str:
    return f"""<a class="card commit-card" href="{commit['html_url']}">
            <div class="card__head">
              <div class="card__tags"><span class="tag">{commit['short_sha']}</span></div>
              <h3 class="card__title">{html_escape(commit['title'])}</h3>
            </div>
            <div class="card__foot commit-card__foot">
              <span class="commit-card__author">{html_escape(commit['author'])}</span>
              <span class="commit-card__date">{commit['date']}</span>
            </div>
          </a>"""


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
    for its generated page, e.g. "src/handlers/auth.rs" -> "src--handlers--auth.rs".

    Strips a single leading "." from the result (e.g. ".github/workflows/x.yml"
    -> "github--workflows--x.yml") so the generated filename never itself
    starts with a literal dot. GitHub Pages' explicit Jekyll build step
    (jekyll-gh-pages.yml, actions/jekyll-build-pages@v1) silently drops any
    file whose name starts with "." from its output -- 404ing the page --
    regardless of this repo's .nojekyll file, which only disables GitHub
    Pages' own *automatic* Jekyll auto-detection pipeline and has no effect
    on an explicitly invoked `jekyll build` command run from a workflow."""
    name = path.replace("/", "--")
    if name.startswith("."):
        name = name[1:]
    return name


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
            {commits_link_html}
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

# Commit history page - one per project, generated unconditionally for
# every card with a Repository link (unlike the source browser, this
# isn't opt-in via data-source-file). Reuses the same .card visual
# language as project cards/skill chips, per the brief - one card per
# commit, the whole card a real link out to that commit on GitHub, and
# the "title + back link" head row reuses .source-page__head as-is.
COMMITS_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Commits — {title} — Jan</title>
  <link rel="stylesheet" href="../styles.css?v={asset_version}" />
</head>
<body>
  <a class="skip-link" href="#main">Skip to content</a>
  <div class="layout">
    {rail_html}
    <main class="main" id="main">
      <section class="section commits-page">
        <p class="section__path">
          <a href="../index.html#projects" class="link">~/projects</a> /
          <a href="{project_href}" class="link">{repo}</a> / commits
        </p>
        <div class="source-page__head">
          <h1 class="section__title">Commit history</h1>
          <a href="{project_href}" class="link">← Back to {title}</a>
        </div>
        <div class="cards commits-list">
{commit_cards_html}
        </div>
      </section>
    </main>
  </div>
</body>
</html>
"""


def render_page(project: dict, rail_html: str, has_source: bool, readme_html: str, commit_count: int) -> str:
    tags_html = "\n          ".join(f'<span class="tag">{t}</span>' for t in project["tags"])
    license_html = (
        f'<span class="license-badge">{project["license"]}</span>'
        if project["license"] else ""
    )
    slug = project["repo"].lower()

    source_link_html = ""
    if has_source:
        files = project["_resolved_files"]
        label = f"View source ({files[0]})" if len(files) == 1 else f"Browse source ({len(files)} files)"
        source_link_html = f'<a href="{slug}-source.html" class="link">{label}</a>'

    commits_link_html = (
        f'<a href="{slug}-commits.html" class="link">Commit history ({commit_count})</a>'
        if commit_count else ""
    )

    return PAGE_TEMPLATE.format(
        title=project["title"],
        repo=project["repo"],
        tags_html=tags_html,
        repo_url=project["repo_url"],
        license_html=license_html,
        readme_html=readme_html,
        rail_html=rail_html,
        source_link_html=source_link_html,
        commits_link_html=commits_link_html,
        asset_version=ASSET_VERSION,
    )


def render_commits_page(project: dict, rail_html: str, commit_cards_html: str) -> str:
    slug = project["repo"].lower()
    return COMMITS_PAGE_TEMPLATE.format(
        title=project["title"],
        repo=project["repo"],
        rail_html=rail_html,
        project_href=f"{slug}.html",
        commit_cards_html=commit_cards_html,
        asset_version=ASSET_VERSION,
    )


def render_source_file_page(project: dict, rail_html_for: dict, file_path: str, tree, up: str, project_href: str, highlighted: str) -> str:
    """Renders one file's page - used both for the single-file case
    (up="../", project_href="<slug>.html") and for each file in the
    multi-file case (index page: up="../", tree base includes the
    subfolder; per-file pages: up="../../", tree base is empty since
    they're siblings). `highlighted` is already-resolved HTML (either
    freshly fetched or reused from the previous build - see
    generate_source_pages), not fetched here."""
    slug = project["repo"].lower()
    tree_base = "" if up == "../../" else f"{slug}-source/"
    tree_html = ""
    if tree is not None:
        rendered = render_tree_html(tree, "", file_path, base=tree_base)
        tree_html = f'<nav class="file-tree"><p class="file-tree__title">Files</p>{rendered}</nav>'

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


def generate_source_pages(project: dict, files: list, rail_html_top: str, rail_html_nested: str, out_dir: Path, site_root: Path, blob_shas: dict, cached_files: dict) -> set:
    """Writes <slug>-source.html (and, for multiple files, <slug>-source/
    per-file pages plus a file-tree sidebar on every one of them).

    For each file, reuses last build's highlighted HTML (read back from
    its own already-published page - see extract_highlighted) instead of
    re-fetching+re-highlighting it, but ONLY when that exact file's git
    blob sha hasn't changed (blob_shas is this run's {path: sha}, from
    fetch_repo_file_tree; cached_files is the same shape from last run's
    cache) - so within one repo, only the files that actually changed
    get re-fetched, not the whole source browser at once.

    Returns the set of resolved paths it wrote, so main() can tell
    cleanup_stale_files which files in projects/ are still current and
    which are leftovers from a data-source-file that's since been
    removed or narrowed."""
    slug = project["repo"].lower()
    owner, repo = project["owner"], project["repo"]
    rail_html_for = {"../": rail_html_top, "../../": rail_html_nested}
    written = set()
    stats = {"reused": 0, "fetched": 0}

    def get_highlighted(file_path: str, existing_page_path: Path) -> str:
        old_sha = cached_files.get(file_path)
        new_sha = blob_shas.get(file_path)
        if old_sha and new_sha and old_sha == new_sha:
            cached = extract_highlighted(existing_page_path)
            if cached is not None:
                stats["reused"] += 1
                return cached
        stats["fetched"] += 1
        return fetch_highlighted_source(owner, repo, file_path)

    if len(files) == 1:
        out_path = out_dir / f"{slug}-source.html"
        highlighted = get_highlighted(files[0], out_path)
        page = render_source_file_page(
            project, rail_html_for, files[0], tree=None,
            up="../", project_href=f"{slug}.html", highlighted=highlighted,
        )
        out_path.write_text(page, encoding="utf-8")
        written.add(out_path.resolve())
        print(f"     -> source page -> {out_path.relative_to(site_root)} ({'reused' if stats['reused'] else 'fetched'})")
        return written

    tree = build_tree(files)
    primary = files[0]
    sub_dir = out_dir / f"{slug}-source"
    sub_dir.mkdir(exist_ok=True)

    # Resolve every file's content BEFORE writing anything - get_highlighted
    # reads each file's existing per-file page, which the loop below is
    # about to overwrite.
    highlighted_by_file = {f: get_highlighted(f, sub_dir / f"{safe_filename(f)}.html") for f in files}

    index_page = render_source_file_page(
        project, rail_html_for, primary, tree=tree,
        up="../", project_href=f"{slug}.html", highlighted=highlighted_by_file[primary],
    )
    index_path = out_dir / f"{slug}-source.html"
    index_path.write_text(index_page, encoding="utf-8")
    written.add(index_path.resolve())
    print(f"     -> source index -> {index_path.relative_to(site_root)} ({len(files)} files)")

    for file_path in files:
        page = render_source_file_page(
            project, rail_html_for, file_path, tree=tree,
            up="../../", project_href=f"../{slug}.html", highlighted=highlighted_by_file[file_path],
        )
        file_out = sub_dir / f"{safe_filename(file_path)}.html"
        file_out.write_text(page, encoding="utf-8")
        written.add(file_out.resolve())
    print(f"        {len(files)} file page(s) -> {sub_dir.relative_to(site_root)}/ ({stats['fetched']} fetched, {stats['reused']} reused)")
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
    cache = load_cache()
    written = set()

    for project in projects:
        slug = project["repo"].lower()
        owner, repo = project["owner"], project["repo"]
        key = f"{owner}/{repo}"
        cache_entry = cache.get(key, {})
        out_path = PROJECTS_DIR / f"{slug}.html"
        existing_commits_path = PROJECTS_DIR / f"{slug}-commits.html"

        meta = fetch_repo_meta(owner, repo)
        pushed_at = meta.get("pushed_at") if meta else None
        default_branch = meta.get("default_branch") if meta else None
        # No pushed_at (repo-meta call failed) -> can't trust a cache
        # comparison either way, so treat it as changed and fetch for
        # real - same safe-by-default behavior this script has always
        # had for a failed call.
        repo_unchanged = bool(pushed_at) and pushed_at == cache_entry.get("pushed_at")

        print(f"  -> {project['title']} ({owner}/{repo}) -> {out_path.relative_to(SITE_ROOT)}"
              + (" (repo unchanged since last build)" if repo_unchanged else ""))

        # ---------------------------------------------------------- README
        readme_html = extract_readme_html(out_path) if repo_unchanged else None
        if readme_html is None:
            readme_html = fetch_readme_html(owner, repo)

        # ---------------------------------------------------------- source files
        files = []
        blob_shas = {}
        cached_files = cache_entry.get("files", {})
        if project["source_entries"]:
            same_entries = cache_entry.get("source_entries") == project["source_entries"]
            reuse_file_list = repo_unchanged and same_entries and cached_files
            if reuse_file_list:
                # Nothing was pushed and the same entries were requested
                # last time, so the resolved file list can't have
                # changed either - skip the tree fetch entirely.
                blob_shas = dict(cached_files)
                files = list(blob_shas.keys())
            elif not default_branch and same_entries and cached_files:
                # The repo-meta call failed outright (no branch to query
                # the tree with, and no reliable pushed_at to compare
                # against either) - rather than resolving dir/* wildcards
                # against an empty tree (which would silently drop every
                # wildcard-matched file and, via cleanup_stale_files,
                # delete their already-published pages), fall back to
                # last known-good file list. Same reasoning as the
                # "failed" status on commits below: a transient API
                # hiccup should never destroy content that was fine
                # yesterday.
                print(f"  ! '{project['title']}': could not reach the GitHub API for repo info - reusing last known file list instead of resolving dir/* wildcards against nothing")
                blob_shas = dict(cached_files)
                files = list(blob_shas.keys())
            else:
                file_tree = fetch_repo_file_tree(owner, repo, default_branch)
                files = expand_source_entries(project["source_entries"], file_tree)
                blob_shas = {f: file_tree.get(f, "") for f in files}
                if not files:
                    print(f"  ! '{project['title']}': data-source-file entries resolved to zero files, skipping source page(s)")
        project["_resolved_files"] = files

        # ---------------------------------------------------------- commits
        cached_newest_sha = cache_entry.get("newest_commit_sha")
        cached_commit_count = cache_entry.get("commit_count", 0)
        commit_cards_html = None
        commit_count = 0
        newest_commit_sha = cached_newest_sha

        if repo_unchanged and cached_commit_count:
            commit_cards_html = extract_commit_cards(existing_commits_path)
            if commit_cards_html is not None:
                commit_count = cached_commit_count

        if commit_cards_html is None:
            new_commits, status = fetch_commits_since(owner, repo, cached_newest_sha)
            if status == "failed" and cached_commit_count:
                commit_cards_html = extract_commit_cards(existing_commits_path)
                if commit_cards_html is not None:
                    commit_count = cached_commit_count
                    print(f"  ! '{project['title']}': could not refresh commit history this run (API error) - kept the existing page")
            elif status == "rewritten":
                print(f"  ! '{project['title']}': previous newest commit not found in current history "
                      f"(force-push/rebase on the default branch?) - rebuilding the commits page from scratch")
                commit_cards_html = "\n".join(f"          {render_commit_card(c)}" for c in new_commits)
                commit_count = len(new_commits)
                newest_commit_sha = new_commits[0]["sha"] if new_commits else None
            else:  # "ok": first-ever fetch, or genuinely nothing new since cached_newest_sha
                previous_cards_html = extract_commit_cards(existing_commits_path) or ""
                new_cards_html = "\n".join(f"          {render_commit_card(c)}" for c in new_commits)
                combined = "\n".join(p for p in (new_cards_html, previous_cards_html) if p)
                if combined:
                    commit_cards_html = combined
                    commit_count = cached_commit_count + len(new_commits)
                if new_commits:
                    newest_commit_sha = new_commits[0]["sha"]
                    print(f"     -> {len(new_commits)} new commit(s) since last build")

        # ---------------------------------------------------------- render + write
        page = render_page(project, rail_html_top, has_source=bool(files), readme_html=readme_html, commit_count=commit_count)
        out_path.write_text(page, encoding="utf-8")
        written.add(out_path.resolve())

        if files:
            written |= generate_source_pages(project, files, rail_html_top, rail_html_nested, PROJECTS_DIR, SITE_ROOT, blob_shas, cached_files)

        if commit_count and commit_cards_html is not None:
            commits_page = render_commits_page(project, rail_html_top, commit_cards_html)
            existing_commits_path.write_text(commits_page, encoding="utf-8")
            written.add(existing_commits_path.resolve())
            print(f"     -> commit history ({commit_count} commits) -> {existing_commits_path.relative_to(SITE_ROOT)}")
        else:
            print(f"  ! '{project['title']}': could not fetch commit history, skipping commits page")

        # ---------------------------------------------------------- update cache
        # Only trust/persist a cache entry when this run actually got a
        # real pushed_at back - a failed repo-meta call leaves whatever
        # was cached before untouched, so the next run can still compare
        # against a known-good timestamp instead of losing all history.
        if pushed_at:
            cache[key] = {
                "pushed_at": pushed_at,
                "source_entries": project["source_entries"],
                "files": blob_shas,
                "newest_commit_sha": newest_commit_sha,
                "commit_count": commit_count,
            }

    print(f"Generated {len(projects)} project page(s).")
    cleanup_stale_files(PROJECTS_DIR, written, SITE_ROOT)
    save_cache(cache)


if __name__ == "__main__":
    main()
