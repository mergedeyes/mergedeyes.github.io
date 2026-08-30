#!/usr/bin/env python3
"""
Updates the site's visitor counter using GoatCounter analytics.

This script fetches real website traffic data to update the static HTML files,
keeping the site entirely free of client-side API requests. It makes a single
authenticated call to `/api/v0/stats/total`, requesting a date range that
starts well before this site (or GoatCounter itself) could possibly have any
real history. Per GoatCounter's API schema, that endpoint's `total` field is
"total visitors for the requested date range" -- not a separate all-time
figure -- so a wide-enough range makes it double as the lifetime total. That
one response is then used for two things:
1. The all-time lifetime total, patched into the footer of `index.html`.
2. The per-day breakdown (`stats`), merged into the local cache
   (.github/data/visitor-count.json) and embedded into `visitors.html` for
   the chart rendering.

This used to also hit the public, unauthenticated `/counter/TOTAL.json`
endpoint for the footer total, to avoid needing a token for that half. That
endpoint turned out to be considerably less reliable in practice (repeatedly
403'd, seemingly rate-limited per-site rather than per-caller -- it 403'd
from unrelated networks too) than the authenticated stats endpoint, which
has been solid. Since the authenticated call already returns everything the
public one did (and more), the public call was dropped entirely rather than
kept as a fallback -- one reliable request beats one reliable + one flaky.

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

# Start of the "all time" range used to compute the lifetime total. Picked
# to comfortably predate any real site history (and GoatCounter itself,
# which didn't exist yet) rather than trying to track the site's actual
# creation date -- it only needs to be "early enough", not exact.
ALL_TIME_START = "2015-01-01T00:00:00Z"


def fetch_goatcounter_stats(token):
    """Fetches all-time-to-date stats in one authenticated call.

    Returns a dict with "total" (all-time lifetime total, see module
    docstring) and "stats" (per-day breakdown, one entry per day since
    ALL_TIME_START) on success, or None on failure. Returning None instead
    of exiting lets the caller fall back to whatever was cached from the
    last successful run, rather than clobbering good data with a failure.
    """
    end_date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT23:59:59Z")
    url = f"https://{GOATCOUNTER_DOMAIN}/api/v0/stats/total?start={ALL_TIME_START}&end={end_date}"

    # Auth goes only via the Authorization header (per GoatCounter's API docs).
    # A token must never be put in the URL's query string: query strings get
    # written to CI logs, proxy logs, and browser history verbatim.
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
            return json.load(resp)
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
    # .strip() guards against a stray trailing newline/space getting baked
    # into the GitHub Actions secret when it was copy-pasted -- that byte
    # makes the token silently not match anything server-side (seen in the
    # wild: valid token, still 401/404, because of an invisible \n at the end).
    token = os.environ.get("GOATCOUNTER_TOKEN", "").strip()
    if not token:
        print("error: GOATCOUNTER_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    daily_cache = state.setdefault("daily", {})

    result = fetch_goatcounter_stats(token)
    ok = result is not None

    if ok:
        new_total = result.get("total", state.get("total", 0))
        for stat in result.get("stats", []):
            day_str = stat.get("day", "")[:10]  # Format: "YYYY-MM-DD"
            if day_str:
                daily_cache[day_str] = stat.get("daily", 0)
    else:
        # Fall back to whatever was cached from the last successful run --
        # a failed fetch shouldn't wipe out good data or write bogus zeros.
        new_total = state.get("total", 0)

    state["total"] = new_total
    state["daily"] = daily_cache
    save_state(state)

    patch_footer(new_total)
    patch_visitor_chart(daily_cache)
    print(f"visitor count total: {new_total}")

    if not ok:
        print(
            "warning: GoatCounter stats fetch failed (see error above) -- "
            "footer total and chart data are both stale/unchanged this run. "
            "Check GOATCOUNTER_TOKEN.",
            file=sys.stderr,
        )
        sys.exit(1)

if __name__ == "__main__":
    main()
