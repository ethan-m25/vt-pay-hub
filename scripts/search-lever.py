#!/usr/bin/env python3
"""
vt-pay-hub/scripts/search-lever.py
Lever job board scraper — Vermont edition.
Vermont Act 97 (S.296) — effective Jul 1, 2025 — all employers must include pay range or pay rate + general job description
Run: python3 ~/vt-pay-hub/scripts/search-lever.py
"""

import html as html_mod
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from _common import (
    make_logger, acquire_lock, exa_search, load_existing_keys,
    write_job, TODAY, OUTPUT_FILE, REGION_TERMS, _NON_REGION_LOC_TERMS,
)

from scrapling import Fetcher

LOG_FILE  = os.path.expanduser("~/vt-pay-hub/scripts/lever.log")
LOCK_FILE = os.path.expanduser("~/vt-pay-hub/scripts/.lever.lock")
LOOKBACK_DATE = (date.today() - timedelta(days=60)).isoformat() + "T00:00:00.000Z"

log = make_logger(LOG_FILE)
fetcher = Fetcher()

SEED_SLUGS = [
    "betterlifepartners",
    "submittable",
    "caseiq",
    "torc-robotics",
    "uvm-health-network",
    "globalfoundries",
    "kindhealth",
]
SEED_SLUGS = list(dict.fromkeys(SEED_SLUGS))


DISCOVERY_QUERIES = [
    'site:jobs.lever.co "Vermont" OR "Burlington, VT" OR "Montpelier" OR "South Burlington" salary range 2025 2026',
    'site:jobs.lever.co "Vermont" OR "Burlington, VT" OR "Montpelier" OR "South Burlington" engineer OR analyst OR manager salary',
    'site:jobs.lever.co "Vermont" OR "Burlington, VT" OR "Montpelier" OR "South Burlington" "pay range" 2025 2026',
]


SALARY_RE = [
    re.compile(r'\$\s*([\\d,]+)(?:\.\d+)?\s*(?:USD|CAD|usd|cad)?\s*[-–—to]+\s*\$\s*([\\d,]+)', re.IGNORECASE),
    re.compile(r'\$([\\d]+(?:\.\d+)?)[kK]\s*[-–—]\s*\$([\\d]+(?:\.\d+)?)[kK]', re.IGNORECASE),
    re.compile(r'(?:pay|salary|compensation|base|wage|range)[^$\n]{0,50}\$?([\\d,]{5,})\s*[-–—to]+\s*\$?([\\d,]{5,})', re.IGNORECASE),
]

LEVER_SLUG_RE = re.compile(r'https?://jobs\.lever\.co/([a-zA-Z0-9._-]+)', re.IGNORECASE)
_SKIP_SLUGS = {'jobs', 'search', 'home', 'usasurveyjob'}


def discover_slugs(seed_slugs):
    known = set(seed_slugs)
    discovered = set()
    for i, query in enumerate(DISCOVERY_QUERIES, 1):
        log(f"  Discovery Exa [{i}/{len(DISCOVERY_QUERIES)}]: {query[:60]}...")
        resp = exa_search(query, num_results=15, start_date=LOOKBACK_DATE, log=log)
        if not resp:
            continue
        new = 0
        for r in resp.get("results", []):
            m = LEVER_SLUG_RE.search(r.get("url", ""))
            if not m:
                continue
            slug = m.group(1).lower()
            if slug in _SKIP_SLUGS or slug in known or len(slug) < 2:
                continue
            discovered.add(slug)
            new += 1
        log(f"    → {new} new slugs")
        time.sleep(1.5)
    return discovered


