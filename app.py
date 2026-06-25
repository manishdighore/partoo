"""
Partoo Analytics Dashboard -- Flask Backend
Run: python app.py  then open http://localhost:5000
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import requests
import concurrent.futures
import os
import json
import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("partoo")
logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__, static_folder=".")
CORS(app)


def load_local_env(path=".env"):
    """Load simple KEY=value pairs for local development without an extra dependency."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def load_partoo_keys_from_env():
    raw_json = os.environ.get("PARTOO_API_KEYS", "").strip()
    loaded = []
    if raw_json:
        try:
            keys = json.loads(raw_json)
            if isinstance(keys, dict):
                for market, key in keys.items():
                    key = (key or "").strip()
                    if key:
                        api_keys[str(market).upper()] = key
                        loaded.append(str(market).upper())
        except Exception as exc:
            log.warning("Could not parse PARTOO_API_KEYS JSON: %s", exc)

    for name, value in os.environ.items():
        if not name.startswith("PARTOO_API_KEY_"):
            continue
        market = name.replace("PARTOO_API_KEY_", "", 1).upper()
        key = (value or "").strip()
        if market and key:
            api_keys[market] = key
            loaded.append(market)

    if loaded:
        log.info("Loaded Partoo API keys from environment for markets: %s", sorted(set(loaded)))


# market_code -> api_key   e.g. {"DK": "abc123", "FR": "xyz456"}
api_keys = {}
load_local_env()
load_partoo_keys_from_env()
openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/set-keys", methods=["POST"])
def set_keys():
    """Save API keys for one or more markets.
    Body: {"keys": {"DK": "key1", "FR": "key2", ...}}
    """
    data = request.get_json(force=True)
    keys = data.get("keys") or {}
    if not isinstance(keys, dict) or not keys:
        return jsonify({"success": False, "message": "Provide a non-empty keys object"}), 400
    saved = []
    for market, key in keys.items():
        key = (key or "").strip()
        if key:
            api_keys[market.upper()] = key
            saved.append(market.upper())
    log.info("Saved keys for markets: %s", saved)
    return jsonify({"success": True, "saved": saved, "configured": list(api_keys.keys())})


@app.route("/api/set-openai-key", methods=["POST"])
def set_openai_key():
    """Save an OpenAI key for data chat. Body: {"openai_key": "..."}"""
    global openai_api_key
    data = request.get_json(force=True)
    key = (data.get("openai_key") or "").strip()
    if not key:
        return jsonify({"success": False, "message": "Provide a non-empty openai_key"}), 400
    openai_api_key = key
    log.info("Saved OpenAI key for data chat")
    return jsonify({"success": True, "configured": bool(openai_api_key)})


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "configured_markets": list(api_keys.keys()),
        "openai_configured": bool(openai_api_key),
    })


API_BASE = "https://api.partoo.co/v2"
DEFAULT_WINDOW_DAYS = 30
PRESENCE_DATA_LAG_DAYS = 5
MAX_REVIEW_PAGES = 10
THEME_STOPWORDS = {
    "about", "after", "again", "also", "avec", "been", "bien", "but", "can", "chez",
    "con", "dans", "del", "des", "did", "die", "does", "for", "from", "get", "got",
    "had", "has", "have", "het", "his", "how", "ich", "ils", "into", "ist", "les",
    "mais", "mon", "muy", "nicht", "not", "nous", "our", "out", "pas", "plus",
    "pour", "que", "qui", "she", "son", "sur", "the", "their", "them", "then",
    "there", "they", "this", "too", "tout", "très", "und", "une", "very", "was",
    "werden", "were", "with", "you", "your", "zijn", "and", "are", "auf", "der",
    "ein", "est", "las", "los", "of", "to", "in", "is", "it", "a", "an", "i",
    "le", "la", "el", "en", "et", "de", "du", "un", "se", "me", "my", "we",
}

REVIEW_METRICS = ["average_rating", "reviews_count", "reply_time", "rating_distribution"]

PRESENCE_METRICS = [
    "business_impressions_desktop_maps",
    "business_impressions_mobile_maps",
    "business_impressions_desktop_search",
    "business_impressions_mobile_search",
    "business_direction_requests",
    "call_clicks",
    "website_clicks",
]


