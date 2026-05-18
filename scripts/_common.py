#!/usr/bin/env python3
"""
vt-pay-hub/scripts/_common.py
Shared utilities for all Vermont Pay Hub search scripts.
Vermont Act 97 (S.296) — effective Jul 1, 2025 — all employers must include pay range or pay rate + general job description
"""

import atexit
import json
import os
import re
import signal
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

socket.setdefaulttimeout(20)

EXA_API_KEY   = os.environ.get("EXA_API_KEY", "d0d9614a-58d8-4166-9b27-4ae6b6e2761e")
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "BSAodGE-EMqeQg5P6m4SW2pFXfrD06r")

_exa_exhausted = False
OLLAMA_API  = "http://127.0.0.1:11434/api/generate"
MODEL       = "qwen2.5:14b"
TODAY       = date.today().isoformat()
SHARED_DIR  = os.path.expanduser("~/.openclaw/shared")
OUTPUT_FILE = os.path.join(SHARED_DIR, f"vt-jobs-raw-{TODAY}.txt")
DATA_FILE   = os.path.expanduser("~/vt-pay-hub/data/jobs.json")
CURRENCY    = "USD"
REGION      = "VT"

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

SKIP_PATTERNS = [
    "glassdoor.com/Salary", "payscale.com", "salary.com",
    "indeed.com/salary", "ziprecruiter.com/Salaries",
    "linkedin.com/jobs/search", "linkedin.com/jobs/?",
    "monster.com/jobs/search", "simplyhired.com/search",
    "myworkdayjobs.com",
    "newswire.com", "businesswire.com", "prnewswire.com",
    "/press-release", "/newsroom/", "/investor-relations",
    "/annual-report", "/media-advisory",
    "talent.com/salary", "levels.fyi", "finance.yahoo.com",
]

_JOB_PAGE_MARKERS = [
    "apply", "qualifications", "requirements", "responsibilities",
    "salary range", "compensation range", "salary:", "we are looking",
    "job description", "about the role", "what you will do",
    "what we offer", "about you", "your responsibilities",
    "minimum qualifications", "preferred qualifications",
    "vt",
]


def is_job_page(text: str) -> bool:
    if not text or len(text) < 300:
        return False
    t = text.lower()
    return sum(1 for m in _JOB_PAGE_MARKERS if m in t) >= 2


REGION_TERMS = ['vermont', 'burlington, vt', 'montpelier', 'south burlington', 'barre, vt', 'rutland, vt', 'brattleboro', 'bennington, vt', 'st. johnsbury', 'middlebury, vt', 'stowe, vt', 'woodstock, vt', 'colchester, vt', 'essex junction', 'williston, vt', ', vt', 'vt,']

_NON_REGION_LOC_TERMS = ['toronto', 'vancouver', 'ontario, canada', 'canada,', 'washington, dc', 'washington dc', 'denver, co', 'boulder, co', 'seattle, wa', 'washington state', 'new york city', 'new york, ny', 'san francisco, ca', 'chicago, il', 'boston, ma', 'newark, nj', 'baltimore, md', 'minneapolis, mn', 'burlington, ma', 'burlington, nc']

_REMOTE_TERMS = ("remote", "distributed", "virtual", "anywhere", "work from", "wfh")


def make_logger(log_file):
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    def log(msg):
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(log_file, "a") as f:
            f.write(line + "\n")

    return log


def acquire_lock(lock_file, log):
    if os.path.exists(lock_file):
        try:
            with open(lock_file) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)
            log(f"Another instance is already running (PID {old_pid}). Exiting.")
            return False
        except (OSError, ValueError):
            log("Stale lock file — removing.")
            os.remove(lock_file)

    with open(lock_file, "w") as f:
        f.write(str(os.getpid()))

    def _release():
        try:
            os.remove(lock_file)
        except OSError:
            pass

    atexit.register(_release)
    signal.signal(signal.SIGTERM, lambda s, f: (_release(), sys.exit(1)))
    return True


def exa_search(query, num_results=10, start_date=None, log=None):
    global _exa_exhausted
    if _exa_exhausted:
        return None

    payload = {
        "query": query,
        "numResults": num_results,
        "type": "auto",
        "contents": {"text": {"maxCharacters": 2000}},
    }
    if start_date:
        payload["startPublishedDate"] = start_date

    req = urllib.request.Request(
        "https://api.exa.ai/search",
        data=json.dumps(payload).encode(),
        method="POST",
    )
    req.add_header("x-api-key", EXA_API_KEY)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", _UA)

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 402:
            _exa_exhausted = True
            if log:
                log("  Exa credits exhausted (402) — switching to Brave Search")
        else:
            if log:
                log(f"  Exa HTTP {e.code}: {e}")
        return None
    except Exception as e:
        if log:
            log(f"  Exa error: {e}")
        return None


