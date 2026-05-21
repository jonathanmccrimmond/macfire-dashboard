"""
MacFire AI Scout — Live Dashboard
Pulls data from Notion Lead Pipeline and serves a shareable web dashboard.

Run locally:  python3 app.py
Deploy:       See README in this folder — Render.com free tier, ~5 mins.
"""

import os, time, datetime, functools
from flask import Flask, jsonify, render_template
import requests

# ── Config ────────────────────────────────────────────────────────────────────
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DB_ID = os.environ.get("NOTION_DB_ID", "")
PORT         = int(os.environ.get("PORT", 5000))

NOTION_HEADERS = {
    "Authorization":  f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type":   "application/json",
}

app = Flask(__name__)

# ── Notion helpers ────────────────────────────────────────────────────────────
def _query_all(filter_body=None, sorts=None):
    """Paginate through all Notion database results."""
    results = []
    cursor  = None
    while True:
        payload = {"page_size": 100}
        if filter_body: payload["filter"] = filter_body
        if sorts:       payload["sorts"]   = sorts
        if cursor:      payload["start_cursor"] = cursor
        r = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query",
            headers=NOTION_HEADERS, json=payload, timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


def _prop(page, name, kind):
    """Safely extract a Notion property value."""
    props = page.get("properties", {})
    p = props.get(name, {})
    try:
        if kind == "title":
            return p["title"][0]["plain_text"] if p.get("title") else ""
        if kind == "select":
            return (p.get("select") or {}).get("name", "")
        if kind == "number":
            return p.get("number")
        if kind == "date":
            return (p.get("date") or {}).get("start", "")
        if kind == "text":
            rt = p.get("rich_text", [])
            return rt[0]["plain_text"] if rt else ""
        if kind == "url":
            return p.get("url", "")
        if kind == "email":
            return p.get("email", "")
        if kind == "phone":
            return p.get("phone_number", "")
    except Exception:
        return ""
    return ""


def _parse(page):
    return {
        "id":           page["id"],
        "company":      _prop(page, "Company",              "title"),
        "priority":     _prop(page, "Priority",             "select"),
        "status":       _prop(page, "Status",               "select"),
        "score":        _prop(page, "Score",                "number"),
        "sector":       _prop(page, "Sector",               "select"),
        "postcode":     _prop(page, "Postcode",             "text"),
        "address":      _prop(page, "Address",              "text"),
        "director":     _prop(page, "Director Name",        "text"),
        "incorporated": _prop(page, "Incorporated",         "date"),
        "scout_date":   _prop(page, "Scout Run Date",       "date"),
        "maps_url":     _prop(page, "Maps Link",            "url"),
        "street_view":  _prop(page, "Street View",          "url"),
        "places":       _prop(page, "Google Places",        "text"),
        "flags":        _prop(page, "Quality Flags",        "text"),
        "trigger":      _prop(page, "Trigger",              "text"),
        "phone":        _prop(page, "Phone Number",         "phone"),
        "email":        _prop(page, "Email Address",        "email"),
        "date_sent":    _prop(page, "Date Sent",            "date"),
        "date_responded": _prop(page, "Date Responded",     "date"),
        "next_action":  _prop(page, "Next Action",          "text"),
        "email_draft":  _prop(page, "Email Draft",          "text"),
        "notion_url":   page.get("url", ""),
    }


# ── Cached data layer (60-second TTL) ─────────────────────────────────────────
_cache     = {}
_cache_ts  = {}
CACHE_TTL  = 60  # seconds


def cached(key, fn):
    now = time.time()
    if key not in _cache or now - _cache_ts.get(key, 0) > CACHE_TTL:
        _cache[key]    = fn()
        _cache_ts[key] = now
    return _cache[key]


def fetch_stats():
    pages = _query_all(
        sorts=[{"property": "Scout Run Date", "direction": "descending"}]
    )
    leads = [_parse(p) for p in pages]

    # Most recent scout run date
    scout_dates = sorted({l["scout_date"] for l in leads if l["scout_date"]}, reverse=True)
    today_date  = scout_dates[0] if scout_dates else ""

    today_leads = [l for l in leads if l["scout_date"] == today_date]
    top5_today  = sorted(today_leads, key=lambda l: l["score"] or 0, reverse=True)[:5]

    # Pipeline status counts (all time)
    status_order = ["New Lead", "Contacted", "Responded", "Meeting Booked", "No Response", "Not Relevant"]
    status_counts = {s: 0 for s in status_order}
    for l in leads:
        s = l["status"]
        if s in status_counts:
            status_counts[s] += 1

    # Priority breakdown (all time)
    priority_counts = {"High": 0, "Medium": 0, "Low": 0}
    for l in leads:
        p = l["priority"]
        if p in priority_counts:
            priority_counts[p] += 1

    # Priority breakdown today
    today_priority = {"High": 0, "Medium": 0, "Low": 0}
    for l in today_leads:
        p = l["priority"]
        if p in today_priority:
            today_priority[p] += 1

    # Sector breakdown (all time)
    sector_counts = {}
    for l in leads:
        s = l["sector"] or "Other"
        sector_counts[s] = sector_counts.get(s, 0) + 1
    sector_counts = dict(sorted(sector_counts.items(), key=lambda x: x[1], reverse=True))

    # Contacted + responded rate
    contacted = sum(1 for l in leads if l["status"] in ("Contacted", "Responded", "Meeting Booked"))
    responded = sum(1 for l in leads if l["status"] in ("Responded", "Meeting Booked"))
    meetings  = sum(1 for l in leads if l["status"] == "Meeting Booked")

    # Recent activity (last 10 leads by scout run date)
    recent = sorted(
        [l for l in leads if l["scout_date"]],
        key=lambda l: (l["scout_date"], l["score"] or 0),
        reverse=True
    )[:15]

    # Score distribution buckets
    score_buckets = {"90-100": 0, "75-89": 0, "60-74": 0, "50-59": 0, "<50": 0}
    for l in leads:
        s = l["score"] or 0
        if s >= 90:   score_buckets["90-100"] += 1
        elif s >= 75: score_buckets["75-89"]  += 1
        elif s >= 60: score_buckets["60-74"]  += 1
        elif s >= 50: score_buckets["50-59"]  += 1
        else:         score_buckets["<50"]     += 1

    # Unique run dates
    run_dates = scout_dates

    # Hot leads — score ≥ 70, New Lead, High/Medium priority, contactable
    hot_leads = [
        l for l in leads
        if (l.get("score") or 0) >= 70
        and l.get("status") == "New Lead"
        and l.get("priority") in ("High", "Medium")
        and (l.get("phone") or l.get("email"))
    ]
    hot_leads = sorted(hot_leads, key=lambda l: l.get("score") or 0, reverse=True)[:10]
    contactable_count = sum(1 for l in leads if l.get("phone") or l.get("email"))

    return {
        "total_leads":       len(leads),
        "today_date":        today_date,
        "today_count":       len(today_leads),
        "today_priority":    today_priority,
        "top5_today":        top5_today,
        "status_counts":     status_counts,
        "priority_counts":   priority_counts,
        "sector_counts":     sector_counts,
        "contacted":         contacted,
        "responded":         responded,
        "meetings":          meetings,
        "recent":            recent,
        "score_buckets":     score_buckets,
        "run_dates":         run_dates,
        "days_running":      len(run_dates),
        "hot_leads":         hot_leads,
        "contactable_count": contactable_count,
        "last_refreshed":    datetime.datetime.now().strftime("%H:%M:%S"),
    }


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/stats")
def api_stats():
    try:
        data = cached("stats", fetch_stats)
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