def extract_metric_value(payload, metric_name):
    """
    Extract numeric or dict value from Partoo API response.

    Known shapes:
      Review analytics:
        {"page":1,"count":1,"max_page":1,
         "data":[{"dimension":null,"metrics":{"average_rating":4.503}}]}
        reply_time value is a dict: {"fast":N,"slow":N,"not_replied":N,"total":N}

      Presence analytics:
        {"page":1,"count":1,"max_page":1,
         "metrics":[{"business_impressions_desktop_maps":213235}]}
    """
    if not isinstance(payload, dict):
        log.debug("     extract: payload is not a dict, got %s", type(payload))
        return None

    log.debug("     extract: top-level keys = %s", list(payload.keys()))

    # Path A: data[0].metrics[metric_name]  -- review analytics format
    data_arr = payload.get("data")
    if isinstance(data_arr, list) and len(data_arr) > 0:
        row = data_arr[0]
        if isinstance(row, dict):
            metrics_obj = row.get("metrics")
            if isinstance(metrics_obj, dict):
                v = metrics_obj.get(metric_name)
                log.debug("     extract: Path A -- data[0].metrics[%s] = %s", metric_name, v)
                if isinstance(v, (int, float, dict)):
                    log.info("     extract: SUCCESS Path A -- value=%s", v)
                    return v
                elif v is not None:
                    log.warning("     extract: Path A key found but unexpected type %s: %r",
                                type(v).__name__, v)

    # Path B: metrics[{metric_name: value}]  -- presence analytics format
    # Also handles: metrics[{name: metric_name, value: N}]
    m = payload.get("metrics")
    if isinstance(m, list):
        for item in m:
            if not isinstance(item, dict):
                continue
            # Shape B1: flat {metric_name: value} inside array
            direct_val = item.get(metric_name)
            if isinstance(direct_val, (int, float, dict)):
                log.info("     extract: SUCCESS Path B1 (metrics array direct key) -- value=%s", direct_val)
                return direct_val
            # Shape B2: {name: metric_name, value: N}
            if item.get("name") == metric_name or item.get("metric") == metric_name:
                for k in ("value", "total", "count"):
                    if isinstance(item.get(k), (int, float)):
                        log.info("     extract: SUCCESS Path B2 (metrics array name/value) -- value=%s", item[k])
                        return item[k]

    # Path C: top-level metrics object (non-array)
    if isinstance(m, dict):
        v = m.get(metric_name)
        if isinstance(v, (int, float, dict)):
            log.info("     extract: SUCCESS Path C (top-level metrics obj) -- value=%s", v)
            return v

    # Path D: flat top-level key
    direct = payload.get(metric_name)
    if isinstance(direct, (int, float, dict)):
        log.info("     extract: SUCCESS Path D (flat key) -- value=%s", direct)
        return direct

    log.warning("     extract: FAILED for %r -- payload: %s", metric_name, json.dumps(payload)[:400])
    return None


def summarize_chat_metrics(metrics):
    lines = []
    for name in REVIEW_METRICS + PRESENCE_METRICS:
        row = metrics.get(name) if isinstance(metrics, dict) else None
        if not isinstance(row, dict):
            continue
        value = row.get("value")
        if isinstance(value, dict):
            value = json.dumps(value, sort_keys=True)
        lines.append("{}: {}".format(name, value))
    return "\n".join(lines) if lines else "No metrics have been loaded yet."


def add_date_filters(params, start, end, metric_type):
    params = dict(params or {})
    if metric_type == "presence_analytics":
        params["filter_date__gte"] = "{}T00:00:00".format(start.isoformat())
        params["filter_date__lte"] = "{}T00:00:00".format(end.isoformat())
    else:
        params["update_date__gte"] = "{}T00:00:00".format(start.isoformat())
        params["update_date__lte"] = "{}T23:59:59".format(end.isoformat())
    return params


def parse_period_args(args):
    window = (args.get("window") or "30d").lower()
    today = date.today()
    if window == "7d":
        days = 7
        end = today
        start = end - timedelta(days=days - 1)
        label = "Last 7 days"
    elif window == "custom":
        start_raw = args.get("start")
        end_raw = args.get("end")
        try:
            start = datetime.strptime(start_raw, "%Y-%m-%d").date()
            end = datetime.strptime(end_raw, "%Y-%m-%d").date()
        except Exception:
            raise ValueError("Custom window needs start and end as YYYY-MM-DD")
        if start > end:
            raise ValueError("Custom start date must be before end date")
        days = (end - start).days + 1
        label = "{} to {}".format(start.isoformat(), end.isoformat())
    else:
        window = "30d"
        days = DEFAULT_WINDOW_DAYS
        end = today
        start = end - timedelta(days=days - 1)
        label = "Last 30 days"

    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    reliable_presence_end = min(end, today - timedelta(days=PRESENCE_DATA_LAG_DAYS))
    reliable_presence_start = reliable_presence_end - timedelta(days=days - 1)

    return {
        "window": window,
        "label": label,
        "days": days,
        "start": start,
        "end": end,
        "previous_start": prev_start,
        "previous_end": prev_end,
        "presence_start": reliable_presence_start,
        "presence_end": reliable_presence_end,
        "presence_lag_days": PRESENCE_DATA_LAG_DAYS,
    }


