#!/usr/bin/env python3
"""
Updates the site's visitor counter using GoatCounter analytics.

This script fetches real website traffic data to update the static HTML files,
keeping the site entirely free of client-side API requests. Each run makes a
single authenticated call to `/api/v0/stats/total` for only the last
LOOKBACK_DAYS days (small, cheap request) and merges those days into the
local day-by-day cache (.github/data/visitor-count.json). Older days already
in the cache are left untouched -- they're not re-fetched every run, only the
last few days are, since a day's count can still be rising until it's fully
past. Re-fetching each day a few times before it ages out of the lookback
window (once per run while it's "recent") lets its GoatCounter-reported count
settle to its true final value before the script stops touching it.

The all-time lifetime total (patched into index.html's footer) is NOT asked
of the API directly -- GoatCounter's `total` field for this endpoint is
"total visitors for the requested date range", so a 3-day request would only
ever give a 3-day total, not a lifetime one. Instead it's derived by summing
every day this script has ever recorded in the local cache. That's accurate
as long as each day gets captured at least once while numbers are being
reported for it; it's also self-consistent and never needs a separate
wide-range request. (An earlier version of this script requested a decade-
plus-wide range every run specifically to get the API's own `total` field
for this; that worked but re-fetched the site's entire history on every run
for no benefit once the local cache already had it, which is what this
lookback-window approach avoids while keeping the same result.)

Before that, an even earlier version used the public, unauthenticated
`/counter/TOTAL.json` endpoint for the footer total, to avoid needing a
token for that half. That endpoint turned out to be considerably less
reliable in practice (repeatedly 403'd, seemingly rate-limited per-site
rather than per-caller) than the authenticated stats endpoint, which has
been solid -- another reason to get everything from the one call that works.

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

# How many trailing days to re-fetch on every run. Small on purpose: older
# days are considered settled and aren't re-queried, so this only needs to
# cover "how long can a day's count still be changing after it started" --
# 3 days is generous headroom for that, not a meaningful history depth.
LOOKBACK_DAYS = 3


def fetch_recent_stats(token):
    """Fetches the last LOOKBACK_DAYS days of per-day stats.

    Returns the list of per-day stat entries (GoatCounter's `stats` array)
    on success, or None on failure. Returning None instead of exiting lets
    the caller fall back to whatever was cached from the last successful
    run, rather than clobbering good data with a failure.
    """
    start_date = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT00:00:00Z")
    end_date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT23:59:59Z")
    url = f"https://{GOATCOUNTER_DOMAIN}/api/v0/stats/total?start={start_date}&end={end_date}"

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
            return json.load(resp).get("stats", [])
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

    recent_stats = fetch_recent_stats(token)
    ok = recent_stats is not None

    if ok:
        # GoatCounter buckets "day" by the site's configured timezone, not
        # UTC -- the request window above is built entirely in UTC, so close
        # to a UTC/site-local midnight boundary GoatCounter can hand back a
        # day label that's a calendar day ahead of what this script (running
        # in UTC) considers "today". Discard any such not-actually-arrived-
        # yet day rather than caching a premature (usually all-zero) entry
        # for a date that hasn't happened yet from this script's own
        # reference point -- it'll get picked up for real once UTC catches
        # up to it and it falls inside a future run's LOOKBACK_DAYS window.
        today_str = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
        for stat in recent_stats:
            day_str = stat.get("day", "")[:10]  # Format: "YYYY-MM-DD"
            if day_str and day_str <= today_str:
                daily_cache[day_str] = stat.get("daily", 0)
    # else: leave daily_cache exactly as loaded -- a failed fetch shouldn't
    # wipe out good data or write bogus zeros over the last few days.

    daily_cache = dict(sorted(daily_cache.items()))
    # The lifetime total is derived from our own cache, not asked of the
    # API directly -- see module docstring for why.
    new_total = sum(daily_cache.values())

    state["total"] = new_total
    state["daily"] = daily_cache
    save_state(state)

    patch_footer(new_total)
    patch_visitor_chart(daily_cache)
    print(f"visitor count total: {new_total}")

    if not ok:
        print(
            "warning: GoatCounter stats fetch failed (see error above) -- "
            f"the last {LOOKBACK_DAYS} day(s) are stale/unchanged this run "
            "(older days and the total are computed from what's already "
            "cached, so they're unaffected). Check GOATCOUNTER_TOKEN.",
            file=sys.stderr,
        )
        sys.exit(1)

if __name__ == "__main__":
    main()
