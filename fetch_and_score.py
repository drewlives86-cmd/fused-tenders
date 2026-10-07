"""
FUSED Tender Intelligence - daily fetch, score and publish.

Pulls notices from Find a Tender and Contracts Finder (free, key-less OCDS APIs),
keeps electrical-compliance matches, scores them 0-100 and writes:
  site/index.html        the dashboard (published to GitHub Pages)
  site/opportunities.csv CRM-ready export
  site/data.json         raw scored data

Usage:  pip install requests ; python fetch_and_score.py --days 45
"""
import argparse, csv, html, json, os, sys, time
from datetime import datetime, timedelta, timezone

import requests

FTS_BASE = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
CF_BASE = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
UA = {"Accept": "application/json", "User-Agent": "FUSED-Tender-Intelligence/1.0"}

KW_HIGH = ["eicr", "electrical installation condition report", "electrical condition report",
           "fixed wire", "fixed wiring", "fixed electrical installation",
           "portable appliance", "pat testing", "pat test", "appliance testing",
           "emergency lighting", "emergency light"]
KW_MED = ["electrical compliance", "electrical safety", "electrical testing", "electrical inspection",
          "periodic inspection and testing", "periodic electrical", "statutory electrical",
          "electrical certification", "rcd testing", "bs 7671", "bs7671", "thermographic",
          "thermal imaging", "electrical remedial", "electrical statutory"]
KW_LOW = []
EV_WORDS = ["ev charg", "electric vehicle charg", "charge point", "chargepoint", "charging point", "ev infrastructure"]
EV_ACTIONS = ["inspect", "testing", "periodic", "maintenance", "compliance", "certif"]
# CPV codes only add confidence to a keyword match; they never trigger a match alone.
CPV_HIGH = {"71631000", "71630000"}
CPV_MED = {"50711000", "71314100", "45310000"}
SECTOR = {"council": 15, "borough": 15, "county": 15, "city of": 15, "nhs": 15, "hospital": 15,
          "health board": 15, "housing": 14, "homes": 12, "trust": 12, "academy": 13, "school": 13,
          "college": 13, "university": 13, "fire and rescue": 12, "police": 10, "ministry of defence": 9}


def get(url, params, session):
    for attempt in range(5):
        r = session.get(url, params=params, headers=UA, timeout=60)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 403:      # Contracts Finder rate limit: wait 5 minutes
            print("  rate limited (403), waiting 5 min", file=sys.stderr); time.sleep(300); continue
        if r.status_code in (429, 503):
            time.sleep(int(r.headers.get("Retry-After", 10)) + 1); continue
        r.raise_for_status()
    raise RuntimeError("too many retries: " + url)


def walk(base, params, session, max_pages=80):
    url = base
    for _ in range(max_pages):
        data = get(url, params, session)
        yield from data.get("releases", [])
        links = data.get("links")
        nxt = links.get("next") if isinstance(links, dict) else None
        if not nxt:
            return
        url, params = nxt, None
        time.sleep(1)