def serialize_period(period):
    return {
        "window": period["window"],
        "label": period["label"],
        "days": period["days"],
        "start": period["start"].isoformat(),
        "end": period["end"].isoformat(),
        "previous_start": period["previous_start"].isoformat(),
        "previous_end": period["previous_end"].isoformat(),
        "presence_start": period["presence_start"].isoformat(),
        "presence_end": period["presence_end"].isoformat(),
        "presence_lag_days": period["presence_lag_days"],
    }


def fetch_single_metric(metric_type, metric_name, headers, params=None):
    url = "{}/{}/metrics".format(API_BASE, metric_type)
    req_params = dict(params or {})
    req_params["metrics"] = metric_name
    log.info("-->  GET  %s  params=%s", url, req_params)
    try:
        resp = requests.get(url, headers=headers, params=req_params, timeout=15)

        try:
            body = resp.json()
            body_str = json.dumps(body, indent=2)
        except Exception:
            body = None
            body_str = resp.text

        log.info("<--  [%s]  HTTP %d", metric_name, resp.status_code)
        log.debug("     RAW RESPONSE:\n%s", body_str)

        resp.raise_for_status()

        value = extract_metric_value(body, metric_name)
        log.info("     FINAL extracted value = %s", value)

        return metric_name, {"value": value, "raw": body}, None

    except requests.exceptions.HTTPError as exc:
        try:
            error_body = exc.response.text
        except Exception:
            error_body = str(exc)
        err = "HTTP {}: {}".format(exc.response.status_code, error_body)
        log.error("     ERROR %s -- %s", metric_name, err)
        return metric_name, None, err

    except Exception as exc:
        log.error("     ERROR %s -- %s", metric_name, str(exc))
        return metric_name, None, str(exc)


def fetch_metrics_batch(metric_type, metric_names, headers, start=None, end=None, dimensions=None):
    params = {"metrics": ",".join(metric_names)}
    if dimensions:
        params["dimensions"] = dimensions
    if start and end:
        params = add_date_filters(params, start, end, metric_type)

    url = "{}/{}/metrics".format(API_BASE, metric_type)
    log.info("-->  GET batch %s  params=%s", url, params)
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=20)
        try:
            body = resp.json()
            body_str = json.dumps(body, indent=2)
        except Exception:
            body = None
            body_str = resp.text
        log.info("<-- batch [%s] HTTP %d", metric_type, resp.status_code)
        log.debug("     RAW RESPONSE:\n%s", body_str)
        resp.raise_for_status()

        if dimensions:
            return {"raw": body, "rows": normalize_dimension_rows(body, metric_names)}, None

        metrics = {}
        for name in metric_names:
            metrics[name] = {"value": extract_metric_value(body, name), "raw": body}
        return metrics, None
    except requests.exceptions.HTTPError as exc:
        error_body = getattr(exc.response, "text", str(exc))
        return None, "HTTP {}: {}".format(exc.response.status_code, error_body)
    except Exception as exc:
        return None, str(exc)


def normalize_dimension_rows(payload, metric_names):
    rows = []
    raw_rows = payload.get("data") if isinstance(payload, dict) else None
    if raw_rows is None and isinstance(payload, dict):
        raw_rows = payload.get("metrics")
    if not isinstance(raw_rows, list):
        return rows
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        metrics_obj = row.get("metrics") if isinstance(row.get("metrics"), dict) else row
        values = {}
        for name in metric_names:
            v = metrics_obj.get(name) if isinstance(metrics_obj, dict) else None
            if isinstance(v, (int, float, dict)):
                values[name] = v
        rows.append({
            "dimension": row.get("dimension") or row.get("date"),
            "dimension_name": row.get("dimension_name") or row.get("filter_date") or row.get("date"),
            "metrics": values,
        })
    return rows


