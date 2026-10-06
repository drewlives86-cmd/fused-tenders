[fetch_and_score.py](https://github.com/user-attachments/files/33103704/fetch_and_score.py)
# fused-tenders"""
FUSED Tender Intelligence — fetch, filter and score pipeline
==============================================================

Pulls live UK public procurement notices from the two free, key-less
government OCDS APIs and scores them for fit against BES/FUSED's
electrical compliance service lines (EICR, PAT, emergency lighting,
fire alarms, fixed wire testing, EV charging, solar PV, remedials).

Data sources (both official, no API key required, Open Government Licence):
  - Find a Tender (FTS)      https://www.find-tender.service.gov.uk/apidocumentation/1.0/GET-ocdsReleasePackages
                              Covers higher-value notices (England/Wales/NI central
                              government thresholds and above; Scotland via PCS mirror).
  - Contracts Finder (CF)    https://www.contractsfinder.service.gov.uk/apidocumentation/Notices/1/GET-Published-Notice-OCDS-Search
                              Covers lower-value notices (England, from ~£12k).

Neither source lets you search by keyword or CPV code server-side — you can only
filter by stage (planning / tender / award / implementation) and by publication
date window. So this script pulls candidate notices by stage/date and does the
keyword + CPV matching locally.

USAGE
-----
    pip install requests
    python fetch_and_score.py --days 30 --out opportunities.csv

KNOWN LIMITATION (found during build, 17 Jul 2026)
----------------------------------------------------
When testing this against the live APIs, Contracts Finder's "tender" and
"planning,tender" combined-stage queries sometimes returned a fixed small
batch of the most recent notices regardless of how far back `publishedFrom`
was set, rather than paging back through the full window. Single-stage
queries (stages=planning alone) behaved correctly and returned properly
date-filtered historical results. If you see suspiciously identical record
counts across different date windows, verify against the cursor/`links.next`
field and consider querying stages one at a time rather than combined. This
may be an API quirk, a caching layer, or specific to certain network paths —
worth confirming with a direct account on a production connection before
relying on it for full historical backfill.

This script is written defensively around that: it queries one stage at a
time and always follows the `links.next` cursor until exhausted, so on a
normal unrestricted connection it will genuinely walk the full date window.
"""

import argparse
import csv
import re
import sys
import time
from datetime import datetime, timedelta

import requests

FTS_BASE = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
CF_BASE = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"

# ---------------------------------------------------------------------------
# Electrical-compliance signal: keywords (text match) + CPV codes (structured
# match). CPV matching is more reliable than free text where available.
# ---------------------------------------------------------------------------

KEYWORDS_HIGH = [
    "eicr", "electrical installation condition report", "fixed wire test",
    "fixed wiring test", "portable appliance test", "pat testing",
    "emergency lighting", "fire alarm", "periodic inspection and testing",
    "rcd testing", "thermographic", "thermal imaging survey",
]
KEYWORDS_MED = [
    "electrical maintenance", "electrical compliance", "electrical remedial",
    "rewire", "rewiring", "distribution board", "switchgear",
    "lightning protection", "earthing and bonding", "ev charging",
    "electric vehicle charging", "solar pv", "photovoltaic",
    "statutory electrical", "bs 7671", "iet wiring regulations",
]
KEYWORDS_LOW = [
    "electrical services", "electrical installation", "electrical works",
    "electrical repairs", "m&e maintenance", "mechanical and electrical",
]

CPV_HIGH = {
    "45312100": "Fire-alarm system installation work",
    "31625000": "Burglar and fire alarms",
    "71631000": "Technical inspection services",
    "50532100": "Electric motor repair services",
}
CPV_MED = {
    "45311000": "Electrical wiring and fitting work",
    "45311100": "Electrical wiring work",
    "45311200": "Electrical fitting work",
    "45317000": "Other electrical installation work",
    "50116100": "Electrical-system repair services",
    "50711000": "Repair and maintenance services of electrical building installations",
    "71314100": "Electricity/Electrical services",
    "45310000": "Electrical installation work",
    "09331200": "Solar photovoltaic modules",
}

SECTOR_KEYWORDS = {
    "council": 15, "borough": 15, "county": 15, "city of": 15,
    "nhs": 15, "hospital": 15, "health board": 15,
    "housing": 14, "homes": 12, "trust": 12,
    "academy": 13, "school": 13, "college": 13, "university": 13,
    "fire and rescue": 12, "police": 10, "ministry of defence": 9,
}


def _text_score(text: str):
    """Return (points, matched_terms) from free-text keyword matching."""
    t = text.lower()
    matched = []
    points = 0
    for kw in KEYWORDS_HIGH:
        if kw in t:
            matched.append(kw)
            points = max(points, 30)
    for kw in KEYWORDS_MED:
        if kw in t:
            matched.append(kw)
            points = max(points, 20)
    for kw in KEYWORDS_LOW:
        if kw in t:
            matched.append(kw)
            points = max(points, 10)
    return points, matched


def _cpv_score(cpv_codes):
    points = 0
    matched = []
    for code, desc in cpv_codes:
        if code in CPV_HIGH:
            points = max(points, 28)
            matched.append(f"{code} {desc}")
        elif code in CPV_MED:
            points = max(points, 18)
            matched.append(f"{code} {desc}")
    return points, matched


def _value_score(amount):
    if not amount:
        return 8
    if amount >= 1_000_000:
        return 20
    if amount >= 250_000:
        return 16
    if amount >= 50_000:
        return 11
    return 6


def _stage_score(stage):
    return {"planning": 20, "tender": 16, "award": 6, "implementation": 3}.get(stage, 5)


def _sector_score(buyer_name):
    b = (buyer_name or "").lower()
    for kw, pts in SECTOR_KEYWORDS.items():
        if kw in b:
            return pts
    return 6


def score_release(release):
    """Score one OCDS release for FUSED electrical-compliance fit (0-100)."""
    tender = release.get("tender", {})
    title = tender.get("title", "") or ""
    desc = tender.get("description", "") or release.get("description", "") or ""
    full_text = f"{title}\n{desc}"

    cpv_codes = []
    cls = tender.get("classification")
    if cls and cls.get("scheme") == "CPV":
        cpv_codes.append((cls["id"], cls.get("description", "")))
    for item in tender.get("items", []):
        for ac in item.get("additionalClassifications", []):
            if ac.get("scheme") == "CPV":
                cpv_codes.append((ac["id"], ac.get("description", "")))

    text_pts, text_hits = _text_score(full_text)
    cpv_pts, cpv_hits = _cpv_score(cpv_codes)
    keyword_component = max(text_pts, cpv_pts)  # 0-30
    evidence = list(dict.fromkeys(text_hits + cpv_hits))  # de-dup, preserve order

    if keyword_component == 0:
        return None  # not an electrical-compliance match — drop it

    value = tender.get("value", {}).get("amount")
    value_component = _value_score(value)  # 0-20

    tag = release.get("tag", ["tender"])[0] if release.get("tag") else "tender"
    stage_component = _stage_score(tag)  # 0-20

    buyer_name = release.get("buyer", {}).get("name", "")
    sector_component = _sector_score(buyer_name)  # 0-15

    deadline = tender.get("tenderPeriod", {}).get("endDate")
    if deadline:
        try:
            dl = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            days_left = (dl - datetime.now(dl.tzinfo)).days
            deadline_component = 15 if 0 <= days_left <= 60 else (8 if days_left > 60 else 2)
        except ValueError:
            deadline_component = 8
    else:
        deadline_component = 8 if tag == "planning" else 4

    total = keyword_component + value_component + stage_component + sector_component + deadline_component
    total = min(100, total)

    if total >= 80:
        priority = "P1"
    elif total >= 60:
        priority = "P2"
    elif total >= 40:
        priority = "P3"
    else:
        priority = "Watch"

    return {
        "priority": priority,
        "fit_score": total,
        "buyer": buyer_name,
        "stage": tag,
        "title": title,
        "value_gbp": value,
        "deadline": deadline,
        "evidence": "; ".join(evidence[:5]),
        "ocid": release.get("ocid"),
        "source_id": release.get("id"),
    }


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _get(url, params, session, max_retries=4):
    for attempt in range(max_retries):
        r = session.get(url, params=params, headers={"Accept": "application/json"}, timeout=30)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 503):
            wait = int(r.headers.get("Retry-After", 5))
            time.sleep(wait)
            continue
        r.raise_for_status()
    raise RuntimeError(f"Failed after {max_retries} retries: {url}")


def fetch_cf(stage, date_from, date_to, session, max_pages=50):
    params = {
        "publishedFrom": date_from, "publishedTo": date_to,
        "stages": stage, "limit": 100,
    }
    url = CF_BASE
    for _ in range(max_pages):
        data = _get(url, params, session)
        yield from data.get("releases", [])
        next_link = data.get("links", {}).get("next")
        if not next_link:
            return
        url, params = next_link, None  # cursor URL already has all params baked in


def fetch_fts(stage, date_from, date_to, session, max_pages=50):
    params = {
        "updatedFrom": date_from, "updatedTo": date_to,
        "stages": stage, "limit": 100,
    }
    url = FTS_BASE
    for _ in range(max_pages):
        data = _get(url, params, session)
        yield from data.get("releases", [])
        next_link = data.get("links", {}).get("next") if isinstance(data.get("links"), dict) else None
        if not next_link:
            return
        url, params = next_link, None


def run(days, out_path):
    date_to = datetime.utcnow().strftime("%Y-%m-%dT23:59:59")
    date_from = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")

    session = requests.Session()
    results = []
    seen_ocids = set()

    for stage in ("planning", "tender"):
        for label, fetcher in (("Contracts Finder", fetch_cf), ("Find a Tender", fetch_fts)):
            print(f"Fetching {label} / stage={stage} / {date_from} -> {date_to} ...", file=sys.stderr)
            count = 0
            for release in fetcher(stage, date_from, date_to, session):
                count += 1
                ocid = release.get("ocid")
                if ocid in seen_ocids:
                    continue
                seen_ocids.add(ocid)
                scored = score_release(release)
                if scored:
                    scored["source_portal"] = label
                    results.append(scored)
            print(f"  -> {count} raw notices scanned", file=sys.stderr)

    results.sort(key=lambda r: r["fit_score"], reverse=True)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "priority", "fit_score", "buyer", "stage", "title", "value_gbp",
            "deadline", "evidence", "source_portal", "ocid", "source_id",
        ])
        writer.writeheader()
        writer.writerows(results)

    print(f"\nMatched {len(results)} electrical-compliance opportunities out of "
          f"{len(seen_ocids)} unique notices scanned.", file=sys.stderr)
    print(f"Written to {out_path}", file=sys.stderr)
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30, help="How many days back to scan")
    ap.add_argument("--out", default="opportunities.csv", help="Output CSV path")
    args = ap.parse_args()
    run(args.days, args.out)
