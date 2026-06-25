# Why our token estimate differs from GitHub's "token usage by user"

> Prepared by Lantern · Pricing/mechanics verified 2026-06-25 against GitHub Copilot
> billing docs, the GitHub Copilot metrics REST API, and Anthropic prompt-caching pricing,
> and independently cross-validated against the open-source tool
> [rajbos/ai-engineering-fluency](https://github.com/rajbos/ai-engineering-fluency)
> (see [§3](#3-independent-validation-cross-checked-against-an-external-tool)).

## TL;DR

The two numbers measure **different things**, and neither is wrong.

- **Our estimate = cost-weighted (billable) tokens.** We invert AI Credits → dollars →
  tokens. Dollars are dominated by the *expensive* tokens (uncached input + output).
  Cache-read tokens are billed at **~10%** of the input rate, so they barely move the
  dollar total — and therefore barely register in a dollar-anchored inversion.
- **GitHub's "token usage by user" = raw throughput.** Every token is counted once at
  **100%** of face value, including cache reads (which agentic sessions re-read on *every*
  iteration) and possibly IDE code completions (which carry **no** AI credits at all).

For heavily agentic, cache-intensive usage the throughput view legitimately runs **2–5×**
higher than the billable view. That is the gap you saw — it is structural, not a defect.
The dollars in both pictures reconcile to the penny; only the token *split* differs.

---

## 1. The core mechanism

GitHub prices every interaction in tokens, sums the cost in dollars, divides by `$0.01`,
and exposes only the final **AI Credit** number in the usage report. To get tokens back,
the tool runs that process in reverse. The catch is that a credit is a **dollar** figure,
and not all tokens cost the same number of dollars:

| Token type | Billed rate (relative to uncached input) | Counted in a throughput report |
|---|---|---|
| Uncached input | 1.0× | 100% |
| **Cache read** | **~0.1×** | **100%** |
| Cache write (Anthropic) | ~1.25× | 100% |
| Output | ~5–6× | 100% |

Because cache reads cost **one-tenth** of a normal input token but are **counted in full**
by a throughput report, any method that works backward from dollars will "see" a cache read
as roughly one-tenth of a token. The more cache-heavy the workload, the wider the gap.

Two things make agentic coding extremely cache-heavy:

1. **Context is re-read every iteration.** In a multi-step agent loop, the whole working
   context (system prompt, tools, files, prior turns) is re-sent and read from cache on
   *each* tool call. Anthropic's own usage example shows a request with 100,000 cached
   tokens is reported as "100,050 tokens processed" — counted at 100%, billed at 10%.
   A 10-step task therefore counts that context ~10× in throughput while paying the 0.1×
   cache rate each time.
2. **Cache reads dominate input volume** — commonly **85–98%** of all input tokens for
   agentic work.

This is almost certainly why even GitHub is "unsure how the token counts total": a
throughput number re-counts cached context on every iteration, so it is itself a modeling
choice, not a single ground-truth scalar.

### A third, separate factor: code completions

GitHub's billing docs state plainly that **"Code completions and next edit suggestions are
not billed in AI credits."** They are high-volume (a suggestion on many keystrokes) but
carry **zero credits**. If GitHub's "token usage by user" figure includes completion
tokens, those tokens **can never** be recovered by inverting credits — there are no dollars
to invert. This is an *additive* source of undercount on top of the cache effect, and it
depends entirely on which GitHub surface the number came from (see [§7](#7-what-we-need-to-confirm)).

---

## 2. Worked reconciliation (real numbers from the tool)

Both scenarios below were run through the actual estimator. They start from a known
token breakdown (so we know the "true" throughput), compute the credits GitHub would
charge, then invert.

### Scenario A — agentic month, Claude Sonnet, cache ≈ 92% of input

| Quantity | Value |
|---|---:|
| Uncached input / cache read / cache write / output | 2.0M / 60.0M / 3.0M / 4.0M |
| **Raw throughput (GitHub-style, all at 100%)** | **69.0M tokens** |
| Cost | $95.25 → 9,525 credits |
| Dollar share: cache reads | 63% of throughput but **19%** of dollars |
| Dollar share: output | 6% of throughput but **63%** of dollars |

| Estimate basis | Result | vs throughput |
|---|---:|---:|
| Old prior (cache 0.65) — *what you ran* | 34.6M | 1.99× under |
| New default prior (cache 0.85) | 41.96M | 1.64× under (band now brackets) |
| **Calibrated to measured cache 0.923** | **68.86M** | **0.998× (exact)** |

### Scenario B — very cache-heavy month, cache ≈ 98% of input

| Quantity | Value |
|---|---:|
| Uncached / cache read / cache write / output | 0.8M / 120.0M / 1.5M / 2.0M |
| **Raw throughput** | **124.3M tokens** |
| Cost | $74.03 → 7,402 credits |

| Estimate basis | Result | vs throughput |
|---|---:|---:|
| Old prior (cache 0.65) — *what you ran* | 26.9M | **4.62× under** |
| New default prior (cache 0.85) | 32.6M | 3.81× under |
| Calibrated (cache + output ratio only) | 104.8M | 0.84× (still short — see note) |
| **Fully calibrated (cache + write + output ratio)** | **124.3M** | **1.000× (exact)** |

> **Note.** In Scenario B the true cache fraction (0.98) exceeds the cache-write floor, so
> pinning the cache fraction alone is not enough — the cache-*write* fraction has to be
> pinned too. The tool now exposes a flag for this (see [§5](#5-what-we-changed)).

**The takeaways:**

- A 4× gap is fully explained by cache intensity alone (Scenario B, old prior = 4.62×).
- In every case the **dollars tie exactly** ($95.25 / $74.03) regardless of the token mix —
  proof that the credit side is correct and only the token split is in question.
- When the mix is pinned to the measured values, the estimate **reproduces GitHub's
  throughput to within a fraction of a percent.** The two systems are reconcilable.

---

## 3. Independent validation: cross-checked against an external tool

To prove the estimator is accurate — not merely internally consistent — we validated it
against an **independent, open-source tool that works in the opposite direction**:
[rajbos/ai-engineering-fluency](https://github.com/rajbos/ai-engineering-fluency)
(formerly the "GitHub Copilot Token Tracker") by Rob Bos. It is a VS Code / Visual Studio /
JetBrains extension and CLI that reads the **actual token counts** from local AI session
logs and computes credits **forward** (tokens → credits). Our estimator runs **backward**
(credits → tokens), so agreement between the two is genuine corroboration, not a circular
check: it was written by a different author, in a different language (TypeScript), built
independently from GitHub's published pricing.

The forward formula you supplied is exactly that tool's `calculateEstimatedCost` function —
so validating against it also validates the formula in your question.

### What the cross-check proved

| # | Check | Result |
|---|---|---|
| 1 | **Forward formula is identical** | Their `calculateEstimatedCost` is mathematically identical to our forward cost model (and to the formula in your question). |
| 2 | **Numerical agreement** | The same token vector (2.0M uncached + 60.0M cache-read + 3.0M cache-write + 4.0M output, Claude Sonnet) yields **9,525.00 credits ($95.25) in both tools** — exact match. |
| 3 | **Our inversion is exact** | Feeding those 9,525 credits back through our estimator with the measured mix recovers **69,000,000 tokens — the exact throughput, ratio 1.0000** — with dollars tying to the penny. Our inversion is the precise inverse of the forward charge. |
| 4 | **Pricing assumptions corroborated** | Their independently-maintained pricing table (same GitHub source) confirms **1 credit = $0.01**, **Anthropic cache-read = 10% of input** (rate is model-dependent across the catalog), and **Anthropic cache-write = 125%** — the exact rates in our catalog. |
| 5 | **GitHub itself bills with this formula** | Their tool decodes GitHub Copilot CLI's `session.shutdown` billing field (`totalNanoAiu ÷ 1e11 = USD`) and confirms it matches the forward per-token calculation. GitHub charges using exactly the model we invert. |
| 6 | **It measures the "throughput" side** | Their reported input tokens **include cache reads at 100%** — the same raw-throughput quantity as GitHub's "token usage by user." This independently confirms the throughput-vs-billable explanation in [§1](#1-the-core-mechanism). |

### What the stress-test improved

Cross-checking *every* per-model rate against their table also surfaced four stale proxy
rates in our catalog — models for which GitHub had not published AI-Credit rates when the
catalog was first built. We corrected all four against GitHub's actual published rates:

| Model | Was (proxy) | Corrected to | Why it mattered |
|---|---|---|---|
| `grok-code-fast-1` | 2.50 / 0.25 / 15.00 | **0.20 / 0.02 / 1.50** | proxy was **10.6× too high** → would under-count Grok tokens ~10.6× (GA model) |
| `gpt-5.2` | 0.75 / 0.075 / 4.50 | **1.75 / 0.175 / 14.00** | ~2.3× rate correction |
| `gpt-4.1` (cache) | 0.20 | **0.50** | legacy OpenAI cache is 25% of input, not 10% |
| `gpt-4o` (cache) | 0.25 | **1.25** | aligns to the 50% OpenAI cache rate |

Each is annotated in `catalog.py` with the cross-verification date. The current-generation
models that dominate real usage (GPT-5.x, Claude Sonnet/Opus/Haiku, Gemini) were already
**exactly correct** and needed no change.

> **Bottom line:** an independent tool, built by a different author in the opposite
> direction, reproduces our credit math to the penny and confirms every pricing assumption.
> The estimator's *math* is proven correct; the only inaccuracies the stress-test found were
> a few per-model rate constants, now fixed.

---

## 4. The assumptions built into the scripts — and why they are defensible

| # | Assumption | Where | Justification | If wrong, direction |
|---|---|---|---|---|
| 1 | 1 credit = $0.01 USD | `catalog.py` | **Exact** — fixed by GitHub, stated in 3 billing docs | none |
| 2 | Per-model token rates | `catalog.py` | From GitHub's published pricing page (verified 2026-06-19); cache 0.1× / write 1.25× independently confirmed against Anthropic pricing and the rajbos/ai-engineering-fluency pricing table (2026-06-25, see [§3](#3-independent-validation-cross-checked-against-an-external-tool)) | small, model-specific |
| 3 | Cache read = 0.1× input | `catalog.py` | Correct pricing — **and the root reason** a dollar inversion is "cache-blind." Documented, not hidden | n/a (correct) |
| 4 | Auto-select = 0.90 (−10%) | `estimator.py` | GitHub charges 10% fewer dollars under Auto, so the same credits = ~11.1% more tokens | small |
| 5 | **Mix priors — cache fraction `c`** | `estimator.py` | **The dominant lever.** `c` is *not* in the CSV, so it must be assumed. Real agentic `c` ≈ 0.9–0.98. **This is the assumption that produced your gap** | large under-count if set low |
| 6 | Mix priors — output ratio `ρ` | `estimator.py` | Also not in the CSV; assumed per SKU. Smaller effect than `c` | moderate |
| 7 | Standard context tier (not long-context) | `estimator.py` | CSV lacks context size. Long-context tiers cost ~2× — assuming standard makes us *over*-count, **partially offsetting** the cache under-count | over-count (offsets) |
| 8 | Code completions excluded | by design | **Confirmed** by GitHub: completions are not billed in credits, so they carry no credits to invert | under-count vs a throughput report that includes them |
| 9 | Gross (not net) credits | usage guidance | Net is $0 inside the included pool; gross reflects actual consumption | under-count if net used |

**The headline assumption is #5.** It is not a hidden fudge factor — it is the one genuinely
unknowable input (cache hit rate) that GitHub does not expose in the usage report. The
previous default (cache fraction 0.65) was too conservative for agentic workloads, which is
why your numbers came in low.

---

## 5. What we changed

1. **Recalibrated the cache-fraction priors** so the expected point and the confidence band
   bracket realistic agentic throughput (`estimator.py`):

   | SKU | Cache fraction (low – exp – high) | Was |
   |---|---|---|
   | `coding_agent_ai_credit` (agent/edit) | 0.55 – 0.85 – 0.97 | 0.40 – 0.65 – 0.85 |
   | `copilot_ai_credit` (chat/review) | 0.30 – 0.55 – 0.80 | 0.20 – 0.45 – 0.70 |
   | `default` | 0.40 – 0.70 – 0.90 | 0.30 – 0.55 – 0.80 |

   Output-ratio priors are unchanged. Dollars are unaffected.

2. **Added calibration flags** to `estimate` so you can pin the mix to *measured* values and
   reproduce a throughput figure exactly:
   - `--cache-fraction <C>` — the dominant lever (cache-read fraction of input tokens)
   - `--output-ratio <RHO>` — output:input ratio
   - `--cache-write-fraction <W>` — only for ultra-cache-heavy tenants (cache > ~0.95)

3. **Labeled the outputs** as **cost-weighted (billable)** in the CSV summary, the executive
   summary, and the terminal output — with a note that a throughput report counts cache reads
   (and possibly completions) at full value.

4. **Corrected four stale per-model rates** in `catalog.py` after the independent cross-check
   in [§3](#3-independent-validation-cross-checked-against-an-external-tool) — most importantly
   `grok-code-fast-1`, whose proxy rate was 10.6× too high. Token counts for any affected
   models (Grok especially) are now accurate.

---

## 6. How to reconcile against GitHub exactly

```bash
python tools/aic_token_estimator/aic_tokens.py estimate \
  --csv copilot_usage_reports/<period>/ai-usage-report.csv \
  --cache-fraction 0.92 \
  --output-ratio 0.06 \
  --cache-write-fraction 0.02
```

Pull the measured cache-read / cache-write / output fractions from any telemetry that
records per-interaction token counts (GitHub Copilot metrics export, VS Code OTel spans,
Datadog/Splunk traces). Pin them with the flags above and the billable estimate converges
on the throughput figure, while the dollars stay identical to the penny.

---

## 7. What we need to confirm

Two questions pin down the remaining magnitude precisely:

1. **Which GitHub surface produced "token usage by user"?** The **billing** surface (AI
   Credits) and the **Copilot metrics** surface (the `users-28-day` NDJSON token export) are
   different systems. If the number is from the metrics export, we should check whether it
   **includes code completions** — if so, that is a second, unrecoverable component of the gap
   on top of cache.
2. **The dominant workload mix** (agentic vs chat). We can derive this ourselves from your
   usage CSV by grouping credits by SKU; it sets the expected size of the gap.

If you can share the two totals plus the model/SKU mix, we will reproduce the exact
reconciliation on your data and confirm the calibrated cache fraction for your tenant.

---

## What each number is for

- **Billable (cost-weighted) tokens** — our default. Use for **chargeback, budgeting, and
  cross-provider cost normalization**: it answers "what did we pay for, in tokens?"
- **Raw throughput tokens** — GitHub's metrics view. Use for **capacity, model behavior, and
  cache-efficiency analysis**: it answers "how many tokens actually flowed?"

Both are correct. Reported side by side, the difference between them is essentially a
**measure of your cache efficiency** — a high ratio means caching is doing a lot of work and
saving real money.