def fetch_period_metrics(headers, period):
    results = {}
    errors = {}

    review_data, review_error = fetch_metrics_batch(
        "review_analytics",
        REVIEW_METRICS,
        headers,
        period["start"],
        period["end"],
    )
    if review_error:
        for name in REVIEW_METRICS:
            errors[name] = review_error
    else:
        results.update(review_data)

    presence_data, presence_error = fetch_metrics_batch(
        "presence_analytics",
        PRESENCE_METRICS,
        headers,
        period["presence_start"],
        period["presence_end"],
    )
    if presence_error:
        for name in PRESENCE_METRICS:
            errors[name] = presence_error
    else:
        results.update(presence_data)

    return results, errors


def fetch_snapshot_metrics(headers):
    results = {}
    errors = {}

    review_data, review_error = fetch_metrics_batch("review_analytics", REVIEW_METRICS, headers)
    if review_error:
        for name in REVIEW_METRICS:
            errors[name] = review_error
    else:
        results.update(review_data)

    presence_data, presence_error = fetch_metrics_batch("presence_analytics", PRESENCE_METRICS, headers)
    if presence_error:
        for name in PRESENCE_METRICS:
            errors[name] = presence_error
    else:
        results.update(presence_data)

    return results, errors


def compute_response_rate(reply_time):
    if not isinstance(reply_time, dict):
        return None
    total = reply_time.get("total") or 0
    if total <= 0:
        return None
    return round(((reply_time.get("fast") or 0) + (reply_time.get("slow") or 0)) / total * 100, 2)


def compute_ctr(metrics):
    def value(name):
        row = metrics.get(name) if isinstance(metrics, dict) else None
        return row.get("value") if isinstance(row, dict) and isinstance(row.get("value"), (int, float)) else 0

    maps = value("business_impressions_desktop_maps") + value("business_impressions_mobile_maps")
    search = value("business_impressions_desktop_search") + value("business_impressions_mobile_search")
    total_impressions = maps + search
    website = value("website_clicks")
    calls = value("call_clicks")
    directions = value("business_direction_requests")
    return {
        "website_from_search": round(website / search * 100, 2) if search else None,
        "calls_from_search": round(calls / search * 100, 2) if search else None,
        "directions_from_maps": round(directions / maps * 100, 2) if maps else None,
        "all_actions_from_all_impressions": round((website + calls + directions) / total_impressions * 100, 2) if total_impressions else None,
        "search_impressions": search,
        "map_impressions": maps,
        "total_impressions": total_impressions,
    }


def fetch_review_rows(headers, params, max_pages=MAX_REVIEW_PAGES):
    reviews = []
    errors = []
    count = None
    for page in range(1, max_pages + 1):
        req_params = dict(params)
        req_params.update({"page": page, "per_page": 100})
        try:
            resp = requests.get("{}/reviews".format(API_BASE), headers=headers, params=req_params, timeout=20)
            body = resp.json()
            resp.raise_for_status()
            if count is None:
                count = body.get("count")
            rows = body.get("reviews") or []
            if isinstance(rows, list):
                reviews.extend(rows)
            max_page = body.get("max_page") or page
            if page >= max_page:
                break
        except Exception as exc:
            errors.append(str(exc))
            break
    return reviews, count, errors


def parse_review_date(row):
    raw = row.get("date") or row.get("update_date")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except Exception:
        return None


def build_review_insights(headers, period):
    today = date.today()
    unanswered, unanswered_count, unanswered_errors = fetch_review_rows(headers, {
        "state__in": "not_treated",
        "order_by": "-update_date",
    })

    aging = {"lt_7": 0, "days_7_30": 0, "days_30_plus": 0, "unknown": 0}
    oldest_days = None
    for row in unanswered:
        review_date = parse_review_date(row)
        if not review_date:
            aging["unknown"] += 1
            continue
        age = (today - review_date).days
        oldest_days = age if oldest_days is None else max(oldest_days, age)
        if age < 7:
            aging["lt_7"] += 1
        elif age <= 30:
            aging["days_7_30"] += 1
        else:
            aging["days_30_plus"] += 1

    recent_reviews, recent_count, theme_errors = fetch_review_rows(headers, {
        "content__isnull": "false",
        "update_date__gte": "{}T00:00:00".format(period["start"].isoformat()),
        "update_date__lte": "{}T23:59:59".format(period["end"].isoformat()),
        "order_by": "-update_date",
    }, max_pages=5)

    themes = extract_review_themes(recent_reviews)
    return {
        "unanswered_aging": {
            "buckets": aging,
            "total_loaded": len(unanswered),
            "api_count": unanswered_count,
            "oldest_days": oldest_days,
            "truncated": unanswered_count is not None and unanswered_count > len(unanswered),
            "errors": unanswered_errors,
        },
        "themes": {
            "items": themes,
            "reviews_loaded": len(recent_reviews),
            "api_count": recent_count,
            "truncated": recent_count is not None and recent_count > len(recent_reviews),
            "errors": theme_errors,
        },
    }


