#!/usr/bin/env python3
"""
Updates the site's visitor counter using GoatCounter analytics.

This script fetches real website traffic data to update the static HTML files,
keeping the site entirely free of client-side API requests. It performs two tasks:
1. Fetches the all-time lifetime visitor count via the public `TOTAL.json`
   endpoint and patches it into the footer of `index.html`.
2. Fetches the last 30 days of daily visitor stats via the authenticated
   `/api/v0/stats/total` endpoint, merges them into the local cache
   (.github/data/visitor-count.json), and embeds the JSON payload into
   `visitors.html` for the chart rendering.

Requires GOATCOUNTER_TOKEN in the environment. This must be a GoatCounter API
token with 'statistics' permissions.
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATE_PATH = os.path.join(REPO_ROOT, ".github", "data", "visitor-count.json")
HTML_FILES_WITH_COUNTER = ["index.html"]
VISITOR_DATA_HTML_FILE = "visitors.html"
GOATCOUNTER_DOMAIN = "mergedeyes.goatcounter.com"

def fetch_goatcounter_total():
    """Fetches the all-time total pageviews for the footer."""
    url = f"https://{GOATCOUNTER_DOMAIN}/counter/TOTAL.json"
    req = urllib.request.Request(url, headers={"User-Agent": "mergedcloud-visitor-counter"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
            return int(data["count"].replace(",", ""))
    except Exception as e:
        print(f"Failed to fetch GoatCounter total: {e}", file=sys.stderr)
        return None

def fetch_daily_stats(token):
    """Fetches the last 30 days of daily traffic for the chart.

    Returns None on failure instead of exiting the process, so that the
    caller can still patch whatever data DID succeed (e.g. the public
    lifetime total) even when this authenticated call fails -- a bad or
    revoked token shouldn't also block the footer count from updating.
    """
    start_date = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=30)).strftime("%Y-%m-%dT00:00:00Z")
    end_date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT23:59:59Z")

    # Auth goes only via the Authorization header (per GoatCounter's API docs).
    # A token must never be put in the URL's query string: query strings get
    # written to CI logs, proxy logs, and browser history verbatim.
    url = f"https://{GOATCOUNTER_DOMAIN}/api/v0/stats/total?start={start_date}&end={end_date}"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "mergedcloud-visitor-counter"
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
            return data.get("stats", [])
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"GoatCounter API request failed: {e.code} {e.reason}\n{body}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"GoatCounter API request failed: {e}", file=sys.stderr)
        return None

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


def patch_visitor_chart(daily):
    """Patch the per-day visits JSON embedded in visitors.html so the
    chart page has fresh data with zero client-side network requests."""
    path = os.path.join(REPO_ROOT, VISITOR_DATA_HTML_FILE)
    if not os.path.isfile(path):
        print(f"warning: {VISITOR_DATA_HTML_FILE} not found, skipping chart data patch", file=sys.stderr)
        return
    with open(path, encoding="utf-8") as f:
        html = f.read()
    payload = json.dumps(dict(sorted(daily.items())), separators=(",", ":"))
    pattern = re.compile(
        r'(<script type="application/json" id="visitor-daily-data">)[^<]*(</script>)'
    )
    new_html, count = pattern.subn(lambda m: m.group(1) + payload + m.group(2), html)
    if count == 0:
        print(f"warning: no visitor-daily-data placeholder found in {VISITOR_DATA_HTML_FILE}", file=sys.stderr)
        return
    if new_html != html:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_html)
        print(f"updated {VISITOR_DATA_HTML_FILE} with {len(daily)} day(s) of data")

def main():
    token = os.environ.get("GOATCOUNTER_TOKEN")
    if not token:
        print("error: GOATCOUNTER_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)

    state = load_state()

    # Get overall total for the footer
    new_total = fetch_goatcounter_total()
    if new_total is None:
        new_total = state.get("total", 0)

    # Get daily stats for the chart. This can fail independently of the
    # public total above (e.g. a bad/revoked API token) -- don't let that
    # stop the footer count (and whatever cached daily data we already
    # have) from still being written out.
    daily_stats = fetch_daily_stats(token)
    daily_cache = state.setdefault("daily", {})
    daily_stats_ok = daily_stats is not None

    if daily_stats_ok:
        for stat in daily_stats:
            day_str = stat.get("day", "")[:10]  # Format: "YYYY-MM-DD"
            if day_str:
                daily_cache[day_str] = stat.get("daily", 0)

    state["total"] = new_total
    state["daily"] = daily_cache
    save_state(state)

    patch_footer(new_total)
    patch_visitor_chart(daily_cache)
    print(f"visitor count total: {new_total}")

    if not daily_stats_ok:
        print(
            "warning: daily stats fetch failed (see error above) -- "
            "footer total was still updated, but chart data is stale/unchanged. "
            "Check GOATCOUNTER_TOKEN.",
            file=sys.stderr,
        )
        sys.exit(1)

if __name__ == "__main__":
    main()
