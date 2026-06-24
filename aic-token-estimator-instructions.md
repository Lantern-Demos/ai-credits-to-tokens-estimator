# AIC Token Estimator — Setup & Usage Instructions

Converts GitHub Copilot **AI Credits** from your monthly AI Usage Report CSV into
estimated token counts (input / cached / cache-write / output / total) per user,
per model, per period.

---

## Background: Why credit → token is an estimation problem

**1 AI Credit = $0.01 USD. Fixed. Always.**
This is the only credit↔dollar conversion GitHub uses. ([Source](https://docs.github.com/en/copilot/concepts/billing/usage-based-billing-for-organizations-and-enterprises))

GitHub prices every interaction by **tokens**, sums the cost in dollars, divides by
`$0.01`, and only then shows you **credits**. Tokens are destroyed in that compression.
The Billing API and the AI Usage Report CSV expose **only the final credit number** —
never the token counts. So a single credit value is not directly invertible to a unique
token count: it collapses input, cache, and output into one scalar.

**The unlock:** GitHub gives you credits **split by model and by user** in the AI Usage
Report. Because per-model token rates differ by 10–150×, decomposing credits by model
first removes the single biggest source of error. From there the tool inverts each
model's credits using calibrated mix assumptions and reports a confidence band.

---

## How the tool works

The pipeline runs in three phases every time you call `estimate`:

### Phase 1 — Aggregate

`aic_tokens.py` streams the input CSV and sums raw credits by `(user, period-bucket, model, sku)`. This collapses thousands of daily rows into a compact set of unique aggregates, reducing the number of inversion calls from O(rows) to O(unique user × model × period combinations).

### Phase 2 — Invert

For each aggregate, `estimator.py` runs the inversion:

```
C_usd  = credits × $0.01                    # exact dollars GitHub charged
R_eff  = blend of per-token rates weighted by (rho, c, w) mix assumptions
I      = C_usd × 1,000,000 / (R_eff × d × m)   # total input tokens
T_out  = rho × I                            # output tokens
T_tot  = I + T_out                          # total tokens
```

Where:
- `rho` = output:input ratio (how many output tokens per input token)
- `c` = cache-read fraction of input tokens
- `w` = cache-write fraction (Anthropic models only)
- `d` = `0.90` when the model was Auto-selected (GitHub's −10% discount)
- `m` = `1.10` if data-residency/FedRAMP surcharge applies (default: off)

`catalog.py` supplies the per-token rates (`r_in`, `r_cache`, `r_cwrite`, `r_out`) for each model. It is the **only file that changes when GitHub updates pricing**.

Because `rho` and `c` are unknown from the CSV alone, `estimator.py` uses **feature priors** keyed by SKU (see [Understanding the confidence band](#understanding-the-confidence-band)) and sweeps the range analytically to produce `total_tokens_low` and `total_tokens_high`.

### Phase 3 — Build outputs

Results are accumulated into five files and a terminal summary (see [Output artifacts](#step-3--run-the-estimate)).

---

## Files included

```
[zip root]/
├── pyproject.toml
├── tools/
│   ├── __init__.py
│   └── aic_token_estimator/
│       ├── __init__.py
│       ├── aic_tokens.py
│       ├── billing_api.py
│       ├── catalog.py
│       ├── estimator.py
│       └── requirements.txt
├── copilot_usage_reports/
│   └── sample-ai-usage.csv
```

**Do NOT include when distributing:**
- `.venv/` — each user creates their own
- `__pycache__/` — auto-generated
- `lantern-april-ai-credit-usage-report.csv` — Lantern internal data (in `copilot_usage_reports/`)
- `stress-test-17k-devs.csv` — internal stress-test fixture (300 MB)
- `token_estimate_reports/` — generated run outputs

---

## Per-model rates reference

All rates are **USD per 1,000,000 tokens**. Encoded in `catalog.py`.
Source: [Models and pricing for GitHub Copilot](https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing) (verified 2026-06-19).

| Model | input | cache-read | cache-write | output | long-ctx tier |
|---|---|---|---|---|---|
| GPT-5 mini | 0.25 | 0.025 | — | 2.00 | — |
| GPT-5.3-Codex | 1.75 | 0.175 | — | 14.00 | — |
| GPT-5.4 | 2.50 | 0.25 | — | 15.00 | >272K → 5.00 / 0.50 / 22.50 |
| GPT-5.4 mini | 0.75 | 0.075 | — | 4.50 | — |
| GPT-5.4 nano | 0.20 | 0.02 | — | 1.25 | — |
| GPT-5.5 | 5.00 | 0.50 | — | 30.00 | >272K → 10.0 / 1.00 / 45.00 |
| Claude Haiku 4.5 | 1.00 | 0.10 | 1.25 | 5.00 | — |
| Claude Sonnet 4 / 4.5 / 4.6 | 3.00 | 0.30 | 3.75 | 15.00 | — |
| Claude Opus 4.5–4.8 | 5.00 | 0.50 | 6.25 | 25.00 | — |
| Claude Fable 5 | 10.00 | 1.00 | 12.50 | 50.00 | — |
| Gemini 2.5 Pro | 1.25 | 0.125 | — | 10.00 | — |
| Gemini 3 Flash | 0.50 | 0.05 | — | 3.00 | — |
| Gemini 3.1 Pro | 2.00 | 0.20 | — | 12.00 | >200K → 4.00 / 0.40 / 18.00 |
| Gemini 3.5 Flash | 1.50 | 0.15 | — | 9.00 | — |
| Raptor mini (GitHub) | 0.25 | 0.025 | — | 2.00 | — |
| MAI-Code-1-Flash (MS) | 0.75 | 0.075 | — | 4.50 | — |

**Two accuracy rules baked into the tool:**
- **Anthropic cache-write is charged** (~1.25× input rate). Ignoring it under-counts Anthropic credits / over-counts tokens. `estimator.py` prices it correctly.
- **Long-context tiers** (GPT-5.x >272K, Gemini >200K) cost ~2× more per token. The tool uses the standard tier by default and notes the assumption.

---

## Auto model-selection: the −10% discount

When a user lets Copilot pick the model (**Auto** mode), GitHub applies a **10% discount
on the per-token rates** before computing credits. The tool handles this automatically:

- **Detection.** In the AI Usage Report the model appears as `Auto: <model>` (e.g.
  `Auto: Claude Sonnet 4.6`). The tool strips the prefix, resolves the real model's
  rates, and applies the discount.
- **Effect on inversion.** Because GitHub charged 10% *fewer* dollars for the same
  tokens, the **same credit count corresponds to ~11.1% more tokens** under Auto.
  `selftest` check `[3]` asserts exactly ×1/0.90 = ×1.1111.

> The `AUTO_DISCOUNT = 0.90` constant in `estimator.py` is a single, configurable value.
> Verify it against your current GitHub billing terms before locking the dashboard.

---

## Setup Instructions

### Prerequisites

- Python 3.10 or higher
  ```bash
  python3 --version   # must be 3.10+
  ```
- Your GitHub Copilot **AI Usage Report CSV or XLSX**, downloaded from:
  > Enterprise Settings → Billing → AI usage → Download report

  > **XLSX note:** The tool accepts `.xlsx` files directly and converts them internally
  > using Python's standard library (no Excel required). If the file is password-protected
  > (OLE2/encrypted), save an unprotected copy from Excel first:
  > File → Info → Protect Workbook → Encrypt with Password → clear the field → Save.

---

### Step 1 — First-time environment setup

Run once from the unzipped folder root:

```bash
# Create virtual environment
python3 -m venv tools/aic_token_estimator/.venv

# Activate (Mac / Linux)
source tools/aic_token_estimator/.venv/bin/activate

# Activate (Windows)
tools\aic_token_estimator\.venv\Scripts\activate

# Install the package
pip install -e .
```

**Subsequent sessions** — activate before each run (no reinstall needed):
```bash
cd /path/to/unzipped-folder
source tools/aic_token_estimator/.venv/bin/activate   # Mac/Linux
# then run the estimate command in Step 3
```

---

### Step 2 — Verify the setup (no data required)

```bash
python tools/aic_token_estimator/aic_tokens.py selftest
```

Expected output — all six lines must say `OK` or `PASS`:

```
[1] GPT-5 mini 20k/10k/3k -> 1.125 credits (expected 1.125)
[2] inversion with true mix -> 33,000 tokens (actual 33,000; err 0.00%)
[3] auto-select token uplift -> x1.1111 (expected x1.1111)
[4] band low<=exp<=high -> 150,262 <= 224,921 <= 417,853  OK
[5] bucket day/week/month -> 2026-06-03 / 2026-W23 / 2026-06  OK
[6] pivot conservation -> grand total 175 (expected 175)  OK

ALL PASS
```

If this fails, stop and contact Lantern before proceeding.

---

### Step 3 — Run the estimate

```bash
python tools/aic_token_estimator/aic_tokens.py estimate \
  --csv copilot_usage_reports/2026-april/lantern-april-ai-credit-usage-report.csv \
  --period month \
  --reports-dir token_estimate_reports \
  --report-stem token-estimate
```

`--period` controls the time bucket for the trend table and the model pivot:

| `--period` | Bucket key | Output subdir | Use for |
|---|---|---|---|
| `day` | `YYYY-MM-DD` | `YYYY-MM-DD` | Single-day or daily token usage by model |
| `week` | `YYYY-Www` (ISO, Monday-start) | `YYYY-MM` | Weekly token usage by model |
| `month` *(default)* | `YYYY-MM` | `YYYY-MM` | Monthly leadership roll-up |

> **Single-day data:** If your report covers only one day (common for mid-month snapshots),
> use `--period day`. The output subdir will be the exact date (`YYYY-MM-DD`) and — when no
> `--report-stem` is provided — the date is automatically appended to all output filenames
> (e.g. `token-estimate-2026-05-15.csv`) so each run's artifacts are self-describing.

Grand totals are printed to the terminal. Each run produces five files in `--reports-dir/<dated-subdir>/`:

| Artifact | Contents |
|---|---|
| `<subdir>/<stem>.csv` | Detailed per `user × <period> × model` rows (credits, cost, token split, band) |
| `<subdir>/<stem>-tokens-by-user.csv` | **Per-user totals** (all models combined) — one row per user, sorted by cost; use for chargeback |
| `<subdir>/<stem>-model-by-<period>.csv` | **Chart-ready pivot**: rows = period bucket, columns = model, cells = estimated tokens |
| `<subdir>/<stem>-model-by-<period>-usd.csv` | Same pivot in exact dollars (reconciles to the penny) |
| `<subdir>/<stem>-executive-summary.md` | Leadership snapshot: cost by model, top users, period trend, model-over-time matrix, and links to all CSVs |

The two pivot CSVs drop straight into Excel/Power BI to chart **weekly or daily token
usage broken down by model**. The detailed CSV answers **user-level token spending** next
to each user's AI credits.

> **Advanced options:**
> - `--out <path>` — write the detailed CSV to a specific path instead of `--reports-dir/<stem>.csv` (use when you want the CSV only, without pivot/summary artifacts, or to pipe to stdout by default)
> - `--summary-out <path>` — write the executive summary markdown to a specific path instead of `--reports-dir/<stem>-executive-summary.md`

---

### Step 3b — (Optional) Pull the report via the billing API instead of a manual download

If you would rather not download the CSV by hand, `fetch` calls the GitHub Enhanced
Billing **reports** API (`POST` to create, `GET` to poll until complete) and writes the
same CSV schema the estimator consumes:

```bash
export GITHUB_TOKEN=...   # enterprise admin / billing-manager token; never commit it
python tools/aic_token_estimator/aic_tokens.py fetch \
  --enterprise your-enterprise-slug \
  --out ~/Downloads/ai-usage-from-api.csv
# then feed it to estimate:
python tools/aic_token_estimator/aic_tokens.py estimate \
  --csv ~/Downloads/ai-usage-from-api.csv \
  --period month \
  --reports-dir token_estimate_reports \
  --report-stem token-estimate
```

> **The billing-reports API returns credits and dollars — not tokens.** It exposes exactly
> the same usage line items as the manual CSV download. Tokens are never returned by
> GitHub and are still estimated locally by `estimate`. The `fetch` step only removes the
> manual download; it does not change accuracy.

---

## Output: What the CSV columns mean

### Detailed CSV (`<stem>.csv`)

| Column | Description |
|---|---|
| `username` | GitHub username |
| `month` | Billing bucket. Header is `date` (daily), `week` (weekly), or `month` (monthly) depending on `--period` |
| `model` | Canonical model ID (e.g. `claude-sonnet-4.6`) |
| `credits` | AI credits charged (from GitHub report) |
| `cost_usd` | Exact cost in USD (`credits × $0.01`) |
| `est_input_tokens` | Estimated uncached input tokens |
| `est_cached_tokens` | Estimated cache-read tokens |
| `est_cache_write_tokens` | Estimated cache-write tokens (Anthropic only) |
| `est_output_tokens` | Estimated output tokens |
| `est_total_tokens` | Estimated total tokens |
| `total_tokens_low` | Lower bound of confidence band |
| `total_tokens_high` | Upper bound of confidence band |
| `unresolved_credits` | Credits where model was not recognized (see below) |

### Per-user totals CSV (`<stem>-tokens-by-user.csv`)

One row per user, all models combined, sorted by cost descending. Use this for chargeback and user-level spend reporting.

| Column | Description |
|---|---|
| `username` | GitHub username |
| `credits` | Total AI credits charged across all models |
| `cost_usd` | Exact total cost in USD (`credits × $0.01`) |
| `est_total_tokens` | Estimated total tokens across all models |
| `total_tokens_low` | Lower bound of confidence band (summed across models) |
| `total_tokens_high` | Upper bound of confidence band (summed across models) |

### Understanding the confidence band

The `total_tokens_low` and `total_tokens_high` columns reflect genuine uncertainty —
GitHub reports credits charged but not the per-turn token breakdown. The band is
computed by sweeping the output:input ratio (`ρ`) and cache fraction (`c`) across a
plausible range derived from feature priors:

| Feature / SKU | ρ (out:in) low–exp–high | c (cache frac) low–exp–high |
|---|---|---|
| `coding_agent_ai_credit` (agent/edit) | 0.04 – 0.12 – 0.30 | 0.40 – 0.65 – 0.85 |
| `copilot_ai_credit` (chat, review) | 0.10 – 0.25 – 0.50 | 0.20 – 0.45 – 0.70 |
| `default` (unknown SKU) | 0.05 – 0.20 – 0.45 | 0.30 – 0.55 – 0.80 |

- **`cost_usd` is exact** — credits × $0.01, no estimation involved
- **Token estimates** are accurate at the monthly aggregate level (single-digit % error);
  individual row error is wider but cancels across users and models at the reporting grain

### Executive summary structure (`<stem>-executive-summary.md`)

The markdown report is designed for leadership review and contains:

| Section | What it shows |
|---|---|
| **Report Artifacts** | Relative links to all four CSVs — click directly from GitHub or VS Code |
| **Leadership Snapshot** | Period, unique user count, total credits, total cost, total tokens, confidence band |
| **Cost Breakdown by Model** | All models ranked by cost with credit/token/share columns |
| **Top Cost Drivers by User** | Top 10 users by cost; link to per-user CSV for full list |
| **Trend table** | Per-period bucket: users active, cost, credits, tokens |
| **Token usage by model over time** | Matrix: periods (rows) × top-5 models (cols) + Other + Total |
| **Data quality & assumptions** | Row counts, resolved models, confidence caveats |
| **Recommended actions** | Actionable bullets based on top driver and unresolved credits |

---

## Improving accuracy with OTel / telemetry data

The confidence band (`total_tokens_low` / `total_tokens_high`) is intentionally wide
because the two key unknowns — **output:input ratio `ρ`** and **cache fraction `c`** —
are not available in the AI Usage Report. The tool uses conservative feature priors that
bracket reality for most workloads.

If you can supply observed values for `ρ` and `c` from telemetry, the band collapses
dramatically — from a typical ×2.5–3.5 spread to ±5–10%.

### What OTel data helps

Any telemetry source that records **actual token counts per Copilot interaction** can
calibrate the priors. Useful sources:

| Source | Data available | How to use |
|---|---|---|
| **GitHub Copilot metrics API** | `prompt_tokens`, `completion_tokens` aggregated per day/org/model | Compute `ρ = completion / prompt` per model per day; use as calibrated expected value |
| **VS Code extension OTel spans** | Per-request `promptTokenCount`, `completionTokenCount` if telemetry is enabled | Compute empirical `ρ` and `c` distributions; feed as `rho_ex` / `c_ex` |
| **Dynatrace / Datadog / Splunk** | If Copilot extension traces are being collected, token attributes appear as span fields | Query `avg(completionTokens) / avg(promptTokens)` per model |
| **GitHub audit log / webhooks** | Does NOT include token counts — credits only | Cannot calibrate directly |

### What to compute from telemetry

Two values calibrate the entire estimate:

```
ρ  =  avg(completion_tokens)  /  avg(prompt_tokens)    # output:input ratio
c  =  avg(cached_tokens)      /  avg(prompt_tokens)    # cache-read fraction
```

Compute these **separately per SKU** (agent vs. chat) and **per model** if your
telemetry is granular enough — the mix parameters differ significantly between them.

### How to narrow the band

Once you have empirical `ρ` and `c` values, replace the prior ranges in `estimator.py`
with tight calibrated ranges. For example, if telemetry shows `ρ ≈ 0.18` and `c ≈ 0.52`
for Claude Sonnet in chat mode:

```python
# In estimator.py — replace the copilot_ai_credit prior:
"copilot_ai_credit": Mix(
    rho_lo=0.15, rho_ex=0.18, rho_hi=0.21,   # ±3 percentage points around observed
    c_lo=0.48,   c_ex=0.52,  c_hi=0.56,
    w_ex=0.03,
),
```

This reduces the band from a ×3 spread to ±10–15% without changing any other code.

### Expected improvement

| Scenario | Typical band width | Token accuracy at aggregate |
|---|---|---|
| No telemetry (current) | ×2.5–3.5 (e.g. 2.6B–10.6B) | ~±8% at monthly grain |
| OTel-calibrated ρ and c | ×1.15–1.25 (e.g. 3.8B–4.7B) | ~±3–5% at monthly grain |
| Actual token counts from API | Band collapses to zero | Exact (no estimation needed) |

> If the GitHub Copilot metrics API ever exposes per-model token totals at the
> enterprise level, that data should be used directly rather than inverted from credits.
> Contact Lantern to update the pipeline to consume that feed.

---

## Accuracy overview & known limitations

| Item | Confidence | Note |
|---|---|---|
| `1 credit = $0.01` | **High** | GitHub docs, ×3 pages |
| Per-model rates | **High** | Live pricing page, verified 2026-06-19; refresh monthly |
| Forward formula / `selftest` | **High** | Reproduces GitHub's own worked example exactly |
| `cost_usd` in output | **Exact** | `credits × $0.01`; only the token *split* is estimated |
| Per-model decomposition | **High** | Comes straight from the AI Usage Report |
| Token split per turn | **Medium** (~±30–60%) | Depends on output:input mix; narrows with aggregate cancellation |
| Aggregate monthly tokens | **Medium-High** (~±8%) | Errors cancel at the user × model × month reporting grain |
| Long-context tier detection | **Low without a signal** | CSV lacks context size; standard tier assumed unless known |
| Code-review model | **Low** | GitHub does not disclose the model; treated as `default` mix |

**Limitations to disclose:**
- IDE **code completions** are *not billed in AI Credits* and carry no tokens — they are out of scope.
- The CSV gives daily aggregates, not per-session detail. Per-session tokens are not available without additional tooling.
- Pricing catalog (`catalog.py`) reflects GitHub rates as of June 2026. If GitHub changes pricing, Lantern will issue an updated `catalog.py`.

---

## Reconciling credits to dollars

Sum `cost_usd` in the output CSV and compare to your billing portal's gross amount —
they should tie to the penny. Credits are exact; only the *token split* is estimated.

> **Use gross credits, not net.** Net credits reflect what you are *billed after the
> included pool*; gross reflects what was actually *consumed*. Tokens correlate with
> consumption, so always invert `aic_quantity` / `gross_amount`, never `net_amount`.
> (Within the pool, `net_amount` is often `$0` even though millions of tokens were used.)

---

## Troubleshooting

### `UNRESOLVED credits` appears in the terminal summary

A model name in the CSV was not recognized by the pricing catalog. This is usually a
newly released model. Record the exact model name and report it to Lantern to update
`catalog.py`.

### `ModuleNotFoundError: No module named 'tools'`

The virtual environment is not activated, or `pip install -e .` was not run.
Repeat Steps 1 and 2.

### `python: command not found`

Use `python3` instead of `python`, or ensure Python 3.10+ is on your PATH.

### The selftest fails

Do not proceed. Contact Lantern with the full error output.

---

## Important notes

- **No internet connection required** — all code uses Python standard library only
- **Download a fresh CSV monthly** — the report covers at most 31 days; run separately for each period
- **Pricing catalog** (`catalog.py`) reflects GitHub's official rates as of June 2026.
  If GitHub changes pricing, Lantern will issue an updated `catalog.py`

---

## Sources

- [Usage-based billing for organizations and enterprises](https://docs.github.com/en/copilot/concepts/billing/usage-based-billing-for-organizations-and-enterprises)
- [Models and pricing for GitHub Copilot](https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing)
- [Billing reports reference (AI usage report fields)](https://docs.github.com/en/billing/reference/billing-reports)
- [REST API: Enhanced billing usage](https://docs.github.com/en/rest/billing/enhanced-billing)
- [REST API: Copilot usage metrics](https://docs.github.com/en/rest/copilot/copilot-usage-metrics)
- [Understanding AI Credits, Token Usage, and the Real Cost of GitHub Copilot](https://medium.com/@anil.goyal0057/understanding-ai-credits-token-usage-and-the-real-cost-of-github-copilot-6a1c319a8f6a)