def extract_review_themes(reviews):
    words = Counter()
    phrases = Counter()
    tag_counts = Counter()
    for row in reviews:
        for tag in row.get("tags") or []:
            label = tag.get("label") if isinstance(tag, dict) else None
            if label:
                tag_counts[label.lower()] += 1
        content = (row.get("content") or "").lower()
        tokens = [t for t in re.findall(r"[a-zA-ZÀ-ÿ]{3,}", content) if t not in THEME_STOPWORDS]
        words.update(tokens)
        phrases.update(" ".join(pair) for pair in zip(tokens, tokens[1:]))

    items = []
    for label, count in tag_counts.most_common(8):
        items.append({"label": label, "count": count, "source": "tag"})
    for label, count in phrases.most_common(10):
        if count > 1 and label not in {i["label"] for i in items}:
            items.append({"label": label, "count": count, "source": "phrase"})
    for label, count in words.most_common(12):
        if label not in {i["label"] for i in items}:
            items.append({"label": label, "count": count, "source": "keyword"})
    return items[:12]


def build_trends(headers, period):
    start = period["end"] - timedelta(days=185)
    review_trend, review_error = fetch_metrics_batch(
        "review_analytics",
        ["average_rating", "reviews_count"],
        headers,
        start,
        period["end"],
        dimensions="month",
    )
    return {
        "reviews": review_trend if review_trend else None,
        "errors": {"reviews": review_error} if review_error else {},
    }


@app.route("/api/debug/<metric_type>/<metric_name>", methods=["GET"])
def debug_metric(metric_type, metric_name):
    """Hit /api/debug/presence_analytics/business_impressions_desktop_maps?market=DK"""
    market = request.args.get("market", "").upper()
    api_key = api_keys.get(market)
    if not api_key:
        return jsonify({"error": "No API key for market: {}".format(market or "(none)")}), 401

    headers = {"x-APIKey": api_key}
    url = "{}/{}/metrics".format(API_BASE, metric_type)

    try:
        resp = requests.get(url, headers=headers, params={"metrics": metric_name}, timeout=15)
        body = resp.json()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    extracted = extract_metric_value(body, metric_name)

    return jsonify({
        "market": market,
        "url": url,
        "http_status": resp.status_code,
        "raw_response": body,
        "top_level_keys": list(body.keys()) if isinstance(body, dict) else None,
        "extracted_value": extracted,
    })


@app.route("/api/metrics", methods=["GET"])
def get_metrics():
    market = request.args.get("market", "").upper()
    api_key = api_keys.get(market)
    if not api_key:
        return jsonify({"error": "No API key for market: {}. Save keys first.".format(market or "(none)")}), 401

    try:
        period = parse_period_args(request.args)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    headers = {"x-APIKey": api_key}
    log.info("=" * 60)
    log.info("Market: %s -- Fetching dashboard metrics for %s", market, period["label"])
    log.info("=" * 60)

    snapshot_results, snapshot_errors = fetch_snapshot_metrics(headers)
    current_results, current_errors = fetch_period_metrics(headers, period)

    previous_period = dict(period)
    previous_period["start"] = period["previous_start"]
    previous_period["end"] = period["previous_end"]
    previous_period["presence_end"] = period["presence_start"] - timedelta(days=1)
    previous_period["presence_start"] = previous_period["presence_end"] - timedelta(days=period["days"] - 1)
    previous_results, previous_errors = fetch_period_metrics(headers, previous_period)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        insights_future = executor.submit(build_review_insights, headers, period)
        trends_future = executor.submit(build_trends, headers, period)
        review_insights = insights_future.result()
        trends = trends_future.result()

    derived = {
        "response_rate": {
            "value": compute_response_rate((snapshot_results.get("reply_time") or {}).get("value")),
            "period_value": compute_response_rate((current_results.get("reply_time") or {}).get("value")),
            "previous_value": compute_response_rate((previous_results.get("reply_time") or {}).get("value")),
        },
        "presence_ctr": compute_ctr(snapshot_results),
        "period_presence_ctr": compute_ctr(current_results),
        "previous_presence_ctr": compute_ctr(previous_results),
    }

    errors = dict(snapshot_errors)
    for name, msg in current_errors.items():
        errors["period_{}".format(name)] = msg
    for name, msg in previous_errors.items():
        errors["previous_{}".format(name)] = msg
    errors.update({"trend_{}".format(k): v for k, v in trends.get("errors", {}).items()})

    log.info("=" * 60)
    log.info("SUMMARY [%s]:", market)
    for name in sorted(snapshot_results):
        log.info("  %-52s  value = %s", name, snapshot_results[name].get("value"))
    if errors:
        log.error("ERRORS:")
        for name, msg in errors.items():
            log.error("  %s -- %s", name, msg)
    log.info("=" * 60)

    return jsonify({
        "market": market,
        "period": serialize_period(period),
        "metrics": snapshot_results,
        "period_metrics": current_results,
        "previous_metrics": previous_results,
        "derived": derived,
        "review_insights": review_insights,
        "trends": trends,
        "errors": errors,
    })


