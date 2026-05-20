#!/usr/bin/env python3
"""
vt-pay-hub/scripts/update-jobs.py
Daily job data updater — Vermont Pay Hub edition.
"""

import glob
import json
import os
import re
import sys
import time
import urllib.request
from datetime import date, datetime

TODAY = date.today().isoformat()
TIMESTAMP = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
REPO_DIR   = os.path.expanduser("~/vt-pay-hub")
DATA_FILE  = os.path.join(REPO_DIR, "data", "jobs.json")
SHARED_DIR = os.path.expanduser("~/.openclaw/shared")
RAW_PATTERN = os.path.join(SHARED_DIR, "vt-jobs-raw-*.txt")

CATEGORY_MAP = {
    "engineer": "Engineering", "developer": "Engineering", "software": "Engineering",
    "data": "Data & Analytics", "analyst": "Data & Analytics", "scientist": "Data & Analytics",
    "product": "Product", "design": "Design", "designer": "Design",
    "marketing": "Marketing", "finance": "Finance", "accounting": "Finance",
    "sales": "Sales", "operations": "Operations",
    "hr": "People & HR", "people": "People & HR", "recruiting": "People & HR",
    "legal": "Legal", "compliance": "Legal",
    "security": "Security", "manager": "Management", "director": "Management", "vp": "Management",
}


def classify_category(role: str) -> str:
    r = role.lower()
    for keyword, category in CATEGORY_MAP.items():
        if keyword in r:
            return category
    return "Other"


def classify_job(job: dict) -> dict:
    width = job.get("max", 0) - job.get("min", 0)
    job["_width"] = width
    job["_compliant"] = True
    job["_nonCompliant"] = False
    job["_exempt"] = False
    job["_federal"] = False
    if "category" not in job or not job["category"]:
        job["category"] = classify_category(job.get("role", ""))
    # Infer work_mode from title + location keywords
    role_loc = (job.get("role", "") + " " + job.get("location", "")).lower()
    if any(t in role_loc for t in ("remote", "work from home", "wfh", "distributed", " anywhere")):
        job.setdefault("work_mode", "remote")
    elif "hybrid" in role_loc:
        job.setdefault("work_mode", "hybrid")
    elif any(t in role_loc for t in ("on-site", "onsite", "in-person", "in office", "in-office")):
        job.setdefault("work_mode", "onsite")
    else:
        job.setdefault("work_mode", "unknown")

    # Infer salary_type: hourly if min < 500, else annual
    vmin = job.get("min", 0) or 0
    job.setdefault("salary_type", "hourly" if 0 < vmin < 500 else "annual")

    return job


def validate_link(url: str, timeout: int = 10) -> bool:
    if not url:
        return False
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "Mozilla/5.0 (compatible; payhub-bot/1.0)")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status < 400
    except Exception:
        return False


def load_existing() -> dict:
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except Exception:
        return {"meta": {}, "jobs": []}


def save_data(db: dict):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE + ".tmp", "w") as f:
        json.dump(db, f, ensure_ascii=False)
    os.replace(DATA_FILE + ".tmp", DATA_FILE)


def main():
    db = load_existing()
    jobs = db.get("jobs", [])

    existing_urls = {j.get("source_url", "") for j in jobs}
    existing_keys = {
        f"{j['role'].lower().strip()}|{j['company'].lower().strip()}"
        for j in jobs
    }

    raw_files = sorted(glob.glob(RAW_PATTERN))
    print(f"Found {len(raw_files)} raw file(s): {[os.path.basename(f) for f in raw_files]}")

    new_jobs = []
    for raw_file in raw_files:
        with open(raw_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    job = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if job.get("source_url") in existing_urls:
                    continue
                key = f"{job.get('role','').lower().strip()}|{job.get('company','').lower().strip()}"
                if key in existing_keys:
                    continue

                try:
                    val_min = int(job["min"])
                    val_max = int(job["max"])
                except (KeyError, ValueError):
                    continue
                if not (30_000 <= val_min < val_max <= 1_500_000):
                    continue

                job.setdefault("currency", "USD")
                job.setdefault("region", "VT")

                job = classify_job(job)
                job["id"] = re.sub(r'[^a-z0-9]', '-', job.get('role','').lower()[:20]) + '-' + \
                             re.sub(r'[^a-z0-9]', '-', job.get('company','').lower()[:15]) + '-' + \
                             str(int(time.time() * 1000))[-8:]
                job["status"] = "active"
                job["date_added"] = TODAY
                job.setdefault("scraped", TODAY)
                job.setdefault("last_seen", TODAY)
                if "posted" not in job:
                    job["posted"] = TODAY

                new_jobs.append(job)
                existing_keys.add(key)
                existing_urls.add(job.get("source_url", ""))

    print(f"New jobs from raw files: {len(new_jobs)}")

    archived_count = 0
    active_jobs = [j for j in jobs if j.get("status") == "active"]
    to_check = [j for j in active_jobs if j.get("date_added", "") < TODAY][:30]
    for j in to_check:
        url = j.get("source_url", "")
        if url and not validate_link(url):
            j["status"] = "archived"
            j["archived_date"] = TODAY
            archived_count += 1

    jobs = jobs + new_jobs

    for j in jobs:
        if j.get("status") == "active":
            j = classify_job(j)

    active_count = sum(1 for j in jobs if j.get("status") == "active")

    db["jobs"] = jobs
    db["meta"] = {
        "count": len(jobs),
        "active": active_count,
        "new_today": len(new_jobs),
        "links_newly_archived": archived_count,
        "updated": TIMESTAMP,
        "state": "Vermont",
        "law": "⚖️ VT Act 97 / S.296 (2025)",
        "currency": "USD",
        "region": "VT",
    }

    save_data(db)
    print(f"Saved: {len(jobs)} total, {active_count} active, {len(new_jobs)} new, {archived_count} archived")

    for f in raw_files:
        try:
            os.remove(f)
        except OSError:
            pass

    return len(new_jobs)


if __name__ == "__main__":
    n = main()
    sys.exit(0)