def fetch_company_jobs(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        page = fetcher.get(url, timeout=20)
        if page.status != 200:
            return []
        return page.json() or []
    except Exception as e:
        log(f"  API error ({slug}): {e}")
        return []


def is_vt_location(location_str: str, desc_text: str = "") -> bool:
    loc = (location_str or "").lower()

    if any(t in loc for t in _NON_REGION_LOC_TERMS):
        if not any(t in loc for t in REGION_TERMS):
            return False

    if any(t in loc for t in REGION_TERMS):
        return True

    if not loc or any(r in loc for r in ("remote", "distributed", "anywhere", "virtual", "work from")):
        return True

    return False


def parse_location(location_str: str) -> str:
    loc = (location_str or "").lower()
    for term in REGION_TERMS:
        if term.startswith(", ") or term.endswith(","):
            continue
        if term in loc:
            t = term.strip()
            if ", " in t:
                return t.title()
            return f"{t.title()}, VT"
    if "remote" in loc or not loc:
        return "Remote (VT)"
    return "Burlington, Vt, VT"


def extract_salary_from_range(sal_range):
    if not sal_range:
        return None
    currency = sal_range.get("currency", "").upper()
    if currency not in ("USD", ""):
        return None
    if sal_range.get("interval", "") != "per-year-salary":
        return None
    try:
        vmin = int(float(sal_range["min"]))
        vmax = int(float(sal_range["max"]))
        if 30_000 <= vmin <= 2_000_000 and vmin < vmax:
            return vmin, vmax
    except (KeyError, ValueError, TypeError):
        pass
    return None


def extract_salary_from_text(text):
    if not text:
        return None
    clean = html_mod.unescape(re.sub(r'<[^>]+>', ' ', text))
    clean = html_mod.unescape(re.sub(r'\s+', ' ', clean).strip())
    for pat in SALARY_RE:
        m = pat.search(clean)
        if m:
            try:
                raw_min = m.group(1).replace(",", "")
                raw_max = m.group(2).replace(",", "")
                if "k" in m.group(0).lower():
                    vmin = int(float(raw_min) * 1000)
                    vmax = int(float(raw_max) * 1000)
                else:
                    vmin = int(float(raw_min))
                    vmax = int(float(raw_max))
                if 30_000 <= vmin <= 2_000_000 and vmin < vmax:
                    return vmin, vmax
            except (ValueError, IndexError):
                continue
    return None


def main():
    if not acquire_lock(LOCK_FILE, log):
        return 1

    log("=== VT Lever scraper started ===")
    log(f"Output: {OUTPUT_FILE}")

    log(f"Running discovery ({len(DISCOVERY_QUERIES)} queries)...")
    extra_slugs = discover_slugs(SEED_SLUGS)
    log(f"  {len(SEED_SLUGS)} seed + {len(extra_slugs)} discovered = {len(SEED_SLUGS) + len(extra_slugs)} total slugs")

    existing_keys = load_existing_keys()
    seen_keys = set(existing_keys)
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    total_found = 0
    api_failures = 0
    discovered_slug_yield = {}
    all_slugs = list(SEED_SLUGS) + sorted(extra_slugs)

    for slug in all_slugs:
        jobs = fetch_company_jobs(slug)
        if not jobs:
            log(f"── {slug}: no jobs or API error")
            api_failures += 1
            time.sleep(1)
            continue

        company_name = slug.replace("-", " ").replace("_", " ").replace(".", " ").title()
        log(f"\n── {company_name} ({slug}): {len(jobs)} postings ──")
        region_count = 0
        found_this = 0

        for job in jobs:
            cats = job.get("categories") or {}
            loc_name = cats.get("location", "") or cats.get("allLocations", "")
            if isinstance(loc_name, list):
                loc_name = ", ".join(loc_name)

            desc_plain = job.get("descriptionPlain") or ""
            if not is_vt_location(loc_name, desc_plain):
                continue
            region_count += 1

            title = (job.get("text") or "").strip()
            if not title:
                continue

            key = f"{title.lower()}|{company_name.lower()}"
            if key in seen_keys:
                continue

            salary = extract_salary_from_range(job.get("salaryRange"))
            if not salary:
                sal_desc = job.get("salaryDescriptionPlain") or job.get("salaryDescription") or ""
                salary = extract_salary_from_text(sal_desc) or extract_salary_from_text(desc_plain)

            if not salary:
                log(f"  [{title[:50]}] → no salary")
                continue

            vmin, vmax = salary
            job_id = job.get("id", "")
            abs_url = f"https://jobs.lever.co/{slug}/{job_id}" if job_id else ""

            posted = TODAY
            created_ms = job.get("createdAt")
            if created_ms:
                try:
                    posted = datetime.fromtimestamp(
                        int(created_ms) / 1000, tz=timezone.utc
                    ).strftime("%Y-%m-%d")
                except (ValueError, OSError):
                    pass

            job_out = {
                "role":            title,
                "company":         company_name,
                "min":             vmin,
                "max":             vmax,
                "location":        parse_location(loc_name),
                "source_url":      abs_url,
                "posted":          posted,
                "source_platform": "lever",
            }

            write_job(OUTPUT_FILE, job_out)
            seen_keys.add(key)
            total_found += 1
            found_this += 1
            log(f"  FOUND: {title[:50]} | ${vmin:,}–${vmax:,} [{loc_name}]")

        log(f"  VT: {region_count} | New w/ salary: {found_this}")
        if slug in extra_slugs:
            discovered_slug_yield[slug] = found_this
        time.sleep(2)

    log(f"\n=== Lever scraper complete: {total_found} new jobs (api_failures={api_failures}) ===")

    seed_set = set(SEED_SLUGS)
    newly_qualified = {
        slug: count
        for slug, count in discovered_slug_yield.items()
        if slug not in seed_set and count >= 3
    }
    if newly_qualified:
        log(f"\nAuto-injecting {len(newly_qualified)} high-yield slug(s) into SEED_SLUGS:")
        script_path = os.path.abspath(__file__)
        try:
            source = open(script_path).read()
            new_lines = []
            for slug, count in sorted(newly_qualified.items(), key=lambda x: -x[1]):
                if f'"{slug}"' in source:
                    continue
                log(f"  + {slug} ({count} VT+salary jobs)")
                new_lines.append(f'    "{slug}",  # auto-discovered {TODAY} — {count} VT+salary')
            if new_lines:
                insert_block = "\n".join(new_lines)
                marker = "\nSEED_SLUGS = list(dict.fromkeys(SEED_SLUGS))"
                if marker in source:
                    source = source.replace(marker, f"\n{insert_block}{marker}")
                    open(script_path, "w").write(source)
        except Exception as e:
            log(f"  Auto-inject error: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
