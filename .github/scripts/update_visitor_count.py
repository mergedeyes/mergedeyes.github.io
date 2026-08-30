#!/usr/bin/env python3
"""
Updates the site's visitor counter.

GitHub's traffic API (`/repos/{owner}/{repo}/traffic/views`) only ever
reports the last 14 days of daily page-view counts for a repository - it
does not keep a running lifetime total. To get a lifetime counter for the
footer, this script keeps its own small state file
(.github/data/visitor-count.json) recording a cumulative total plus the
most recent date it has already folded into that total, and each run only
adds in days newer than that.

As long as this workflow runs at least once every 14 days, no day's views
get skipped or double-counted (a wider gap would silently lose days for
which GitHub has since rolled the data off - unavoidable given what the
API provides).

Requires TRAFFIC_PAT and GITHUB_REPOSITORY in the environment. TRAFFIC_PAT
must be a personal access token with permission to read this repo's
traffic stats (classic PAT with the "repo" scope, or a fine-grained PAT
with "Administration: Read-only" on this repository) - the default
Actions-issued GITHUB_TOKEN cannot call the traffic API no matter what is
granted under `permissions:` in the workflow, since traffic/views sits
under repo administration rather than the standard Actions permission
set. Reads/writes only local files in the repo checkout plus one call to
api.github.com.
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATE_PATH = os.path.join(REPO_ROOT, ".github", "data", "visitor-count.json")
HTML_FILES_WITH_COUNTER = ["index.html"]


def fetch_traffic_views(repo, token):
    url = f"https://api.github.com/repos/{repo}/traffic/views?per=day"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mergedcloud-visitor-counter",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"GitHub traffic API request failed: {e.code} {e.reason}\n{body}", file=sys.stderr)
        sys.exit(1)


def load_state():
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def patch_footer(total):
    formatted = f"{total:,}"
    pattern = re.compile(r'(id="visitor-count">)[^<]*(<)')
    for name in HTML_FILES_WITH_COUNTER:
        path = os.path.join(REPO_ROOT, name)
        with open(path, encoding="utf-8") as f:
            html = f.read()
        new_html, count = pattern.subn(rf'\g<1>{formatted}\g<2>', html)
        if count == 0:
            print(f"warning: no visitor-count placeholder found in {name}", file=sys.stderr)
            continue
        if new_html != html:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_html)
            print(f"updated {name} -> {formatted}")


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ.get("TRAFFIC_PAT")
    if not token:
        print(
            "error: TRAFFIC_PAT is not set. The default GITHUB_TOKEN cannot read "
            "this repo's traffic stats - add a personal access token (classic PAT "
            "with 'repo' scope, or fine-grained PAT with 'Administration: "
            "Read-only') as the TRAFFIC_PAT repository secret.",
            file=sys.stderr,
        )
        sys.exit(1)

    state = load_state()
    data = fetch_traffic_views(repo, token)
    views = data.get("views", [])

    new_total = state["total"]
    last_counted = state["last_counted_date"]

    for entry in sorted(views, key=lambda v: v["timestamp"]):
        day = entry["timestamp"][:10]  # "YYYY-MM-DDT00:00:00Z" -> "YYYY-MM-DD"
        if day > last_counted:
            new_total += entry["count"]
            last_counted = day

    state["total"] = new_total
    state["last_counted_date"] = last_counted
    save_state(state)

    patch_footer(new_total)
    print(f"visitor count total: {new_total} (as of {last_counted})")


if __name__ == "__main__":
    main()
