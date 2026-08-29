#!/usr/bin/env python3
"""
Generates a standalone HTML page under /projects for every project card in
index.html that has a "Repository" link pointing at GitHub. Each page gets:
  - the project title
  - its README, fetched pre-rendered as HTML via the GitHub API
  - its tags (languages/stack) and license badge, copied from the card
  - a link back to the repository

Intended to run in a GitHub Action; reads GITHUB_TOKEN from the environment
to authenticate API calls (raises the rate limit and works for private
repos too, though none of these are private today).
"""
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

SITE_ROOT = Path(__file__).parent
INDEX_HTML = SITE_ROOT / "index.html"
PROJECTS_DIR = SITE_ROOT / "projects"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
API_HEADERS_JSON = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
API_HEADERS_HTML = {
    "Accept": "application/vnd.github.html+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
if GITHUB_TOKEN:
    API_HEADERS_JSON["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    API_HEADERS_HTML["Authorization"] = f"Bearer {GITHUB_TOKEN}"


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

        projects.append({
            "title": title,
            "tags": tags,
            "owner": owner,
            "repo": repo,
            "repo_url": repo_url,
            "license": license_text,
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


def clean_readme_html(raw_html: str) -> str:
    """Strips GitHub-page-only chrome (heading permalink icons, the
    outer #readme/article wrapper) so the markup drops cleanly into our
    own page template and styling instead of expecting GitHub's own CSS.

    Note: GitHub rewrites relative links/images in a rendered README to
    absolute repo URLs, so this doesn't need to handle relative paths
    itself - verified for text links; re-check if a project's README
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


def extract_rail(html: str) -> str:
    """Pulls the whole <header class="rail" id="rail"> nav block out of
    index.html so project pages share the exact same navigation - single
    source of truth, nothing to remember to keep in sync by hand.

    In-page anchor links (#projects, #top, etc.) only make sense on
    index.html itself; from a page under /projects/ they're rewritten to
    point back at ../index.html#... instead.
    """
    soup = BeautifulSoup(html, "html.parser")
    rail = soup.select_one("header.rail#rail")
    if rail is None:
        return ""

    for a in rail.find_all("a", href=True):
        if a["href"].startswith("#"):
            a["href"] = f"../index.html{a['href']}"

    return str(rail)


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{title} — Jan</title>
  <link rel="stylesheet" href="../styles.css" />
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


def render_page(project: dict, rail_html: str) -> str:
    tags_html = "\n          ".join(f'<span class="tag">{t}</span>' for t in project["tags"])
    license_html = (
        f'<span class="license-badge">{project["license"]}</span>'
        if project["license"] else ""
    )
    readme_html = fetch_readme_html(project["owner"], project["repo"])
    return PAGE_TEMPLATE.format(
        title=project["title"],
        repo=project["repo"],
        tags_html=tags_html,
        repo_url=project["repo_url"],
        license_html=license_html,
        readme_html=readme_html,
        rail_html=rail_html,
    )


def main():
    html = INDEX_HTML.read_text(encoding="utf-8")
    projects = extract_projects(html)
    rail_html = extract_rail(html)
    if not rail_html:
        print("  ! Warning: couldn't find <header class=\"rail\" id=\"rail\"> in index.html - generated pages will have no navbar.")

    if not projects:
        print("No project cards with a GitHub Repository link found.")
        return

    PROJECTS_DIR.mkdir(exist_ok=True)
    for project in projects:
        slug = project["repo"].lower()
        out_path = PROJECTS_DIR / f"{slug}.html"
        print(f"  -> {project['title']} ({project['owner']}/{project['repo']}) -> {out_path.relative_to(SITE_ROOT)}")
        page = render_page(project, rail_html)
        out_path.write_text(page, encoding="utf-8")

    print(f"Generated {len(projects)} project page(s).")


if __name__ == "__main__":
    main()