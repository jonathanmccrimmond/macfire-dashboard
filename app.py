"""
MacFire AI Scout — Live Dashboard
Pulls data from Notion Lead Pipeline and serves a shareable web dashboard.

Run locally:  python3 app.py
Deploy:       See README in this folder — Render.com free tier, ~5 mins.
"""

import os, time, datetime, functools
from pathlib import Path
from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv
import requests

# Load secrets from ~/Documents/MacFire/.env when running locally;
# in production (Render etc.) env vars come from the platform.
load_dotenv(Path.home() / "Documents" / "MacFire" / ".env")

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
        "website":      _prop(page, "Website",              "url"),
        "domain":       _prop(page, "Domain",               "text"),
        "notion_url":   page.get("url", ""),
    }

def _has_company_identity(lead):
    return bool((lead.get("company") or "").strip())

def _has_industry(lead):
    return bool((lead.get("sector") or "").strip())

def _has_geo(lead):
    return bool((lead.get("postcode") or "").strip() or (lead.get("address") or "").strip())

def _has_role_target(lead):
    role_hints = " ".join([
        lead.get("director") or "",
        lead.get("trigger") or "",
        lead.get("next_action") or "",
        lead.get("flags") or "",
    ]).lower()
    return any(x in role_hints for x in ("director", "owner", "founder", "manager", "head", "contact"))

def _website_confidence(lead):
    if (lead.get("website") or "").strip():
        return "known"
    if (lead.get("domain") or "").strip():
        return "inferred"
    return "missing"

def _enrichment_profile(lead):
    score = 0
    if _has_company_identity(lead):
        score += 25
    if _has_industry(lead):
        score += 20
    if _has_geo(lead):
        score += 20
    if _has_role_target(lead):
        score += 15
    if (lead.get("phone") or "").strip() or (lead.get("email") or "").strip():
        score += 20

    if score >= 75 and ((lead.get("phone") or "").strip() or (lead.get("email") or "").strip()):
        queue = "Ready"
        next_action = "Start outreach"
    elif score >= 45:
        queue = "Research"
        next_action = "Find decision maker and verify contact"
    else:
        queue = "Insufficient"
        next_action = "Confirm company identity and location first"

    lead["profile_score"] = score
    lead["enrichment_queue"] = queue
    lead["next_best_action"] = next_action
    lead["website_confidence"] = _website_confidence(lead)
    return lead


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


def invalidate_cache(key=None):
    """Invalidate cache entries so UI updates reflect state changes quickly."""
    if key is None:
        _cache.clear()
        _cache_ts.clear()
        return
    _cache.pop(key, None)
    _cache_ts.pop(key, None)


def notion_update_status(page_id, status_name):
    """Update the Status select property for a Notion page."""
    payload = {"properties": {"Status": {"select": {"name": status_name}}}}
    r = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=NOTION_HEADERS,
        json=payload,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def fetch_stats():
    pages = _query_all(
        sorts=[{"property": "Scout Run Date", "direction": "descending"}]
    )
    leads = [_parse(p) for p in pages]
    leads = [_enrichment_profile(l) for l in leads]

    # Most recent scout run date
    scout_dates = sorted({l["scout_date"] for l in leads if l["scout_date"]}, reverse=True)
    today_date  = scout_dates[0] if scout_dates else ""

    today_leads = [l for l in leads if l["scout_date"] == today_date]
    top10_leads = sorted(
        [l for l in leads if l["status"] != "Not Relevant"],
        key=lambda l: (l["score"] or 0, l["scout_date"] or ""),
        reverse=True
    )[:10]

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
    contracted = sum(
        1 for l in leads
        if (l.get("status") or "").strip().lower() in ("contracted", "customer", "won", "closed won")
    )

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
    contactable_leads = sorted(
        [l for l in leads if (l.get("phone") or l.get("email")) and l["status"] != "Not Relevant"],
        key=lambda l: (l["scout_date"] or "", l["score"] or 0),
        reverse=True
    )
    contactable_count = len(contactable_leads)
    # Queue KPI cards are high-priority only.
    queue_counts = {"Ready": 0, "Research": 0, "Insufficient": 0}
    for l in leads:
        if l.get("priority") != "High":
            continue
        if l.get("status") == "Not Relevant":
            continue
        q = l.get("enrichment_queue")
        if q in queue_counts:
            queue_counts[q] += 1
    enrichment_focus = sorted(
        [
            l for l in leads
            if l.get("priority") in ("High", "Medium")
            and l.get("status") != "Not Relevant"
            and not ((l.get("phone") or "").strip() or (l.get("email") or "").strip())
        ],
        key=lambda l: ((l.get("score") or 0), (l.get("profile_score") or 0), (l.get("scout_date") or "")),
        reverse=True
    )[:20]

    return {
        "total_leads":       len(leads),
        "today_date":        today_date,
        "today_count":       len(today_leads),
        "today_priority":    today_priority,
        "top10_leads":       top10_leads,
        "status_counts":     status_counts,
        "priority_counts":   priority_counts,
        "sector_counts":     sector_counts,
        "contacted":         contacted,
        "responded":         responded,
        "meetings":          meetings,
        "contracted":        contracted,
        "recent":            recent,
        "score_buckets":     score_buckets,
        "run_dates":         run_dates,
        "days_running":      len(run_dates),
        "contactable_count": contactable_count,
        "contactable_leads": contactable_leads,
        "queue_counts":      queue_counts,
        "enrichment_focus":  enrichment_focus,
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
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status in (401, 403):
            return jsonify({
                "ok": False,
                "error": (
                    "Notion authorization failed for the dashboard runtime. "
                    "Update deployment env vars (NOTION_TOKEN / NOTION_DB_ID) and "
                    "confirm the integration has access to the Lead Pipeline database."
                ),
                "status": status,
            }), 502
        msg = ""
        try:
            msg = e.response.text
        except Exception:
            msg = str(e)
        return jsonify({"ok": False, "error": f"Notion request failed: {msg}", "status": status}), 502
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/leads/action", methods=["POST"])
def api_lead_action():
    """Lead row action endpoint: currently supports mark_not_relevant."""
    try:
        payload = request.get_json(silent=True) or {}
        lead_id = (payload.get("lead_id") or "").strip()
        action = (payload.get("action") or "").strip()

        if not lead_id:
            return jsonify({"ok": False, "error": "Missing lead_id"}), 400
        if not action:
            return jsonify({"ok": False, "error": "Missing action"}), 400

        if action == "mark_not_relevant":
            notion_update_status(lead_id, "Not Relevant")
            invalidate_cache("stats")
            return jsonify({"ok": True, "updated_status": "Not Relevant"})

        return jsonify({"ok": False, "error": f"Unsupported action: {action}"}), 400
    except requests.HTTPError as e:
        msg = ""
        try:
            msg = e.response.text
        except Exception:
            msg = str(e)
        return jsonify({"ok": False, "error": f"Notion update failed: {msg}"}), 502
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
