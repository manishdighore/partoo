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

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("partoo")
logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__, static_folder=".")
CORS(app)

# market_code -> api_key   e.g. {"DK": "abc123", "FR": "xyz456"}
api_keys = {}


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


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "configured_markets": list(api_keys.keys())})


REVIEW_METRICS = ["average_rating", "reviews_count", "reply_time"]

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


def fetch_single_metric(metric_type, metric_name, headers):
    url = "https://api.partoo.co/v2/{}/metrics?metrics={}".format(metric_type, metric_name)
    log.info("-->  GET  %s", url)
    try:
        resp = requests.get(url, headers=headers, timeout=15)

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


@app.route("/api/debug/<metric_type>/<metric_name>", methods=["GET"])
def debug_metric(metric_type, metric_name):
    """Hit /api/debug/presence_analytics/business_impressions_desktop_maps?market=DK"""
    market = request.args.get("market", "").upper()
    api_key = api_keys.get(market)
    if not api_key:
        return jsonify({"error": "No API key for market: {}".format(market or "(none)")}), 401

    headers = {"x-APIKey": api_key}
    url = "https://api.partoo.co/v2/{}/metrics?metrics={}".format(metric_type, metric_name)

    try:
        resp = requests.get(url, headers=headers, timeout=15)
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

    headers = {"x-APIKey": api_key}
    tasks = (
        [("review_analytics", m) for m in REVIEW_METRICS]
        + [("presence_analytics", m) for m in PRESENCE_METRICS]
    )

    log.info("=" * 60)
    log.info("Market: %s  --  Fetching %d metrics in parallel ...", market, len(tasks))
    log.info("=" * 60)

    results = {}
    errors = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {
            executor.submit(fetch_single_metric, mt, mn, headers): (mt, mn)
            for mt, mn in tasks
        }
        for future in concurrent.futures.as_completed(futures):
            metric_name, data, error = future.result()
            if error:
                errors[metric_name] = error
            else:
                results[metric_name] = data

    log.info("=" * 60)
    log.info("SUMMARY [%s]:", market)
    for name in sorted(results):
        log.info("  %-52s  value = %s", name, results[name].get("value"))
    if errors:
        log.error("ERRORS:")
        for name, msg in errors.items():
            log.error("  %s -- %s", name, msg)
    log.info("=" * 60)

    return jsonify({"market": market, "metrics": results, "errors": errors})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("\n Partoo Dashboard  -->  http://localhost:{}\n".format(port))
    app.run(debug=True, port=port, host="0.0.0.0")