@app.route("/api/markets/summary", methods=["GET"])
def get_markets_summary():
    try:
        period = parse_period_args(request.args)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    configured = sorted(api_keys.items())
    if not configured:
        return jsonify({"markets": [], "period": serialize_period(period), "errors": {}})

    def fetch_market_summary(item):
        market, api_key = item
        headers = {"x-APIKey": api_key}
        metrics, errors = fetch_period_metrics(headers, period)
        reply_time = (metrics.get("reply_time") or {}).get("value")
        ctr = compute_ctr(metrics)
        return market, {
            "market": market,
            "average_rating": (metrics.get("average_rating") or {}).get("value"),
            "reviews_count": (metrics.get("reviews_count") or {}).get("value"),
            "response_rate": compute_response_rate(reply_time),
            "website_ctr": ctr.get("website_from_search"),
            "actions_ctr": ctr.get("all_actions_from_all_impressions"),
            "search_impressions": ctr.get("search_impressions"),
            "total_impressions": ctr.get("total_impressions"),
            "errors": errors,
        }

    summaries = []
    errors = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(configured), 6)) as executor:
        futures = [executor.submit(fetch_market_summary, item) for item in configured]
        for future in concurrent.futures.as_completed(futures):
            market, summary = future.result()
            summaries.append(summary)
            if summary["errors"]:
                errors[market] = summary["errors"]

    summaries.sort(key=lambda row: (row.get("response_rate") is None, -(row.get("response_rate") or 0)))
    return jsonify({
        "period": serialize_period(period),
        "markets": summaries,
        "errors": errors,
    })


@app.route("/api/chat", methods=["POST"])
def chat_with_data():
    if not openai_api_key:
        return jsonify({
            "error": "No OpenAI API key configured. Set OPENAI_API_KEY or save one in API Keys."
        }), 400

    data = request.get_json(force=True)
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Provide a non-empty message"}), 400

    market = (data.get("market") or "").upper()
    metrics = data.get("metrics") or {}
    errors = data.get("errors") or {}
    history = data.get("history") or []

    system = (
        "You are a concise Partoo analytics assistant. Answer only from the dashboard "
        "metrics supplied in the user message. If the data is missing, say what should "
        "be loaded first. Use plain business language and cite metric names when useful."
    )
    context = {
        "market": market or "not selected",
        "metrics_summary": summarize_chat_metrics(metrics),
        "api_errors": errors,
    }

    messages = [{"role": "system", "content": system}]
    for h in history[-8:]:
        role = h.get("role", "user")
        if role in ("user", "assistant"):
            messages.append({"role": role, "content": h.get("content", "")})
    messages.append({
        "role": "user",
        "content": "Dashboard context:\n{}\n\nQuestion: {}".format(json.dumps(context, indent=2), message),
    })

    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": "Bearer {}".format(openai_api_key),
                "Content-Type": "application/json",
            },
            json={
                "model": os.environ.get("OPENAI_MODEL", "gpt-4o"),
                "messages": messages,
                "temperature": 0.2,
                "max_tokens": 600,
            },
            timeout=30,
        )
        if not resp.ok:
            return jsonify({"error": "OpenAI error {}: {}".format(resp.status_code, resp.text[:300])}), 502
        answer = resp.json()["choices"][0]["message"]["content"]
        return jsonify({"answer": answer})
    except Exception as exc:
        log.exception("Chat failed")
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("\n Partoo Dashboard  -->  http://localhost:{}\n".format(port))
    app.run(debug=True, port=port, host="0.0.0.0")
