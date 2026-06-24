# Copilot Instructions — AIC Token Estimator

## Setup

All dependencies are Python standard library only — no third-party packages. The venv lives at `tools/aic_token_estimator/.venv`. Always activate it and run commands from the **repo root**:

```bash
# One-time setup
python3 -m venv tools/aic_token_estimator/.venv
source tools/aic_token_estimator/.venv/bin/activate   # Windows: tools\aic_token_estimator\.venv\Scripts\activate
pip install -e .

# Every subsequent session
source tools/aic_token_estimator/.venv/bin/activate
```

Verify setup with the built-in selftest (all 6 checks must print `OK` or `PASS`):
```bash
python tools/aic_token_estimator/aic_tokens.py selftest
```

## Project overview

This repo contains `aic-token-estimator`, a Python CLI that converts GitHub Copilot **AI Credits** from a monthly AI Usage Report CSV into estimated token counts (input / cached / cache-write / output / total) per user, per model, per time period.

## Commands

**Verify setup (no data required):**
```bash
python tools/aic_token_estimator/aic_tokens.py selftest
```

**Run an estimate:**
```bash
python tools/aic_token_estimator/aic_tokens.py estimate \
  --csv copilot_usage_reports/<YYYY-month>/ai-usage-report.csv \
  --out output.csv \
  --period day|week|month \         # default: month
  --reports-dir token_estimate_reports \
  --report-stem my-report
```

**Fetch report via GitHub Billing API instead of manual CSV download:**
```bash
export GITHUB_TOKEN=...
python tools/aic_token_estimator/aic_tokens.py fetch \
  --enterprise <slug> --out report.csv
```

> Python-specific conventions live in `.github/instructions/python.instructions.md`.

## Architecture

```
tools/aic_token_estimator/
├── catalog.py      # ModelPricing dataclasses + pricing table; get_pricing() resolves messy model strings
├── estimator.py    # Core math: credits → USD → tokens via inversion; MIX_PRIORS per SKU; TokenEstimate
├── billing_api.py  # GitHub Enhanced Billing API client (urllib only); create+poll report, normalize to CSV
└── aic_tokens.py   # CLI entry point: subcommands estimate / selftest / fetch; CSV parsing + output
```

The estimation pipeline has two phases:
1. **Aggregate**: stream the input CSV, sum credits by `(user, bucket, model, sku)` — reduces calls from O(rows) to O(unique aggregates)
2. **Invert**: call `estimate_tokens()` once per aggregate; accumulate into output keyed by `(user, bucket, canonical_model)`

## Key conventions

**`Auto: <Model Name>` prefix in CSVs** means the user let Copilot select the model. `normalize_model()` in `catalog.py` strips this prefix and `estimator.py` applies `AUTO_DISCOUNT = 0.90` (GitHub charges 10% fewer dollars for the same tokens under auto-select, so inversion yields ~11.1% more tokens).

**Confidence bands are computed analytically**, not by sampling. Min-tokens corner = high `rho` + low cache; max-tokens corner = low `rho` + high cache. See `estimator.py::estimate_tokens()`.

**`MIX_PRIORS` in `estimator.py`** defines `(rho, c, w)` operating points keyed by SKU. The two main SKUs from the CSV are:
- `coding_agent_ai_credit` — agentic/edit mode (high cache, low output ratio)
- `copilot_ai_credit` — chat/review (moderate cache, higher output ratio)

**`catalog.py` pricing is dated.** The comment at the top of `catalog.py` includes the verification date. When GitHub changes pricing, only `catalog.py` needs updating. Legacy/deprecated models (e.g. `gpt-4.1`, `gpt-4o`) use proxy rates with explanatory comments.

**Outputs per run** (written to `--reports-dir/<YYYY-MM>/`):
- `<YYYY-MM>/<stem>.csv` — per `user × period × model` detail
- `<YYYY-MM>/<stem>-model-by-<period>.csv` — chart-ready pivot (tokens)
- `<YYYY-MM>/<stem>-model-by-<period>-usd.csv` — same pivot in USD
- `<YYYY-MM>/<stem>-tokens-by-user.csv` — per-user totals across all models
- `<YYYY-MM>/<stem>-executive-summary.md` — leadership snapshot

> File-type-specific rules live in `.github/instructions/`: `python`, `csv`, `toml`, `markdown`.

**No linter, formatter, or test framework** is configured. The only automated check is `selftest`.
