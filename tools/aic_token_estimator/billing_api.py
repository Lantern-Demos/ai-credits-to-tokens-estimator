"""
billing_api.py — Fetch a GitHub Enterprise AI usage report via the Enhanced
Billing *reports* API and normalize it into the CSV schema that aic_tokens.py
`estimate` consumes.

Flow (matches the documented two-call pattern):

  1. POST /enterprises/{enterprise}/settings/billing/reports
       -> creates an asynchronous billing report, returns a report id.
  2. GET  /enterprises/{enterprise}/settings/billing/reports/{report_id}
       -> poll until status == "completed", then read the result.

Important: this API returns the SAME enhanced-billing usage line items as the
manual CSV download (credits / dollars by date, product, sku, model, user, cost
center). It does NOT return token counts — tokens are never exposed by GitHub
and must still be estimated by the estimator.

Standard library only (urllib) so the toolkit stays dependency-free. The token
is read from --token or the GITHUB_TOKEN environment variable and is never
printed or logged.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_ROOT = "https://api.github.com"
API_VERSION = "2026-03-10"
REQUEST_TIMEOUT_S = 30

# Columns the estimator understands; we normalize the API payload to these.
OUTPUT_COLUMNS = [
    "date", "username", "product", "sku", "model", "quantity", "unit_type",
    "applied_cost_per_quantity", "gross_amount", "discount_amount",
    "net_amount", "organization", "cost_center_name", "aic_quantity",
    "aic_gross_amount",
]


def _request(method: str, url: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-GitHub-Api-Version", API_VERSION)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"{method} {url} -> network error: {e.reason}") from None
    if not payload:
        return {}
    return json.loads(payload)


def create_report(enterprise: str, token: str, *, year: int | None = None,
                  month: int | None = None, day: int | None = None) -> str:
    """POST a new billing report; return its report id."""
    url = f"{API_ROOT}/enterprises/{enterprise}/settings/billing/reports"
    body: dict = {}
    if year is not None:
        body["year"] = year
    if month is not None:
        body["month"] = month
    if day is not None:
        body["day"] = day
    resp = _request("POST", url, token, body or None)
    report_id = resp.get("report_id") or resp.get("id")
    if not report_id:
        raise RuntimeError(f"create report: no report id in response: {resp}")
    return str(report_id)


def poll_report(enterprise: str, token: str, report_id: str, *,
                interval_s: float = 3.0, timeout_s: float = 300.0) -> dict:
    """GET the report until status == completed (or timeout)."""
    url = (f"{API_ROOT}/enterprises/{enterprise}/settings/billing/reports/"
           f"{report_id}")
    deadline = time.monotonic() + timeout_s
    while True:
        resp = _request("GET", url, token)
        status = str(resp.get("status", "")).lower()
        if status in ("completed", "complete", "done", "succeeded", ""):
            # Empty status with data present is treated as completed.
            if status or resp.get("data") or resp.get("rows") or resp.get("usageItems"):
                return resp
        if status in ("failed", "error"):
            raise RuntimeError(f"report {report_id} failed: {resp}")
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"report {report_id} not completed within {timeout_s:.0f}s "
                f"(last status: {status or 'unknown'})"
            )
        time.sleep(interval_s)


def _extract_rows(report: dict) -> list[dict]:
    """Pull the usage line-item list out of whatever envelope the API uses."""
    for key in ("usageItems", "rows", "data", "items"):
        val = report.get(key)
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            for inner in ("usageItems", "rows", "data", "items"):
                if isinstance(val.get(inner), list):
                    return val[inner]
    return []


def _normalize_row(item: dict) -> dict:
    """Map an API usage item to the estimator's CSV columns (best-effort)."""
    def g(*names, default=""):
        for n in names:
            if n in item and item[n] not in (None, ""):
                return item[n]
        return default

    return {
        "date": g("date", "usageAt", "day"),
        "username": g("username", "user", "userLogin", "actor"),
        "product": g("product"),
        "sku": g("sku"),
        "model": g("model", "modelName"),
        "quantity": g("quantity", "grossQuantity", "aic_quantity", default=0),
        "unit_type": g("unit_type", "unitType", default="ai-credits"),
        "applied_cost_per_quantity": g("applied_cost_per_quantity",
                                       "appliedCostPerQuantity", default=0.01),
        "gross_amount": g("gross_amount", "grossAmount", default=0),
        "discount_amount": g("discount_amount", "discountAmount", default=0),
        "net_amount": g("net_amount", "netAmount", default=0),
        "organization": g("organization", "org"),
        "cost_center_name": g("cost_center_name", "costCenterName", "cost_center"),
        "aic_quantity": g("aic_quantity", "aicQuantity", "quantity",
                          "grossQuantity", default=0),
        "aic_gross_amount": g("aic_gross_amount", "aicGrossAmount",
                              "gross_amount", "grossAmount", default=0),
    }


def write_csv(report: dict, out_path: Path) -> int:
    """Normalize a completed report into the estimator CSV. Return row count."""
    rows = _extract_rows(report)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for item in rows:
            writer.writerow(_normalize_row(item))
    return len(rows)


def resolve_token(explicit: str | None) -> str:
    token = explicit or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise RuntimeError(
            "no token provided: pass --token or set GITHUB_TOKEN / GH_TOKEN"
        )
    return token


def fetch_to_csv(enterprise: str, out_path: Path, *, token: str | None = None,
                 year: int | None = None, month: int | None = None,
                 day: int | None = None, interval_s: float = 3.0,
                 timeout_s: float = 300.0) -> int:
    """End-to-end: create report, poll, normalize to CSV. Return row count."""
    tok = resolve_token(token)
    print(f"# creating billing report for enterprise '{enterprise}'…",
          file=sys.stderr)
    report_id = create_report(enterprise, tok, year=year, month=month, day=day)
    print(f"# report id: {report_id} — polling until completed…",
          file=sys.stderr)
    report = poll_report(enterprise, tok, report_id,
                         interval_s=interval_s, timeout_s=timeout_s)
    count = write_csv(report, out_path)
    print(f"# wrote {count:,} usage rows -> {out_path}", file=sys.stderr)
    print("# NOTE: report contains credits/dollars, not tokens; run "
          "`aic_tokens.py estimate` to estimate tokens.", file=sys.stderr)
    return count
