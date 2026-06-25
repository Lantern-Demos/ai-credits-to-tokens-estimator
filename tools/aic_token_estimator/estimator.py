"""
estimator.py — Deterministic AI Credits -> token estimation.

Implements the layered reverse-estimation method:

  Step 1  Decompose AI Credits BY MODEL (done by the caller from the CSV).
  Step 2  Per-model inversion of credits -> USD -> tokens using a mix model.
  Step 3  Mix parameters (rho, c, w) come from OTel calibration when available,
          otherwise from feature priors (see MIX_PRIORS).
  Step 4  Confidence band by sweeping (rho, c) across the prior/calibrated range.

Math (all rates are USD per 1,000,000 tokens):

  Let
    C_usd = credits * USD_PER_CREDIT                 # USD GitHub computed
    I     = total input tokens (uncached + cached + cache-write)
    rho   = output:input ratio        (T_out = rho * I)
    c     = cache-read fraction of I
    w     = cache-write fraction of I  (Anthropic only; 0 elsewhere)
    d     = 0.90 if auto-selected else 1.00          # GitHub auto-select -10%
    m     = 1.10 if data-residency/FedRAMP else 1.00 # +10% surcharge

  GitHub's forward cost:
    C_usd = (I / 1e6) * R * d * m
      where R = (1-c-w)*r_in + c*r_cache + w*r_cwrite + rho*r_out

  Invert for I:
    I       = C_usd * 1e6 / (R * d * m)
    T_out   = rho * I
    T_total = I * (1 + rho)

Because I is proportional to 1/R, the token estimate is largest when R is
smallest (low rho, high cache) and smallest when R is largest (high rho, low
cache). We exploit this to produce analytic low/high bounds without sampling.

What the token total represents
-------------------------------
The estimate is inverted from DOLLARS, so it is a *cost-weighted* (billable)
token figure. GitHub bills cache-read tokens at ~0.1x the uncached-input rate,
yet a raw token-throughput report (e.g. the GitHub Copilot metrics export)
counts those cached tokens at 100%. For cache-heavy agentic usage the throughput
view can read several times higher than this dollar-inverted view — both are
valid, they just measure different things. The cache fraction `c` is the lever
that moves between them; supply a measured `c` (see calibrate_mix) to make the
estimate reproduce a throughput figure. IDE code completions are not billed in
AI credits, so they carry no credits to invert and are out of scope here.

Source for constants and the forward formula:
  https://docs.github.com/en/copilot/concepts/billing/usage-based-billing-for-organizations-and-enterprises
  https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing
"""

from __future__ import annotations

from dataclasses import dataclass

from tools.aic_token_estimator.catalog import USD_PER_CREDIT, ModelPricing, get_pricing

AUTO_DISCOUNT = 0.90        # auto model selection: -10% on per-token rates
RESIDENCY_SURCHARGE = 1.10  # data-residency / FedRAMP: +10% on all requests


@dataclass(frozen=True)
class Mix:
    """A (rho, c, w) operating point with low/expected/high ranges."""
    rho_lo: float
    rho_ex: float
    rho_hi: float
    c_lo: float
    c_ex: float
    c_hi: float
    w_ex: float = 0.0  # cache-write fraction (Anthropic); same across band


# Empirical feature priors. rho = output:input ratio; c = cache-read fraction
# of total input tokens; w = cache-write fraction.
#
# Why the cache fractions are high (and why this is the dominant lever on the
# token TOTAL): GitHub bills cache-read tokens at ~0.1x the uncached-input rate,
# but a raw token-throughput report still counts every cached token at 100%.
# Agentic sessions re-send the whole working context on each tool-call iteration,
# so cache reads dominate input volume — commonly 0.85-0.97 of all input tokens.
# Because this estimator inverts DOLLARS (where cache reads are nearly free) back
# into face-value tokens, an under-stated cache fraction under-counts throughput
# by multiples. These priors are set so the expected point and the high band
# bracket realistic agentic throughput; pass a measured value via calibrate_mix()
# / the `--cache-fraction` CLI flag to collapse the band and reconcile exactly.
# Ranges stay wide until per-tenant telemetry narrows them.
MIX_PRIORS: dict[str, Mix] = {
    # Agentic coding (IDE agent/edit mode, coding agent): huge cached context
    # re-read every iteration, modest generated output.
    "coding_agent_ai_credit": Mix(0.04, 0.12, 0.30, 0.55, 0.85, 0.97, w_ex=0.05),
    "agent":                  Mix(0.04, 0.12, 0.30, 0.55, 0.85, 0.97, w_ex=0.05),
    # Chat / ask / code review / spaces: smaller context, longer explanations.
    "copilot_ai_credit":      Mix(0.10, 0.25, 0.50, 0.30, 0.55, 0.80, w_ex=0.03),
    "spark_ai_credit":        Mix(0.10, 0.25, 0.50, 0.30, 0.55, 0.80, w_ex=0.03),
    # Generic fallback when SKU/feature is unknown.
    "default":                Mix(0.05, 0.20, 0.45, 0.40, 0.70, 0.90, w_ex=0.03),
}


def get_mix(sku: str | None) -> Mix:
    if sku and sku in MIX_PRIORS:
        return MIX_PRIORS[sku]
    return MIX_PRIORS["default"]


