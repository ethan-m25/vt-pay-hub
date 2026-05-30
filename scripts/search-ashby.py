#!/usr/bin/env python3
"""
vt-pay-hub/scripts/search-ashby.py
Ashby job board scraper — Vermont edition.

Strategy:
  1. POST to Ashby's public GraphQL API — works for all Ashby customers
  2. Filter CA locations; extract salary from compensationTierSummary or fallback
  3. Auto-inject high-yield discovered slugs into SEED_SLUGS

Run: python3 ~/vt-pay-hub/scripts/search-ashby.py
"""

import html as html_mod
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(__file__))
from _common import (
    make_logger, acquire_lock, exa_search, load_existing_keys,
    load_existing_urls,
    write_job, TODAY, OUTPUT_FILE,
)

LOG_FILE  = os.path.expanduser("~/vt-pay-hub/scripts/ashby.log")
REGION_CODE = "vt"
LOCK_FILE = os.path.expanduser("~/vt-pay-hub/scripts/.ashby.lock")

log = make_logger(LOG_FILE)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"

# Verified Ashby customers with Vermont presence
# === Phase 4 seed loader (added 2026-05-27) ===
sys.path.insert(0, os.path.expanduser('~/shared-scripts'))
from hub_employer_seeds import load_ashby_seeds
SEED_SLUGS = load_ashby_seeds('vt')

DISCOVERY_QUERIES = [
    'site:jobs.ashbyhq.com "Vermont" OR "Burlington" salary 2026',
]

CA_TERMS = [
    'vermont',
    'burlington',
    'south burlington',
    'montpelier',
    'rutland',
    ', vt',
    'remote',
]

_NON_CA_TERMS = [
    'new york',
    'california',
    'san francisco',
    'seattle',
    'chicago',
    'boston',
    'toronto',
    'london',
]

ASHBY_GQL_URL = "https://jobs.ashbyhq.com/api/non-user-graphql"
ASHBY_SLUG_RE = re.compile(r'https?://jobs\.ashbyhq\.com/([a-zA-Z0-9._-]+)', re.IGNORECASE)
_SKIP_SLUGS = {'api', 'search', 'home'}

GQL_QUERY = """
query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
  jobBoard: jobBoardWithTeams(
    organizationHostedJobsPageName: $organizationHostedJobsPageName
  ) {
    jobPostings {
      id
      title
      locationName
      compensationTierSummary
    }
  }
}
"""

SALARY_RE = [
    re.compile(r'\$\s*([\d,]+(?:\.\d+)?)\s*[kK]?\s*[-–—]\s*\$\s*([\d,]+(?:\.\d+)?)\s*[kK]?', re.IGNORECASE),
    re.compile(r'([\d,]+)\s*[-–—]\s*([\d,]+)\s*(?:USD|per year|annually)', re.IGNORECASE),
]


