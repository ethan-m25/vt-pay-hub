#!/usr/bin/env python3
"""
vt-pay-hub/scripts/search-greenhouse.py
Greenhouse job board scraper — Vermont edition.
Vermont Act 97 (S.296) — effective Jul 1, 2025 — all employers must include pay range or pay rate + general job description
Run: python3 ~/vt-pay-hub/scripts/search-greenhouse.py
"""

import html as html_mod
import json
import os
import re
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from _common import (
    make_logger, acquire_lock, load_existing_keys,
    write_job, TODAY, OUTPUT_FILE, REGION_TERMS, _NON_REGION_LOC_TERMS,
)

from scrapling import Fetcher

LOG_FILE  = os.path.expanduser("~/vt-pay-hub/scripts/greenhouse.log")
LOCK_FILE = os.path.expanduser("~/vt-pay-hub/scripts/.greenhouse.lock")
LOOKBACK_DATE = (date.today() - timedelta(days=60)).isoformat() + "T00:00:00.000Z"

log = make_logger(LOG_FILE)
fetcher = Fetcher()

SEED_SLUGS = [
    ("vipvermontinformationprocessing2", "Vermont Info Processing"),
    ("submittable", None),
    ("globalfoundries", "GlobalFoundries"),
]


SALARY_PATTERNS = [
    r'\$\s*([\\d,]+)\s*[-–—]\s*\$\s*([\\d,]+)',
    r'([\\d,]+)\s*[-–—]\s*([\\d,]+)\s*(?:USD|usd)',
    r'salary[:\s]+\$?([\\d,]+)[kK]?\s*[-–—]\s*\$?([\\d,]+)[kK]?',
    r'compensation[:\s]+\$?([\\d,]+)[kK]?\s*[-–—]\s*\$?([\\d,]+)[kK]?',
    r'pay range[:\s]+\$?([\\d,]+)[kK]?\s*[-–—]\s*\$?([\\d,]+)[kK]?',
    r'"salary_min":\s*(\d+).*?"salary_max":\s*(\d+)',
    r'"min_salary":\s*(\d+).*?"max_salary":\s*(\d+)',
]


def parse_salary_from_text(text: str):
    if not text:
        return None, None
    text = html_mod.unescape(html_mod.unescape(text))
    for pat in SALARY_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            try:
                raw_min = m.group(1).replace(",", "")
                raw_max = m.group(2).replace(",", "")
                val_min = int(float(raw_min))
                val_max = int(float(raw_max))
                if val_min < 1000:
                    val_min *= 1000
                if val_max < 1000:
                    val_max *= 1000
                if 30_000 <= val_min < val_max <= 1_500_000:
                    return val_min, val_max
            except (ValueError, IndexError):
                continue
    return None, None


def is_vt_job(title: str, location: str, content: str) -> bool:
    loc_low = location.lower()
    content_low = (content or "").lower()

    if any(t in loc_low for t in _NON_REGION_LOC_TERMS):
        return False

    if any(t in loc_low for t in REGION_TERMS):
        return True

    if any(t in content_low for t in REGION_TERMS) and (
        "salary range" in content_low or "pay range" in content_low or "compensation range" in content_low
    ):
        return True

    if not loc_low or any(r in loc_low for r in ("remote", "distributed", "virtual", "anywhere", "work from", "wfh")):
        return True

    return False


def parse_location(location: str) -> str:
    loc = (location or "").lower()
    for term in REGION_TERMS:
        if term.startswith(", ") or term.endswith(","):
            continue
        if term in loc:
            t = term.strip()
            if ", " in t:
                return t.title()
            return f"{t.title()}, VT"
    if "remote" in loc:
        return "Remote (VT)"
    return "Burlington, Vt, VT"


def fetch_company_jobs(slug: str, company_name_override=None):
    for board_base in [
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    ]:
        try:
            resp = fetcher.get(board_base, timeout=20)
            data = resp.json()
            if data.get("jobs"):
                break
        except Exception as e:
            log(f"  [{slug}] API error: {e}")
            data = {}

    jobs_raw = data.get("jobs", [])
    if not jobs_raw:
        return []

    company_name = company_name_override or data.get("company", {}).get("name") or slug.title()
    results = []

    for j in jobs_raw:
        updated_at = j.get("updated_at", "")
        if updated_at and updated_at < LOOKBACK_DATE:
            continue

        title = j.get("title", "").strip()
        location_obj = j.get("location", {})
        location = location_obj.get("name", "") if isinstance(location_obj, dict) else str(location_obj)
        content_html = j.get("content", "")
        content_text = re.sub(r'<[^>]+>', ' ', content_html)
        content_text = html_mod.unescape(content_text)

        if not is_vt_job(title, location, content_text):
            continue

        val_min, val_max = parse_salary_from_text(content_html + " " + content_text)
        if val_min is None:
            val_min, val_max = parse_salary_from_text(str(j))

        if val_min is None:
            continue

        posted_date = updated_at[:10] if updated_at else TODAY
        job_url = j.get("absolute_url") or f"https://boards.greenhouse.io/{slug}/jobs/{j.get('id','')}"

        results.append({
            "role": title,
            "company": company_name,
            "min": val_min,
            "max": val_max,
            "location": parse_location(location),
            "source_url": job_url,
            "posted": posted_date,
            "source_platform": "greenhouse",
        })

    return results


def main():
    if not acquire_lock(LOCK_FILE, log):
        return

    log("=== VT Greenhouse scraper started ===")
    existing = load_existing_keys()
    log(f"Existing dedup keys: {len(existing)}")

    new_count = 0
    for slug, name_override in SEED_SLUGS:
        log(f"[{slug}] fetching...")
        jobs = fetch_company_jobs(slug, name_override)
        for job in jobs:
            key = f"{job['role'].lower().strip()}|{job['company'].lower().strip()}"
            if key in existing:
                continue
            write_job(OUTPUT_FILE, job)
            existing.add(key)
            new_count += 1
            log(f"  + {job['role']} @ {job['company']} | ${job['min']:,}–${job['max']:,} | {job['location']}")
        time.sleep(0.5)

    log(f"=== Done. {new_count} new VT jobs written to {OUTPUT_FILE} ===")


if __name__ == "__main__":
    main()