def calibrate_mix(sku: str | None, *, cache_fraction: float | None = None,
                  output_ratio: float | None = None,
                  cache_write_fraction: float | None = None) -> Mix:
    """Return the SKU prior with measured dimensions pinned to known values.

    Supplying `cache_fraction` (c), `output_ratio` (rho), and/or
    `cache_write_fraction` (w) collapses that dimension's low/expected/high to the
    measured value while keeping the prior range for whatever is still unknown.
    Use this to reconcile the estimate with an observed token-throughput figure
    (e.g. the GitHub Copilot metrics report): a higher cache fraction yields more
    face-value tokens for the same credits. Pin `w` as well for ultra-cache-heavy
    tenants where c exceeds the prior's default cache-write floor (otherwise
    `_invert` caps c at 1 - w and recovery is incomplete).
    """
    base = get_mix(sku)
    rho_lo, rho_ex, rho_hi = base.rho_lo, base.rho_ex, base.rho_hi
    c_lo, c_ex, c_hi = base.c_lo, base.c_ex, base.c_hi
    w = base.w_ex
    if output_ratio is not None:
        rho_lo = rho_ex = rho_hi = output_ratio
    if cache_fraction is not None:
        c_lo = c_ex = c_hi = cache_fraction
    if cache_write_fraction is not None:
        w = cache_write_fraction
    return Mix(rho_lo, rho_ex, rho_hi, c_lo, c_ex, c_hi, w_ex=w)


def _rates(p: ModelPricing, long_context: bool) -> tuple[float, float, float, float]:
    """Return (r_in, r_cache, r_cwrite, r_out) per 1M tokens for the active tier."""
    if long_context and p.long is not None:
        r_in, r_cache, r_out = p.long.inp, p.long.cache, p.long.out
    else:
        r_in, r_cache, r_out = p.inp, p.cache, p.out
    r_cwrite = p.cwrite if p.cwrite is not None else r_in
    return r_in, r_cache, r_cwrite, r_out


def _invert(credits: float, p: ModelPricing, rho: float, c: float, w: float,
            auto: bool, residency: bool, long_context: bool) -> dict:
    """Invert a single (rho, c, w) operating point -> token counts."""
    r_in, r_cache, r_cwrite, r_out = _rates(p, long_context)
    # Anthropic-only cache-write; force 0 for providers without a cwrite rate.
    if p.cwrite is None:
        w = 0.0
    c = min(c, 1.0 - w)
    r_eff = (1 - c - w) * r_in + c * r_cache + w * r_cwrite + rho * r_out  # USD/1M
    d = AUTO_DISCOUNT if auto else 1.0
    m = RESIDENCY_SURCHARGE if residency else 1.0
    c_usd = credits * USD_PER_CREDIT
    if r_eff <= 0:
        return {"input": 0.0, "cached": 0.0, "cache_write": 0.0,
                "output": 0.0, "total": 0.0}
    total_input = c_usd * 1_000_000 / (r_eff * d * m)  # = I
    t_out = rho * total_input
    return {
        "input": total_input * (1 - c - w),  # uncached input tokens
        "cached": total_input * c,
        "cache_write": total_input * w,
        "output": t_out,
        "total": total_input + t_out,
    }


@dataclass(frozen=True)
class TokenEstimate:
    model: str
    credits: float
    cost_usd: float
    auto: bool
    resolved: bool
    # expected point estimate
    input_tokens: float
    cached_tokens: float
    cache_write_tokens: float
    output_tokens: float
    total_tokens: float
    # confidence band on TOTAL tokens
    total_low: float
    total_high: float


def estimate_tokens(credits: float, model: str, sku: str | None = None,
                    *, auto: bool | None = None, residency: bool = False,
                    long_context: bool = False, mix: Mix | None = None) -> TokenEstimate:
    """Estimate tokens for a single (credits, model) datum.

    `auto` overrides the auto-select flag; when None it is inferred from an
    'Auto:' model-name prefix. `mix` overrides the feature prior (used to inject
    OTel-calibrated parameters).
    """
    p = get_pricing(model)
    inferred_auto = model.strip().lower().startswith("auto:")
    is_auto = inferred_auto if auto is None else auto

    if p is None:
        return TokenEstimate(model, credits, credits * USD_PER_CREDIT, is_auto,
                             False, 0, 0, 0, 0, 0, 0, 0)

    m = mix or get_mix(sku)
    # Expected point.
    exp = _invert(credits, p, m.rho_ex, m.c_ex, m.w_ex, is_auto, residency, long_context)
    # Max-tokens corner: smallest R -> low rho, high cache.
    hi = _invert(credits, p, m.rho_lo, m.c_hi, m.w_ex, is_auto, residency, long_context)
    # Min-tokens corner: largest R -> high rho, low cache.
    lo = _invert(credits, p, m.rho_hi, m.c_lo, m.w_ex, is_auto, residency, long_context)

    return TokenEstimate(
        model=p.id, credits=credits, cost_usd=credits * USD_PER_CREDIT,
        auto=is_auto, resolved=True,
        input_tokens=exp["input"], cached_tokens=exp["cached"],
        cache_write_tokens=exp["cache_write"], output_tokens=exp["output"],
        total_tokens=exp["total"],
        total_low=lo["total"], total_high=hi["total"],
    )
