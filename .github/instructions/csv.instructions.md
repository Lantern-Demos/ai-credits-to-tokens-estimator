---
applyTo: "**/*.csv"
---

## Input: AI Usage Report CSV

Source: GitHub Enterprise Settings → Billing → AI usage → Download report.

The report schema has changed over time. `aic_tokens.py` resolves each field via
prioritized name lists — always use the `_pick(row, COLS)` helper when reading a
field, never access `row["column"]` directly.

| Field | Recognized column names (in priority order) |
|---|---|
| AI credits | `aic_quantity`, `quantity`, `gross_quantity`, `grossQuantity` |
| Model | `model` |
| Username | `username`, `user` |
| Date | `date` |
| SKU/feature | `sku` |
| Organization | `organization`, `org` |
| Cost center | `cost_center_name`, `cost_center` |

**Always use gross credits (`aic_quantity`), never `net_amount`.** Net credits are
`$0` for usage within the included seat pool even though tokens were consumed.

**Model names include an `Auto:` prefix** when the user let Copilot select the
model (e.g., `Auto: Claude Sonnet 4.6`). The estimator strips this prefix and
applies the `AUTO_DISCOUNT = 0.90` rate adjustment automatically.

The sample input file for testing is:
```
copilot_usage_reports/sample-ai-usage.csv
```

## Output: estimate CSV

Columns written by `aic_tokens.py estimate`:

| Column | Description |
|---|---|
| `username` | GitHub username |
| `month` / `date` / `week` | Period bucket (header name varies with `--period`) |
| `model` | Canonical model ID (e.g. `claude-sonnet-4.6`) |
| `credits` | AI credits charged |
| `cost_usd` | Exact cost (`credits × $0.01`) |
| `est_input_tokens` | Estimated uncached input tokens |
| `est_cached_tokens` | Estimated cache-read tokens |
| `est_cache_write_tokens` | Estimated cache-write tokens (Anthropic only) |
| `est_output_tokens` | Estimated output tokens |
| `est_total_tokens` | Estimated total tokens (expected point) |
| `total_tokens_low` | Lower bound of confidence band |
| `total_tokens_high` | Upper bound of confidence band |
| `unresolved_credits` | Credits where the model was not in the pricing catalog |

Output CSVs in `token_estimate_reports/<YYYY-MM>/` are **generated artifacts** — do not
hand-edit them. Re-run `aic_tokens.py estimate` to regenerate.