def _gql_fetch(slug):
    payload = json.dumps({
        "operationName": "ApiJobBoardWithTeams",
        "query": GQL_QUERY,
        "variables": {"organizationHostedJobsPageName": slug},
    }).encode()
    req = urllib.request.Request(
        ASHBY_GQL_URL,
        data=payload,
        headers={
            "User-Agent": UA,
            "Content-Type": "application/json",
            "x-requested-with": "XMLHttpRequest",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
            board = (data.get("data") or {}).get("jobBoard") or {}
            return board.get("jobPostings") or []
    except Exception as e:
        log(f"  GQL error ({slug}): {e}")
        return None


def _fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception:
        return None


def _parse_salary(summary):
    if not summary:
        return None
    if "per hour" in summary.lower() or "/hr" in summary.lower():
        return None
    m = re.search(r'\$([\d,.]+)\s*[kK]?\s*[-–—]\s*\$([\d,.]+)\s*[kK]?', summary)
    if m:
        try:
            def _pn(s, is_k):
                v = float(s.replace(",", ""))
                return int(v * 1000) if is_k and v < 1000 else int(v)
            k = "k" in summary[m.start():m.end()].lower()
            vmin, vmax = _pn(m.group(1), k), _pn(m.group(2), k)
            if 30_000 <= vmin <= 2_000_000 and vmin < vmax:
                return vmin, vmax
        except Exception:
            pass
    return None


def _parse_salary_text(text):
    if not text:
        return None
    for pat in SALARY_RE:
        m = pat.search(text)
        if not m:
            continue
        try:
            k = "k" in m.group(0).lower()
            def _pn(s):
                v = float(s.replace(",", ""))
                return int(v * 1000) if k and v < 1000 else int(v)
            vmin, vmax = _pn(m.group(1)), _pn(m.group(2))
            if 30_000 <= vmin <= 2_000_000 and vmin < vmax:
                return vmin, vmax
        except Exception:
            continue
    return None


def _fetch_job_salary(slug, job_id):
    html = _fetch_html(f"https://jobs.ashbyhq.com/{slug}/{job_id}")
    if not html:
        return None
    plain = html_mod.unescape(re.sub(r'<[^>]+>', ' ', html))
    return _parse_salary_text(re.sub(r'\s+', ' ', plain))


REMOTE_CLAIMED_FILE = os.path.expanduser("~/.openclaw/remote-claimed.json")


def _load_remote_claimed():
    try:
        with open(REMOTE_CLAIMED_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_remote_claimed(data):
    os.makedirs(os.path.dirname(REMOTE_CLAIMED_FILE), exist_ok=True)
    tmp = REMOTE_CLAIMED_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, REMOTE_CLAIMED_FILE)


def _is_ca(location_str, is_remote=False):
    loc = (location_str or "").lower()
    if any(t in loc for t in _NON_CA_TERMS):
        return False
    if any(t in loc for t in CA_TERMS):
        return True
    if is_remote:
        return True
    return False


def _parse_location(location_str, is_remote=False):
    loc = (location_str or "").lower()
    city_map = {
        "burlington": "Burlington, VT",
        "south burlington": "South Burlington, VT",
        "montpelier": "Montpelier, VT",
        "rutland": "Rutland, VT",
    }
    for k, v in city_map.items():
        if k in loc:
            return v
    if "remote" in loc or is_remote:
        return "Remote (Vermont)"
    return "Vermont, VT"


def discover_slugs(seed_set):
    discovered = set()
    for i, query in enumerate(DISCOVERY_QUERIES, 1):
        log(f"  Discovery Exa [{i}/{len(DISCOVERY_QUERIES)}]: {query[:60]}...")
        resp = exa_search(query, num_results=10, log=log)
        if not resp:
            continue
        new = 0
        for r in resp.get("results", []):
            m = ASHBY_SLUG_RE.search(r.get("url", ""))
            if not m:
                continue
            slug = m.group(1).lower().split("/")[0]
            if slug in _SKIP_SLUGS or slug in seed_set or len(slug) < 2:
                continue
            discovered.add(slug)
            new += 1
        log(f"    → {new} new slugs")
        time.sleep(1.5)
    return discovered


def main():
    if not acquire_lock(LOCK_FILE, log):
        return 1

    log("=== Vermont Ashby scraper started ===")
    log(f"Output: {OUTPUT_FILE}")

    seed_set = {s for s, _ in SEED_SLUGS}
    log(f"Running Exa discovery ({len(DISCOVERY_QUERIES)} queries)...")
    extra_slugs = discover_slugs(seed_set)
    log(f"  {len(SEED_SLUGS)} seed + {len(extra_slugs)} discovered = "
        f"{len(SEED_SLUGS) + len(extra_slugs)} total slugs")

    existing_keys = load_existing_keys()
    seen_keys = set(existing_keys)
    seen_urls = load_existing_urls()
    remote_claimed = _load_remote_claimed()
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    all_slugs = list(SEED_SLUGS) + [(s, s.replace("-", " ").title()) for s in sorted(extra_slugs)]

    total_found = 0
    api_failures = 0
    discovered_slug_yield = {}

    for slug, company_display in all_slugs:
        jobs = _gql_fetch(slug)
        if jobs is None:
            api_failures += 1
            time.sleep(2)
            continue
        if not jobs:
            log(f"── {company_display} ({slug}): no postings")
            time.sleep(1)
            continue

        log(f"\n── {company_display} ({slug}): {len(jobs)} postings ──")
        ca_count = 0
        found_this = 0

        for job in jobs:
            loc_name = job.get("locationName", "") or ""
            is_remote = bool(job.get("isRemote"))
            if not _is_ca(loc_name, is_remote):
                continue
            ca_count += 1

            title = (job.get("title") or "").strip()
            if not title:
                continue

            ashby_url = f"https://jobs.ashbyhq.com/{slug}/{job.get('id', '')}"
            if ashby_url in seen_urls:
                continue
            key = f"{title.lower()}|{company_display.lower()}"
            if key in seen_keys:
                continue

            if is_remote:
                if key in remote_claimed and remote_claimed[key] != REGION_CODE:
                    log(f"  [SKIP remote dupe] {title[:50]} (claimed by {remote_claimed[key]})")
                    continue
                remote_claimed[key] = REGION_CODE
                _save_remote_claimed(remote_claimed)

            salary = _parse_salary(job.get("compensationTierSummary", ""))
            if not salary:
                job_id = job.get("id", "")
                if job_id:
                    salary = _fetch_job_salary(slug, job_id)
                    time.sleep(0.5)

            if not salary:
                log(f"  [{title[:50]}] → no salary")
                continue

            vmin, vmax = salary
            posted = TODAY
            date_m = re.search(r'(\d{4}-\d{2}-\d{2})', job.get("publishedDate") or "")
            if date_m:
                posted = date_m.group(1)

            job_out = {
                "role":            title,
                "company":         company_display,
                "min":             vmin,
                "max":             vmax,
                "location":        _parse_location(loc_name, is_remote),
                "source_url":      ashby_url,
                "posted":          posted,
                "source_platform": "ashby",
            }

            write_job(OUTPUT_FILE, job_out)
            seen_keys.add(key)
            seen_urls.add(ashby_url)
            total_found += 1
            found_this += 1
            log(f"  FOUND: {title[:50]} | ${vmin:,}–${vmax:,} [{loc_name}]")

        log(f"  VE: {ca_count} | New w/ salary: {found_this}")
        if slug in extra_slugs:
            discovered_slug_yield[slug] = found_this
        time.sleep(2)

    log(f"\n=== CA Ashby scraper complete: {total_found} new jobs (api_failures={api_failures}) ===")

    newly_qualified = {
        s: c for s, c in discovered_slug_yield.items()
        if s not in seed_set and c >= 1
    }
    if newly_qualified:
        log(f"\nAuto-injecting {len(newly_qualified)} high-yield slug(s) into SEED_SLUGS:")
        script_path = os.path.abspath(__file__)
        try:
            source = open(script_path).read()
            new_lines = []
            for slug, count in sorted(newly_qualified.items(), key=lambda x: -x[1]):
                display = slug.replace("-", " ").title()
                entry = f'    ("{slug}", "{display}"),'
                if entry[:20] in source:
                    continue
                log(f"  + {slug} ({count} CA+salary jobs)")
                new_lines.append(f'{entry}  # auto-discovered {TODAY}')
            if new_lines:
                marker = ']\n\nDISCOVERY_QUERIES'
                source = source.replace(marker, "\n" + "\n".join(new_lines) + "\n" + marker)
                open(script_path, "w").write(source)
        except Exception as e:
            log(f"  Auto-inject error: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