def brave_search(query, num_results=10, log=None):
    params = urllib.parse.urlencode({"q": query, "count": min(num_results, 20)})
    req = urllib.request.Request(
        f"https://api.search.brave.com/res/v1/web/search?{params}"
    )
    req.add_header("Accept", "application/json")
    req.add_header("Accept-Encoding", "gzip")
    req.add_header("X-Subscription-Token", BRAVE_API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            try:
                import gzip
                data = json.loads(gzip.decompress(raw))
            except Exception:
                data = json.loads(raw)
        results = data.get("web", {}).get("results", [])
        return {"results": [{"url": r.get("url", ""), "text": r.get("description", "")} for r in results]}
    except Exception as e:
        if log:
            log(f"  Brave error: {e}")
        return None


def fetch_html_text(url, timeout=15, user_agent=None, max_chars=4000,
                    skip_workday=True, min_content_len=0):
    if not url:
        return None
    if skip_workday and "myworkdayjobs.com" in url:
        return None
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", user_agent or _UA)
        req.add_header("Accept-Language", "en-US,en;q=0.9")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read().decode("utf-8", errors="ignore")
        html = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<style[^>]*>.*?</style>',  ' ', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:max_chars] if len(text) >= min_content_len else None
    except Exception:
        return None


EXTRACT_PROMPT = """\
Extract ONE Vermont job posting from the text below.

URL: {url}
Search snippet (may be from a different but related job — DO NOT use for salary numbers): {snippet}
Page text (authoritative — use THIS for all data including salary): {page_text}

Today's date: {today}

Return ONLY valid JSON in this exact format if a valid Vermont job with explicit USD salary range is found:
{"role":"Job Title","company":"Company Name","min":80000,"max":120000,"location":"Vermont location","source_url":"{url}","posted":"YYYY-MM-DD"}

Return ONLY the word null (no quotes, no JSON) if:
- No explicit USD annual salary range with actual dollar numbers in the PAGE TEXT
- Not a Vermont location
- This is a salary guide / aggregator page / company careers homepage
- Hourly rate only (do NOT convert hourly to annual)
- URL is a search results page

Rules:
- min and max = annual USD integers extracted from PAGE TEXT ONLY (e.g. 90000)
- NEVER use salary numbers from the snippet — only use page text salary data
- location must be in Vermont
- posted = date visible in posting, or {today} if not shown
- source_url = exact URL of this specific job posting"""


def _call_ollama(prompt):
    payload = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0, "num_predict": 256},
    }).encode()
    req = urllib.request.Request(OLLAMA_API, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read()).get("response", "").strip()


def extract_job(url, snippet, page_text, log=None):
    prompt = EXTRACT_PROMPT.format(
        url=url,
        snippet=(snippet or "")[:600],
        page_text=(page_text or "")[:3000],
        today=TODAY,
    )

    output = None
    for attempt in range(2):
        try:
            output = _call_ollama(prompt)
            break
        except Exception as e:
            if attempt == 0:
                if log:
                    log(f"  Ollama attempt 1 failed ({type(e).__name__}: {e}) — retrying in 5s")
                time.sleep(5)
            else:
                if log:
                    log(f"  Ollama failed after retry: {e}")
                return None

    if not output or re.match(r'^null$', output, re.IGNORECASE):
        return None

    m = re.search(r'\{[^{}]*"role"[^{}]*\}', output, re.DOTALL)
    if not m:
        return None

    try:
        job = json.loads(m.group())
    except json.JSONDecodeError:
        return None

    for k in ("role", "company", "min", "max", "source_url"):
        if k not in job:
            return None

    try:
        val_min, val_max = int(job["min"]), int(job["max"])
    except (ValueError, TypeError):
        return None

    if not (30_000 <= val_min <= 1_000_000) or val_min >= val_max:
        return None

    if not any(t in job.get("location", "").lower() for t in REGION_TERMS):
        return None

    if page_text:
        def _in_text(val):
            s = str(val)
            k = str(val // 1000)
            return (
                re.search(r'[,\s$]' + s[:3], page_text) is not None
                or re.search(s.replace("000", "[,.]?000"), page_text) is not None
                or re.search(rf'\b{k}[kK]\b', page_text) is not None
            )
        if not (_in_text(val_min) or _in_text(val_max)):
            if log:
                log(f"  Salary {val_min:,}–{val_max:,} not found in page text (snippet hallucination) — skip")
            return None

    return job


def load_existing_keys():
    try:
        with open(DATA_FILE) as f:
            db = json.load(f)
        return {
            f"{j['role'].lower().strip()}|{j['company'].lower().strip()}"
            for j in db.get("jobs", [])
        }
    except Exception:
        return set()


def collect_candidates(queries, num_results, log, start_date=None, skip=None):
    if skip is None:
        skip = SKIP_PATTERNS
    candidates = {}
    for i, query in enumerate(queries, 1):
        if _exa_exhausted:
            log(f"Brave [{i:2d}/{len(queries)}]: {query[:65]}...")
            resp = brave_search(query, num_results=num_results, log=log)
        else:
            log(f"Exa [{i:2d}/{len(queries)}]: {query[:65]}...")
            resp = exa_search(query, num_results=num_results, start_date=start_date, log=log)
            if resp is None and _exa_exhausted:
                log(f"Brave [{i:2d}/{len(queries)}] (retry): {query[:65]}...")
                resp = brave_search(query, num_results=num_results, log=log)
        if not resp:
            continue
        results = resp.get("results", [])
        log(f"  → {len(results)} results")
        for r in results:
            url = r.get("url", "").strip()
            if not url or url in candidates:
                continue
            if any(p in url for p in skip):
                continue
            candidates[url] = (r.get("text") or "")[:600]
        time.sleep(1.5)
    return candidates


def write_job(output_file, job):
    job.setdefault("currency", CURRENCY)
    job.setdefault("region",   REGION)
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "a") as f:
        f.write(json.dumps(job, ensure_ascii=False) + "\n")
