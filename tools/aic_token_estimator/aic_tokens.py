#!/usr/bin/env python3
"""
aic_tokens.py — CLI for estimating tokens from GitHub Copilot AI Credits.

Subcommands
-----------
  estimate   Read an AI Usage Report CSV and emit a per user x model x month
             token estimate (expected + low/high band), plus rollups.
  selftest   Run a built-in deterministic sanity check (no external data).

Examples
--------
  python aic_tokens.py estimate --csv ai_usage_report.csv --out estimate.csv
  python aic_tokens.py selftest

The AI Usage Report CSV is downloaded from GitHub:
  Enterprise/Org settings -> Billing -> AI usage -> download report (<=31 days).
It is grouped by date x model x username and contains AI-credit quantities, not
tokens. See docs.github.com/en/billing/reference/billing-reports
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

# Allow running the script directly (python tools/aic_token_estimator/aic_tokens.py)
# as well as via the installed package (pip install -e .).
_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from tools.aic_token_estimator.catalog import USD_PER_CREDIT, get_pricing
from tools.aic_token_estimator.estimator import calibrate_mix, estimate_tokens


# ── CSV column resolution ────────────────────────────────────────────────────
# The AI Usage Report has changed column names over time. We accept several.
CREDIT_COLS = ["aic_quantity", "quantity", "gross_quantity", "grossQuantity"]
MODEL_COLS = ["model"]
USER_COLS = ["username", "user"]
DATE_COLS = ["date"]
SKU_COLS = ["sku"]
ORG_COLS = ["organization", "org"]
COST_CENTER_COLS = ["cost_center_name", "cost_center"]


def _pick(row: dict, names: list[str]) -> str | None:
    for n in names:
        if n in row and str(row[n]).strip() != "":
            return str(row[n]).strip()
    # case-insensitive fallback
    lower = {k.lower(): k for k in row}
    for n in names:
        if n.lower() in lower:
            v = str(row[lower[n.lower()]]).strip()
            if v != "":
                return v
    return None


def _parse_date(date_str: str) -> date | None:
    """Parse a date (ISO YYYY-MM-DD or M/D/YY[YY]) into a date object."""
    s = date_str.strip()
    if not s:
        return None
    if "-" in s and len(s) >= 10 and s[4] == "-":
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 3:
            m, d, y = parts
            y = y if len(y) == 4 else ("20" + y.zfill(2))
            try:
                return date(int(y), int(m), int(d))
            except ValueError:
                return None
    return None


VALID_PERIODS = ("day", "week", "month")


def _bucket_of(date_str: str, period: str = "month") -> str:
    """Normalize a date to a bucket key for the requested period.

    period 'day'   -> YYYY-MM-DD
    period 'week'  -> YYYY-Www (ISO week, Monday-start)
    period 'month' -> YYYY-MM
    """
    d = _parse_date(date_str)
    if d is None:
        # Fall back to a month-ish slice when the date can't be parsed.
        return date_str.strip()[:7]
    if period == "day":
        return d.strftime("%Y-%m-%d")
    if period == "week":
        iso = d.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    return d.strftime("%Y-%m")


def _slug(s: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in s).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out or "report"


def _resolve_reports_dir(preferred: str | None = None) -> Path:
    if preferred:
        return Path(preferred)
    return Path("token_estimate_reports")


# ── XLSX input support ───────────────────────────────────────────────────────
# .xlsx files are ZIP archives of XML. We parse them with stdlib zipfile +
# xml.etree.ElementTree so no third-party packages are required.

_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_CELL_REF_RE = re.compile(r"([A-Z]+)(\d+)")


def _col_letter_to_idx(col: str) -> int:
    """Convert a spreadsheet column letter (A, B, AA, …) to a 0-based index."""
    result = 0
    for ch in col.upper():
        result = result * 26 + (ord(ch) - ord("A") + 1)
    return result - 1


def _xlsx_to_rows(path: Path) -> list[dict[str, str]]:
    """Read the first sheet of an .xlsx file and return rows as a list of dicts.

    The first row is treated as headers. Empty cells are returned as empty
    strings. All values are returned as strings (matching csv.DictReader).
    """
    # .xlsx files are ZIP archives. Encrypted or legacy .xls files use the
    # OLE2 compound document format (magic bytes d0 cf 11 e0) and cannot be
    # parsed here — the user must re-save as unencrypted .xlsx or export to CSV.
    try:
        zf_ctx = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        with path.open("rb") as f:
            magic = f.read(8)
        if magic[:4] == b"\xd0\xcf\x11\xe0":
            raise ValueError(
                f"{path.name} appears to be an encrypted .xlsx or a legacy "
                ".xls file. Open it in Excel, remove any password protection, "
                "and re-save as .xlsx — or export directly to CSV."
            ) from None
        raise ValueError(
            f"{path.name} is not a valid .xlsx file (not a zip archive)."
        ) from None

    with zf_ctx as zf:
        names = zf.namelist()

        # ── Shared strings ───────────────────────────────────────────────────
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            with zf.open("xl/sharedStrings.xml") as f:
                ss_root = ET.parse(f).getroot()
            for si in ss_root.iter(f"{_XLSX_NS}si"):
                shared.append(
                    "".join(t.text or "" for t in si.iter(f"{_XLSX_NS}t"))
                )

        # ── Sheet data ───────────────────────────────────────────────────────
        sheet_name = "xl/worksheets/sheet1.xml"
        if sheet_name not in names:
            return []
        with zf.open(sheet_name) as f:
            ws_root = ET.parse(f).getroot()

    grid: dict[int, dict[int, str]] = {}
    max_col = 0

    for row_el in ws_root.iter(f"{_XLSX_NS}row"):
        row_idx = int(row_el.get("r", "0")) - 1
        grid[row_idx] = {}
        for c_el in row_el.iter(f"{_XLSX_NS}c"):
            ref = c_el.get("r", "")
            m = _CELL_REF_RE.match(ref.upper())
            if not m:
                continue
            col_idx = _col_letter_to_idx(m.group(1))
            max_col = max(max_col, col_idx)

            ctype = c_el.get("t", "n")
            v_el = c_el.find(f"{_XLSX_NS}v")
            is_el = c_el.find(f"{_XLSX_NS}is")

            if ctype == "s":
                raw = v_el.text if v_el is not None else None
                val = shared[int(raw)] if raw is not None else ""
            elif ctype == "inlineStr":
                val = (
                    "".join(t.text or "" for t in c_el.iter(f"{_XLSX_NS}t"))
                    if is_el is not None else ""
                )
            elif ctype == "b":
                val = "TRUE" if (v_el is not None and v_el.text == "1") else "FALSE"
            else:
                # number, formula string result, error — return as-is
                val = v_el.text if v_el is not None and v_el.text is not None else ""
            grid[row_idx][col_idx] = val

    if not grid:
        return []

    min_row = min(grid.keys())
    headers = [grid[min_row].get(c, "") for c in range(max_col + 1)]

    return [
        {headers[c]: grid[row_idx].get(c, "") for c in range(len(headers))}
        for row_idx in sorted(grid.keys())
        if row_idx != min_row
    ]


def _open_input(path: Path) -> list[dict[str, str]]:
    """Return all data rows from a .csv or .xlsx file as a list of dicts."""
    if path.suffix.lower() == ".xlsx":
        print(f"# detected .xlsx — reading with built-in parser", file=sys.stderr)
        return _xlsx_to_rows(path)
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _derive_subdir(buckets_sorted: list[str], period: str) -> str:
    """Derive a dated subdir name from the latest period bucket in the data.

    - day   → YYYY-MM-DD  (full date; single-day runs land in their exact date)
    - week  → YYYY-MM     (month of the latest week's Monday)
    - month → YYYY-MM
    """
    if not buckets_sorted:
        return datetime.now(timezone.utc).strftime("%Y-%m")
    latest = buckets_sorted[-1]
    if period == "day":
        # Bucket is already YYYY-MM-DD — use it directly
        return latest
    if period == "month":
        # Bucket is already YYYY-MM
        return latest
    # period == "week": bucket is YYYY-Www; use the Monday of that ISO week
    parts = latest.split("-W")
    if len(parts) == 2:
        yr, wk = int(parts[0]), int(parts[1])
        d = date.fromisocalendar(yr, wk, 1)
        return d.strftime("%Y-%m")
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _write_csv(path: Path, rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out)
        writer.writerows(rows)


def _build_pivot(
    period_label: str,
    buckets: list[str],
    models: list[str],
    cell_values: dict[tuple, float],
    *,
    as_int: bool,
) -> list[list]:
    """Build a chart-ready period × model pivot table (rows + TOTAL row)."""
    def fmt(v: float):
        return int(v) if as_int else round(v, 4)

    header = [period_label] + models + ["TOTAL"]
    rows: list[list] = [header]
    col_totals = {m: 0.0 for m in models}
    grand_total = 0.0
    for b in buckets:
        row: list = [b]
        row_total = 0.0
        for m in models:
            v = cell_values.get((b, m), 0.0)
            row.append(fmt(v))
            col_totals[m] += v
            row_total += v
        row.append(fmt(row_total))
        grand_total += row_total
        rows.append(row)
    total_row: list = ["TOTAL"]
    for m in models:
        total_row.append(fmt(col_totals[m]))
    total_row.append(fmt(grand_total))
    rows.append(total_row)
    return rows


def _build_executive_summary_markdown(
    *,
    source_csv: Path,
    generated_at: str,
    period: str,
    period_label: str,
    data_buckets: list[str],
    raw_row_count: int,
    aggregate_count: int,
    grand: dict[str, float],
    unresolved_pct: float,
    model_rollup: dict[str, dict[str, float]],
    user_rollup: dict[str, dict[str, float]],
    period_rollup: dict[str, dict[str, float]],
    period_users: dict[str, set[str]],
    period_model_tokens: dict[tuple, float],
    detailed_csv_path: Path,
    tokens_pivot_path: Path,
    usd_pivot_path: Path,
    user_totals_path: Path,
) -> str:
    buckets = [b for b in data_buckets if b]
    reporting_period = f"{buckets[0]} to {buckets[-1]}" if buckets else "Unknown"
    total_cost = grand["credits"] * USD_PER_CREDIT
    resolved_models = sum(1 for model in model_rollup if get_pricing(model) is not None)
    # All models sorted by cost descending.
    all_models = sorted(model_rollup.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True)
    # Top 5 kept for the time-series model matrix (chart headings).
    top_models = all_models[:5]
    top_users = sorted(user_rollup.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True)[:10]
    trend_rows = sorted(period_rollup.items(), key=lambda kv: kv[0])
    trend_heading = {"day": "Daily Trend", "week": "Weekly Trend",
                     "month": "Monthly Trend"}.get(period, "Trend")
    col_title = period_label.capitalize()

    # Relative links (all files live in the same directory as this summary).
    detail_link = f"[{detailed_csv_path.name}](./{detailed_csv_path.name})"
    tokens_pivot_link = f"[{tokens_pivot_path.name}](./{tokens_pivot_path.name})"
    usd_pivot_link = f"[{usd_pivot_path.name}](./{usd_pivot_path.name})"
    user_totals_link = f"[{user_totals_path.name}](./{user_totals_path.name})"

    lines: list[str] = []
    lines.append("# Executive Summary — AI Credit to Token Estimate")
    lines.append("")
    lines.append(f"**Generated:** {generated_at}")
    lines.append(f"**Source CSV:** `{source_csv}`")
    lines.append("")
    lines.append("## Report Artifacts")
    lines.append("")
    lines.append("| File | Description |")
    lines.append("|---|---|")
    lines.append(f"| {detail_link} | Per-user × {period_label} × model detail (credits, cost, token split, confidence band) |")
    lines.append(f"| {user_totals_link} | Per-user totals across all models — one row per user, sorted by cost |")
    lines.append(f"| {tokens_pivot_link} | Chart-ready pivot: rows = {period_label}, columns = model, cells = est. tokens |")
    lines.append(f"| {usd_pivot_link} | Same pivot in exact USD (reconciles to the penny) |")
    lines.append("")
    lines.append("> Full per-user cost breakdown is in the detailed CSV above. "
                 "The tables below show top users only.")
    lines.append("")
    lines.append("## Leadership Snapshot")
    lines.append("")
    lines.append(f"- **Reporting period:** {reporting_period}")
    lines.append(f"- **Granularity:** {period}")
    lines.append(f"- **Unique users:** {len(user_rollup)}")
    lines.append(f"- **Total AI Credits consumed:** {grand['credits']:.1f}")
    lines.append(f"- **Total spend (USD):** ${total_cost:,.2f}")
    lines.append(f"- **Estimated total tokens (cost-weighted):** {int(grand['total']):,}")
    lines.append(
        f"- **Estimated token confidence band:** {int(grand['low']):,} to {int(grand['high']):,}"
    )
    lines.append(f"- **Unresolved credits:** {grand['unresolved']:.1f} ({unresolved_pct:.2f}%)")
    lines.append("")
    lines.append("## What leadership should use this for")
    lines.append("")
    lines.append(
        "Use this summary to track spend pace, identify which models and users drive cost, "
        "and compare token-normalized costs across providers."
    )
    lines.append("")
    lines.append("## Cost Breakdown by Model")
    lines.append("")
    lines.append("| Model | Cost (USD) | Credits | Est. Tokens | Share of Total Cost |")
    lines.append("|---|---:|---:|---:|---:|")
    for model, values in all_models:
        share = (values["cost_usd"] / total_cost * 100.0) if total_cost > 0 else 0.0
        lines.append(
            f"| {model} | ${values['cost_usd']:,.2f} | {values['credits']:.1f} | "
            f"{int(values['tokens']):,} | {share:.1f}% |"
        )
    if not all_models:
        lines.append("| _(none)_ | $0.00 | 0.0 | 0 | 0.0% |")
    lines.append("")
    lines.append("## Top Cost Drivers by User")
    lines.append("")
    lines.append(
        f"> Showing top {len(top_users)} of {len(user_rollup):,} users by cost. "
        f"See {user_totals_link} for all users (one row per user, all models combined) "
        f"or {detail_link} for the full per-user × model breakdown."
    )
    lines.append("")
    lines.append("| User | Cost (USD) | Credits | Est. Tokens | Share of Total Cost |")
    lines.append("|---|---:|---:|---:|---:|")
    for user, values in top_users:
        share = (values["cost_usd"] / total_cost * 100.0) if total_cost > 0 else 0.0
        lines.append(
            f"| {user} | ${values['cost_usd']:,.2f} | {values['credits']:.1f} | "
            f"{int(values['tokens']):,} | {share:.1f}% |"
        )
    if not top_users:
        lines.append("| _(none)_ | $0.00 | 0.0 | 0 | 0.0% |")
    lines.append("")
    lines.append(f"## {trend_heading}")
    lines.append("")
    lines.append(f"| {col_title} | Users | Cost (USD) | Credits | Est. Tokens |")
    lines.append("|---|---:|---:|---:|---:|")
    for bucket, values in trend_rows:
        n_users = len(period_users.get(bucket, set()))
        lines.append(
            f"| {bucket} | {n_users} | ${values['cost_usd']:,.2f} | {values['credits']:.1f} | "
            f"{int(values['tokens']):,} |"
        )
    if not trend_rows:
        lines.append("| _(none)_ | 0 | $0.00 | 0.0 | 0 |")
    lines.append("")
    lines.append(f"## Estimated Token Usage by Model over Time (per {period_label})")
    lines.append("")
    top_model_names = [m for m, _ in top_models]
    if top_model_names and buckets:
        lines.append(
            "| " + col_title + " | " + " | ".join(top_model_names)
            + " | Other | Total |"
        )
        lines.append("|---|" + "---:|" * (len(top_model_names) + 2))
        for b in buckets:
            bucket_total = sum(
                v for (bb, _m), v in period_model_tokens.items() if bb == b
            )
            cells: list[str] = []
            top_sum = 0.0
            for m in top_model_names:
                t = period_model_tokens.get((b, m), 0.0)
                cells.append(f"{int(t):,}")
                top_sum += t
            other = max(bucket_total - top_sum, 0.0)
            cells.append(f"{int(other):,}")
            cells.append(f"{int(bucket_total):,}")
            lines.append("| " + b + " | " + " | ".join(cells) + " |")
        lines.append("")
        lines.append(
            "_Columns are the top 5 cost-driving models; remaining models are summed "
            "into **Other**. Token counts are estimates; only dollars are exact._"
        )
    else:
        lines.append("_(no model activity in range)_")
    lines.append("")
    lines.append("## Data quality and assumptions")
    lines.append("")
    lines.append(f"- Raw CSV rows processed: **{raw_row_count:,}**")
    lines.append(
        f"- Unique aggregates estimated (user × {period_label} × model × sku): **{aggregate_count:,}**"
    )
    lines.append(f"- Resolved models in pricing catalog: **{resolved_models}**")
    lines.append(
        "- Dollars are exact (`credits × $0.01`). Token splits are estimated from model pricing and "
        "feature priors."
    )
    lines.append(
        "- **Token figures are cost-weighted (billable) estimates, not raw throughput.** They are "
        "inverted from dollars, where GitHub bills cache-read tokens at ~10% of the uncached-input "
        "rate. A raw token-throughput report (e.g. the GitHub Copilot metrics export) counts those "
        "cached tokens at 100%, so it can read several times higher for cache-heavy agentic usage. "
        "Both views are valid — they measure different things."
    )
    lines.append(
        "- IDE **code completions and next edit suggestions are excluded**: GitHub does not bill them "
        "in AI credits, so they carry no credits to invert (a throughput report may still count them)."
    )
    lines.append(
        "- To reconcile with a measured throughput figure, re-run with `--cache-fraction <c>` (and "
        "optionally `--output-ratio <rho>`) to pin the token mix to observed values."
    )
    lines.append(
        "- Confidence bands (`total_tokens_low/high`) express plausible token range for planning and "
        "cross-provider comparison."
    )
    lines.append("")
    lines.append("## Recommended actions")
    lines.append("")
    if unresolved_pct > 0:
        lines.append(
            f"- Add catalog mappings for unresolved model names ({grand['unresolved']:.1f} credits) "
            "to improve estimate coverage."
        )
    else:
        lines.append("- Catalog coverage is complete for this run (0 unresolved credits).")
    if all_models:
        lines.append(
            f"- Prioritize optimization on **{all_models[0][0]}**, currently the largest cost driver."
        )
    lines.append(
        f"- Use {user_totals_link} for per-user chargeback (one row per user, all models combined)."
    )
    lines.append(
        f"- Use {detail_link} for per-user × model drill-down and leadership review."
    )
    lines.append(
        f"- Use {tokens_pivot_link} and {usd_pivot_link} to chart token usage and spend per model over time."
    )
    lines.append("")
    return "\n".join(lines)


def cmd_estimate(args: argparse.Namespace) -> int:
    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"error: input file not found: {csv_path}", file=sys.stderr)
        return 2

    period = (getattr(args, "period", "month") or "month").lower()
    if period not in VALID_PERIODS:
        print(f"error: --period must be one of {', '.join(VALID_PERIODS)}",
              file=sys.stderr)
        return 2
    period_label = {"day": "date", "week": "week", "month": "month"}[period]

    # Optional calibration overrides. When supplied, the assumed cache fraction
    # and/or output ratio are pinned to the measured value for every row, which
    # lets the (otherwise cost-weighted) estimate reproduce a token-throughput
    # figure. See `calibrate_mix` in estimator.py.
    cache_override = getattr(args, "cache_fraction", None)
    if cache_override is not None and not 0.0 <= cache_override <= 1.0:
        print("error: --cache-fraction must be between 0 and 1", file=sys.stderr)
        return 2
    rho_override = getattr(args, "output_ratio", None)
    if rho_override is not None and rho_override < 0.0:
        print("error: --output-ratio must be >= 0", file=sys.stderr)
        return 2
    cwrite_override = getattr(args, "cache_write_fraction", None)
    if cwrite_override is not None and not 0.0 <= cwrite_override <= 1.0:
        print("error: --cache-write-fraction must be between 0 and 1", file=sys.stderr)
        return 2
    calibrating = (cache_override is not None or rho_override is not None
                   or cwrite_override is not None)

    # Phase 1: stream the input and aggregate raw credits by
    # (user, bucket, model_raw, sku). This reduces the number of
    # estimate_tokens() calls from O(rows) to O(unique aggregates) —
    # typically 30-100× fewer calls for month-sized reports.
    raw_agg: dict[tuple, float] = defaultdict(float)
    raw_row_count = 0

    try:
        input_rows = _open_input(csv_path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for row in input_rows:
        credit_raw = _pick(row, CREDIT_COLS)
        if credit_raw is None:
            continue
        try:
            credits = float(credit_raw)
        except ValueError:
            continue
        if credits <= 0:
            continue
        model_raw = _pick(row, MODEL_COLS) or "unknown"
        sku = _pick(row, SKU_COLS) or ""
        user = _pick(row, USER_COLS) or "(unknown)"
        bucket = _bucket_of(_pick(row, DATE_COLS) or "", period)
        raw_agg[(user, bucket, model_raw, sku)] += credits
        raw_row_count += 1

    print(f"# read {raw_row_count:,} rows → {len(raw_agg):,} unique "
          f"(user × {period_label} × model × sku) aggregates", file=sys.stderr)

    # Phase 2: call estimate_tokens once per aggregate, roll up into output
    # keyed by (user, bucket, canonical_model).
    agg: dict[tuple, dict] = defaultdict(lambda: {
        "credits": 0.0, "input": 0.0, "cached": 0.0, "cwrite": 0.0,
        "output": 0.0, "total": 0.0, "low": 0.0, "high": 0.0, "unresolved": 0.0})

    for (user, bucket, model_raw, sku), credits in raw_agg.items():
        mix = (calibrate_mix(sku or None, cache_fraction=cache_override,
                             output_ratio=rho_override,
                             cache_write_fraction=cwrite_override)
               if calibrating else None)
        est = estimate_tokens(credits, model_raw, sku or None, mix=mix)
        a = agg[(user, bucket, est.model)]
        a["credits"] += credits
        if est.resolved:
            a["input"] += est.input_tokens
            a["cached"] += est.cached_tokens
            a["cwrite"] += est.cache_write_tokens
            a["output"] += est.output_tokens
            a["total"] += est.total_tokens
            a["low"] += est.total_low
            a["high"] += est.total_high
        else:
            a["unresolved"] += credits

    # Phase 3: build output rows + leadership rollups.
    header = ["username", period_label, "model", "credits", "cost_usd",
              "est_input_tokens", "est_cached_tokens", "est_cache_write_tokens",
              "est_output_tokens", "est_total_tokens",
              "total_tokens_low", "total_tokens_high", "unresolved_credits"]
    rows: list[list] = [header]
    grand = defaultdict(float)
    model_rollup: dict[str, dict[str, float]] = defaultdict(
        lambda: {"credits": 0.0, "cost_usd": 0.0, "tokens": 0.0}
    )
    user_rollup: dict[str, dict[str, float]] = defaultdict(
        lambda: {"credits": 0.0, "cost_usd": 0.0, "tokens": 0.0, "low": 0.0, "high": 0.0}
    )
    period_rollup: dict[str, dict[str, float]] = defaultdict(
        lambda: {"credits": 0.0, "cost_usd": 0.0, "tokens": 0.0}
    )
    # Model × period matrices for the dashboard pivot + time-series section.
    period_model_tokens: dict[tuple, float] = defaultdict(float)
    period_model_cost: dict[tuple, float] = defaultdict(float)
    # Unique users per period bucket (for per-granularity user counts).
    period_users: dict[str, set[str]] = defaultdict(set)
    buckets_seen: set[str] = set()
    for (user, bucket, model), a in sorted(agg.items()):
        row = [
            user, bucket, model, round(a["credits"], 3),
            round(a["credits"] * USD_PER_CREDIT, 4),
            int(a["input"]), int(a["cached"]), int(a["cwrite"]),
            int(a["output"]), int(a["total"]),
            int(a["low"]), int(a["high"]), round(a["unresolved"], 3),
        ]
        rows.append(row)
        cost_usd = a["credits"] * USD_PER_CREDIT
        model_rollup[model]["credits"] += a["credits"]
        model_rollup[model]["cost_usd"] += cost_usd
        model_rollup[model]["tokens"] += a["total"]
        user_rollup[user]["credits"] += a["credits"]
        user_rollup[user]["cost_usd"] += cost_usd
        user_rollup[user]["tokens"] += a["total"]
        user_rollup[user]["low"] += a["low"]
        user_rollup[user]["high"] += a["high"]
        period_rollup[bucket]["credits"] += a["credits"]
        period_rollup[bucket]["cost_usd"] += cost_usd
        period_rollup[bucket]["tokens"] += a["total"]
        period_model_tokens[(bucket, model)] += a["total"]
        period_model_cost[(bucket, model)] += cost_usd
        period_users[bucket].add(user)
        buckets_seen.add(bucket)
        for k in ("credits", "input", "cached", "cwrite", "output", "total",
                  "low", "high", "unresolved"):
            grand[k] += a[k]

    if args.out:
        out_path = Path(args.out)
        _write_csv(out_path, rows)
    else:
        writer = csv.writer(sys.stdout)
        writer.writerows(rows)
        out_path = None

    reports_base = _resolve_reports_dir(args.reports_dir)
    # Organize reports into a dated subdir: YYYY-MM-DD for day period, YYYY-MM otherwise.
    buckets_sorted = sorted(buckets_seen)
    reports_subdir = _derive_subdir(buckets_sorted, period)
    reports_dir = reports_base / reports_subdir
    reports_dir.mkdir(parents=True, exist_ok=True)

    # For single-day runs without an explicit stem, append the date to the
    # filename so every artifact is self-describing (e.g. estimate-2026-05-15.csv).
    default_stem = (
        Path(args.out).stem if args.out
        else f"{csv_path.stem}-estimate"
    )
    if args.report_stem:
        report_stem = _slug(args.report_stem)
    elif period == "day" and len(buckets_sorted) == 1:
        report_stem = _slug(f"{default_stem}-{buckets_sorted[0]}")
    else:
        report_stem = _slug(default_stem)

    reports_csv_path = reports_dir / f"{report_stem}.csv"
    if out_path is None or out_path.resolve() != reports_csv_path.resolve():
        _write_csv(reports_csv_path, rows)

    # Dashboard pivots: model × period matrices (chart-ready for Excel/BI).
    models_sorted = sorted(model_rollup.keys())
    tokens_pivot = _build_pivot(period_label, buckets_sorted, models_sorted,
                                period_model_tokens, as_int=True)
    usd_pivot = _build_pivot(period_label, buckets_sorted, models_sorted,
                             period_model_cost, as_int=False)
    tokens_pivot_path = reports_dir / f"{report_stem}-model-by-{period}.csv"
    usd_pivot_path = reports_dir / f"{report_stem}-model-by-{period}-usd.csv"
    _write_csv(tokens_pivot_path, tokens_pivot)
    _write_csv(usd_pivot_path, usd_pivot)

    # Per-user totals (all models combined): one row per user sorted by cost descending.
    user_totals_header = [
        "username", "credits", "cost_usd",
        "est_total_tokens", "total_tokens_low", "total_tokens_high",
    ]
    user_totals_rows: list[list] = [user_totals_header]
    for user, u in sorted(user_rollup.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True):
        user_totals_rows.append([
            user,
            round(u["credits"], 3),
            round(u["cost_usd"], 4),
            int(u["tokens"]),
            int(u["low"]),
            int(u["high"]),
        ])
    user_totals_path = reports_dir / f"{report_stem}-tokens-by-user.csv"
    _write_csv(user_totals_path, user_totals_rows)

    unresolved_pct = (grand["unresolved"] / grand["credits"] * 100.0) if grand["credits"] > 0 else 0.0
    summary_md = _build_executive_summary_markdown(
        source_csv=csv_path,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        period=period,
        period_label=period_label,
        data_buckets=buckets_sorted,
        raw_row_count=raw_row_count,
        aggregate_count=len(raw_agg),
        grand=grand,
        unresolved_pct=unresolved_pct,
        model_rollup=model_rollup,
        user_rollup=user_rollup,
        period_rollup=period_rollup,
        period_users=period_users,
        period_model_tokens=period_model_tokens,
        detailed_csv_path=reports_csv_path,
        tokens_pivot_path=tokens_pivot_path,
        usd_pivot_path=usd_pivot_path,
        user_totals_path=user_totals_path,
    )
    summary_path = Path(args.summary_out) if args.summary_out else reports_dir / f"{report_stem}-executive-summary.md"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(summary_md, encoding="utf-8")

    print("\n# ── Grand totals ─────────────────────────────", file=sys.stderr)
    print(f"# period             : {period}", file=sys.stderr)
    print(f"# unique users       : {len(user_rollup)}", file=sys.stderr)
    print(f"# credits            : {grand['credits']:.1f}", file=sys.stderr)
    print(f"# cost (USD)         : {grand['credits'] * USD_PER_CREDIT:.2f}", file=sys.stderr)
    print(f"# est total tokens   : {int(grand['total']):,}", file=sys.stderr)
    print(f"#   band low / high  : {int(grand['low']):,} / {int(grand['high']):,}", file=sys.stderr)
    if calibrating:
        bits = []
        if cache_override is not None:
            bits.append(f"cache-fraction={cache_override:g}")
        if rho_override is not None:
            bits.append(f"output-ratio={rho_override:g}")
        if cwrite_override is not None:
            bits.append(f"cache-write-fraction={cwrite_override:g}")
        print(f"#   calibration     : {', '.join(bits)} (pinned to measured values)",
              file=sys.stderr)
    else:
        print("#   basis           : COST-WEIGHTED (billable) tokens, not raw "
              "throughput.", file=sys.stderr)
        print("#                      Cache reads bill at ~10% but a throughput "
              "report counts them 100%;", file=sys.stderr)
        print("#                      reconcile with --cache-fraction <c>.",
              file=sys.stderr)
    print(f"#   input (uncached) : {int(grand['input']):,}", file=sys.stderr)
    print(f"#   cached           : {int(grand['cached']):,}", file=sys.stderr)
    print(f"#   cache-write      : {int(grand['cwrite']):,}", file=sys.stderr)
    print(f"#   output           : {int(grand['output']):,}", file=sys.stderr)
    if grand["unresolved"]:
        print(f"# UNRESOLVED credits : {grand['unresolved']:.1f} "
              f"(model not in catalog)", file=sys.stderr)
    print(f"# detailed CSV       : {reports_csv_path}", file=sys.stderr)
    print(f"# tokens by user CSV : {user_totals_path}", file=sys.stderr)
    print(f"# model×{period} CSV  : {tokens_pivot_path}", file=sys.stderr)
    print(f"# model×{period} USD  : {usd_pivot_path}", file=sys.stderr)
    print(f"# exec summary (MD)  : {summary_path}", file=sys.stderr)
    return 0


def cmd_selftest(_args: argparse.Namespace) -> int:
    """Round-trip check: build credits from known tokens, then invert."""
    from tools.aic_token_estimator.catalog import get_pricing
    failures = 0

    # 1) Forward/inverse consistency for GPT-5 mini using the Medium example.
    #    20k input, 10k cached, 3k output -> 1.125 credits (docs example).
    p = get_pricing("gpt-5-mini")
    assert p is not None
    cost = (20_000 * p.inp + 10_000 * p.cache + 3_000 * p.out) / 1_000_000
    credits = cost / USD_PER_CREDIT
    print(f"[1] GPT-5 mini 20k/10k/3k -> {credits:.3f} credits "
          f"(expected 1.125)")
    if abs(credits - 1.125) > 1e-6:
        failures += 1

    # 2) Inversion recovers total tokens when the TRUE mix is supplied.
    from tools.aic_token_estimator.estimator import Mix
    total_input = 30_000  # 20k uncached + 10k cached
    rho = 3_000 / total_input
    c = 10_000 / total_input
    true_mix = Mix(rho, rho, rho, c, c, c, w_ex=0.0)
    est = estimate_tokens(credits, "gpt-5-mini", mix=true_mix)
    actual_total = 20_000 + 10_000 + 3_000
    err = abs(est.total_tokens - actual_total) / actual_total
    print(f"[2] inversion with true mix -> {int(est.total_tokens):,} tokens "
          f"(actual 33,000; err {err*100:.2f}%)")
    if err > 0.005:
        failures += 1

    # 3) Auto-select -10% yields ~11.1% MORE tokens for the same credits.
    base = estimate_tokens(1.0, "claude-sonnet-4.6")
    auto = estimate_tokens(1.0, "Auto: Claude Sonnet 4.6")
    ratio = auto.total_tokens / base.total_tokens
    print(f"[3] auto-select token uplift -> x{ratio:.4f} (expected x1.1111)")
    if abs(ratio - (1 / 0.9)) > 1e-3:
        failures += 1

    # 4) Band brackets the expected point estimate.
    e = estimate_tokens(100.0, "claude-sonnet-4.6", "copilot_ai_credit")
    ok = e.total_low <= e.total_tokens <= e.total_high
    print(f"[4] band low<=exp<=high -> {int(e.total_low):,} <= "
          f"{int(e.total_tokens):,} <= {int(e.total_high):,}  {'OK' if ok else 'FAIL'}")
    if not ok:
        failures += 1

    # 5) Bucket resolution: same date maps to day/week/month correctly.
    day_b = _bucket_of("6/3/26", "day")
    week_b = _bucket_of("6/3/26", "week")
    month_b = _bucket_of("6/3/26", "month")
    bucket_ok = (day_b == "2026-06-03" and week_b == "2026-W23"
                 and month_b == "2026-06")
    print(f"[5] bucket day/week/month -> {day_b} / {week_b} / {month_b}  "
          f"{'OK' if bucket_ok else 'FAIL'}")
    if not bucket_ok:
        failures += 1

    # 6) Pivot conservation: cell values sum to the grand TOTAL.
    cells = {("2026-06-01", "gpt-5.4"): 100.0,
             ("2026-06-01", "claude-opus-4.8"): 50.0,
             ("2026-06-02", "gpt-5.4"): 25.0}
    pivot = _build_pivot("date", ["2026-06-01", "2026-06-02"],
                         ["claude-opus-4.8", "gpt-5.4"], cells, as_int=True)
    grand_total_cell = pivot[-1][-1]  # TOTAL row, TOTAL column
    pivot_ok = grand_total_cell == 175
    print(f"[6] pivot conservation -> grand total {grand_total_cell} "
          f"(expected 175)  {'OK' if pivot_ok else 'FAIL'}")
    if not pivot_ok:
        failures += 1

    print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILURE(S)'}")
    return 1 if failures else 0


def cmd_fetch(args: argparse.Namespace) -> int:
    """Pull an AI usage report via the billing-reports API into a CSV."""
    from tools.aic_token_estimator.billing_api import fetch_to_csv
    try:
        fetch_to_csv(
            args.enterprise, Path(args.out), token=args.token,
            year=args.year, month=args.month, day=args.day,
            interval_s=args.poll_interval, timeout_s=args.timeout,
        )
    except (RuntimeError, TimeoutError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("estimate", help="estimate tokens from an AI Usage Report CSV")
    pe.add_argument("--csv", required=True, help="path to AI Usage Report CSV or XLSX")
    pe.add_argument("--out", help="write per-row CSV here (default: stdout)")
    pe.add_argument(
        "--period", choices=VALID_PERIODS, default="month",
        help="time bucket for trend + model pivot (default: month)",
    )
    pe.add_argument("--summary-out", help="write executive summary markdown here")
    pe.add_argument(
        "--reports-dir",
        help="folder to also store detailed CSV + executive summary "
             "(default: token_estimate_reports)",
    )
    pe.add_argument("--report-stem", help="base filename for reports-dir artifacts")
    pe.add_argument(
        "--cache-fraction", type=float, metavar="C",
        help="pin the assumed cache-read fraction of input tokens (0-1) for all "
             "rows. Use this to reconcile the (cost-weighted) estimate against a "
             "measured token-throughput report; higher C => more tokens per credit.",
    )
    pe.add_argument(
        "--output-ratio", type=float, metavar="RHO",
        help="pin the assumed output:input token ratio for all rows (calibration "
             "knob; pairs with --cache-fraction).",
    )
    pe.add_argument(
        "--cache-write-fraction", type=float, metavar="W",
        help="pin the assumed cache-write fraction of input tokens (0-1). Needed "
             "only for ultra-cache-heavy tenants where --cache-fraction exceeds the "
             "prior cache-write floor.",
    )
    pe.set_defaults(func=cmd_estimate)

    pf = sub.add_parser(
        "fetch",
        help="download an AI usage report via the billing-reports API into a CSV",
    )
    pf.add_argument("--enterprise", required=True, help="enterprise slug")
    pf.add_argument("--out", required=True, help="path to write the usage CSV")
    pf.add_argument("--token", help="GitHub token (else GITHUB_TOKEN / GH_TOKEN)")
    pf.add_argument("--year", type=int, help="report year (optional)")
    pf.add_argument("--month", type=int, help="report month 1-12 (optional)")
    pf.add_argument("--day", type=int, help="report day 1-31 (optional)")
    pf.add_argument("--poll-interval", type=float, default=3.0,
                    help="seconds between status polls (default: 3)")
    pf.add_argument("--timeout", type=float, default=300.0,
                    help="max seconds to wait for completion (default: 300)")
    pf.set_defaults(func=cmd_fetch)

    ps = sub.add_parser("selftest", help="run built-in deterministic checks")
    ps.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