def parse_dt(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def score(rel, portal, now):
    t = rel.get("tender") or {}
    title = t.get("title") or ""
    parts = [title, t.get("description") or "", rel.get("description") or ""]
    for lot in t.get("lots", []) or []:
        parts += [lot.get("title") or "", lot.get("description") or ""]
    text = "\n".join(parts).lower()

    cpvs = []
    c = t.get("classification")
    if c and c.get("scheme") == "CPV":
        cpvs.append((c["id"], c.get("description", "")))
    for it in t.get("items", []) or []:
        for ac in it.get("additionalClassifications", []) or []:
            if ac.get("scheme") == "CPV":
                cpvs.append((ac["id"], ac.get("description", "")))

    kw, ev = 0, []
    tl = title.lower()
    body = "\n".join(parts[1:]).lower()[:2500]
    # skip notices that are clearly something else (training courses, property leases, software)
    if any(x in tl for x in ("trainer", "training", "lease", "units ", "back office", "software", "licence", "licensing")):
        return None
    for lst, pts in ((KW_HIGH, 30), (KW_MED, 22)):
        for k in lst:
            if k in tl:
                kw = max(kw, pts); ev.append(k + " (in title)")
            elif k in body:
                kw = max(kw, int(pts * 0.55)); ev.append(k + " (in description)")
    for blob, full in ((tl, 26), (body, 15)):
        if any(w in blob for w in EV_WORDS) and any(w in blob for w in EV_ACTIONS):
            kw = max(kw, full); ev.append("EV charge point inspection/maintenance")
            break
    if kw == 0:
        return None
    for code, d in cpvs:
        if code in CPV_HIGH or code in CPV_MED:
            ev.append(f"CPV {code} {d}"); kw = min(30, kw + 2)

    tags = rel.get("tag") or ["tender"]
    if "tender" in tags or "tenderUpdate" in tags:
        tag = "tender"
    elif "planning" in tags or "planningUpdate" in tags:
        tag = "planning"
    else:
        return None

    # deadline / staleness
    dl = parse_dt((t.get("tenderPeriod") or {}).get("endDate") or "")
    future = parse_dt((t.get("communication") or {}).get("futureNoticeDate") or "")
    stale, status, dl_pts = False, "", 8 if tag == "planning" else 4
    if tag == "tender" and dl:
        days = (dl - now).days
        if days < 0:
            return None                      # closed tender, drop
        dl_pts = 15 if days <= 60 else 8
        status = f"Live - closes in {days} days"
    elif tag == "tender":
        status = "Live - no deadline stated"
    else:
        status = "Pre-market / pipeline"
        if dl and dl < now: stale, status, dl_pts = True, "Pipeline - stated deadline has passed, verify", 2
        elif future and future < now: stale, status, dl_pts = True, "Pipeline - expected notice date has passed, verify", 2

    val = (t.get("value") or {}).get("amount")
    vpts = 8 if not val else 20 if val >= 1e6 else 16 if val >= 250e3 else 11 if val >= 50e3 else 6
    buyer = (rel.get("buyer") or {}).get("name", "")
    bl = buyer.lower()
    spts = next((p for k, p in SECTOR.items() if k in bl), 6)
    total = min(100, kw + vpts + (20 if tag == "planning" else 16) + spts + dl_pts)
    prio = "P1" if total >= 80 else "P2" if total >= 60 else "P3" if total >= 40 else "Watch"
    if stale and prio == "P1": prio = "P2"
    if not any("(in title)" in e or e.startswith("EV charge") and kw >= 26 for e in ev) and prio in ("P1", "P2"):
        prio = "P3"

    if portal == "Contracts Finder":
        url = next((d.get("url") for d in t.get("documents", []) or [] if d.get("documentType") in ("tenderNotice",) or "Notice" in (d.get("description") or "")), "")
        if not url:
            url = ((rel.get("planning") or {}).get("documents") or [{}])[0].get("url", "")
    else:
        url = f"https://www.find-tender.service.gov.uk/Notice/{rel.get('id')}"

    contact = ""
    for p in rel.get("parties", []) or []:
        if "buyer" in (p.get("roles") or []):
            cp = p.get("contactPoint") or {}
            contact = " / ".join(x for x in (cp.get("name"), cp.get("email"), cp.get("telephone")) if x)
            break

    return dict(priority=prio, fit_score=total, buyer=buyer, stage=tag, status=status, stale=stale,
                title=title, value_gbp=val, deadline=dl.date().isoformat() if dl else "",
                evidence="; ".join(dict.fromkeys(ev))[:300], contact=contact, portal=portal,
                url=url, ocid=rel.get("ocid"), released=rel.get("date", ""))


def render(rows, stats, updated):
    E = html.escape
    def money(v): return f"£{v:,.0f}" if v else "not stated"
    cards = []
    for r in rows:
        stale = '<span class="stale">VERIFY STATUS</span>' if r["stale"] else ""
        cards.append(f"""<div class="card" data-p="{r['priority']}" data-s="{r['stage']}">
<div class="top"><div><span class="badge {r['priority']}">{r['priority']}</span><span class="score">Fit {r['fit_score']}/100</span>{stale}</div>
<div class="meta">Deadline: <b>{E(r['deadline'] or 'n/a')}</b> &middot; Value: <b>{money(r['value_gbp'])}</b></div></div>
<div class="title">{E(r['title'])}</div>
<div class="buyer">{E(r['buyer'])} &middot; {E(r['status'])} &middot; {E(r['portal'])}</div>
<div class="ev"><b>Evidence:</b> {E(r['evidence'])}</div>
{f'<div class="ev"><b>Buyer contact:</b> {E(r["contact"])}</div>' if r['contact'] else ''}
<div class="src"><a href="{E(r['url'])}" target="_blank" rel="noopener">Open source notice &#8599;</a></div></div>""")
    body = "\n".join(cards) or '<p>No matching opportunities in this window.</p>'
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FUSED Tender Intelligence</title><style>
:root{{--navy:#0f2540;--accent:#e8622c;--bg:#f4f6f8;--b:#dfe4ea;--m:#5c6b7a}}
*{{box-sizing:border-box}}body{{font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;margin:0;background:var(--bg);color:#1c2733}}
header{{background:linear-gradient(135deg,#0f2540,#16324f);color:#fff;padding:26px 32px}}header h1{{margin:0 0 6px;font-size:22px}}header p{{margin:0;color:#cdd8e3;font-size:14px}}
.wrap{{max-width:1200px;margin:0 auto;padding:22px 32px 60px}}.stats{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}}
.stat{{background:#fff;border:1px solid var(--b);border-radius:10px;padding:12px 16px;min-width:120px}}.stat .n{{font-size:22px;font-weight:700;color:var(--navy)}}.stat .l{{font-size:11px;color:var(--m);text-transform:uppercase}}
.filters{{margin-bottom:16px}}.filters button{{border:1px solid var(--b);background:#fff;border-radius:20px;padding:6px 14px;margin-right:6px;cursor:pointer;font-size:13px}}.filters button.on{{background:var(--navy);color:#fff}}
.card{{background:#fff;border:1px solid var(--b);border-radius:10px;padding:16px 18px;margin-bottom:12px}}.top{{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}}
.badge{{color:#fff;font-size:11px;font-weight:700;padding:3px 9px;border-radius:20px}}.P1{{background:#c0392b}}.P2{{background:#d98c1f}}.P3{{background:#2d7dd2}}.Watch{{background:#7f8c9a}}
.score,.meta{{font-size:13px;color:var(--m);margin-left:8px}}.meta{{margin:0}}.stale{{margin-left:8px;font-size:11px;font-weight:700;color:#a13a1a;background:#fdecdf;border-radius:20px;padding:2px 8px}}
.title{{font-size:16px;font-weight:700;color:var(--navy);margin:8px 0 2px}}.buyer{{font-size:13.5px;color:var(--m);margin-bottom:8px}}.ev{{font-size:13.5px;margin-top:5px}}
.src a{{color:var(--accent);font-weight:600;text-decoration:none;font-size:13px}}.foot{{font-size:12.5px;color:var(--m);margin-top:30px;line-height:1.6}}</style></head><body>
<header><h1>FUSED Tender Intelligence</h1><p>PAT, EICR, fixed wire, electrical compliance, EV inspection and emergency lighting opportunities from Find a Tender and Contracts Finder. Updated daily. Last refresh: {updated}.</p></header>
<div class="wrap"><div class="stats">
<div class="stat"><div class="n">{len(rows)}</div><div class="l">Opportunities</div></div>
<div class="stat"><div class="n">{stats['P1']}</div><div class="l">P1</div></div>
<div class="stat"><div class="n">{stats['P2']}</div><div class="l">P2</div></div>
<div class="stat"><div class="n">{stats['tender']}</div><div class="l">Live tenders</div></div>
<div class="stat"><div class="n">{stats['planning']}</div><div class="l">Pre-market</div></div>
<div class="stat"><div class="n">{stats['scanned']}</div><div class="l">Notices scanned</div></div></div>
<div class="filters"><button class="on" data-f="all">All</button><button data-f="P1">P1</button><button data-f="P2">P2</button><button data-f="tender">Live tenders</button><button data-f="planning">Pre-market</button> <a href="opportunities.csv" style="font-size:13px;margin-left:10px">Download CSV</a></div>
<div id="cards">{body}</div>
<div class="foot">Score (0-100) = service match (30) + value (20) + stage, pre-market scores higher (20) + buyer sector fit (15) + deadline urgency (15). Both source APIs filter only by stage and date, so every notice is scanned and matched locally. Always check the source notice before acting.</div></div>
<script>document.querySelectorAll('.filters button').forEach(b=>b.onclick=()=>{{document.querySelectorAll('.filters button').forEach(x=>x.classList.remove('on'));b.classList.add('on');const f=b.dataset.f;document.querySelectorAll('.card').forEach(c=>{{c.style.display=(f==='all'||c.dataset.p===f||c.dataset.s===f)?'':'none'}})}});</script>
</body></html>"""


def run(days, outdir):
    days = max(days, 150)   # open tenders can be months old; closed ones are dropped locally
    now = datetime.now(timezone.utc)
    s = requests.Session()
    best, seen_ids, scanned, ok = {}, set(), 0, 0
    for stage in ("planning", "tender"):
        for portal, base, pf, pt in (("Contracts Finder", CF_BASE, "publishedFrom", "publishedTo"),
                                     ("Find a Tender", FTS_BASE, "updatedFrom", "updatedTo")):
            print(f"{portal} / {stage}", file=sys.stderr)
            n = 0
            start = now - timedelta(days=days)
            while start < now:
                end = min(start + timedelta(days=3), now)
                p = {pf: start.strftime("%Y-%m-%dT%H:%M:%S"), pt: end.strftime("%Y-%m-%dT%H:%M:%S"),
                     "stages": stage, "limit": 100}
                try:
                    for rel in walk(base, p, s):
                        rid = (rel.get("ocid"), rel.get("id"))
                        if rid in seen_ids: continue
                        seen_ids.add(rid); n += 1
                        r = score(rel, portal, now)
                        if not r: continue
                        key = r["ocid"]
                        rank = (r["stage"] == "tender", r["released"])
                        if key not in best or rank > best[key][0]:
                            best[key] = (rank, r)
                    ok += 1
                except Exception as e:
                    print(f"  window {start.date()} FAILED: {e}", file=sys.stderr)
                start = end
            scanned += n
            print(f"  {n} notices", file=sys.stderr)
    rows = [v[1] for v in best.values()]
    if ok == 0 or scanned == 0:
        sys.exit("No data retrieved - leaving previous site untouched.")
    rows.sort(key=lambda r: (r["stale"], -r["fit_score"]))
    stats = dict(P1=sum(r["priority"] == "P1" for r in rows), P2=sum(r["priority"] == "P2" for r in rows),
                 tender=sum(r["stage"] == "tender" for r in rows), planning=sum(r["stage"] == "planning" for r in rows),
                 scanned=scanned)
    os.makedirs(outdir, exist_ok=True)
    updated = now.strftime("%d %b %Y %H:%M UTC")
    open(os.path.join(outdir, "index.html"), "w", encoding="utf-8").write(render(rows, stats, updated))
    json.dump(dict(updated=updated, stats=stats, rows=rows), open(os.path.join(outdir, "data.json"), "w"), indent=1)
    with open(os.path.join(outdir, "opportunities.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["priority"])
        w.writeheader(); w.writerows(rows)
    print(f"Done: {len(rows)} matches from {scanned} notices -> {outdir}", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--out", default="site")
    a = ap.parse_args()
    run(a.days, a.out)
