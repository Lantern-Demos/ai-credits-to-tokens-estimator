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


# Empirical feature priors. rho = output:input ratio; c = cache-read fraction.
# Ranges are intentionally wide so the confidence band brackets reality until
# OTel calibration narrows them. Keyed by normalized SKU/feature.
MIX_PRIORS: dict[str, Mix] = {
    # Agentic coding (IDE agent/edit mode, coding agent): huge cached context,
    # modest generated output.
    "coding_agent_ai_credit": Mix(0.04, 0.12, 0.30, 0.40, 0.65, 0.85, w_ex=0.05),
    "agent":                  Mix(0.04, 0.12, 0.30, 0.40, 0.65, 0.85, w_ex=0.05),
    # Chat / ask / code review / spaces: smaller context, longer explanations.
    "copilot_ai_credit":      Mix(0.10, 0.25, 0.50, 0.20, 0.45, 0.70, w_ex=0.03),
    "spark_ai_credit":        Mix(0.10, 0.25, 0.50, 0.20, 0.45, 0.70, w_ex=0.03),
    # Generic fallback when SKU/feature is unknown.
    "default":                Mix(0.05, 0.20, 0.45, 0.30, 0.55, 0.80, w_ex=0.03),
}


def get_mix(sku: str | None) -> Mix:
    if sku and sku in MIX_PRIORS:
        return MIX_PRIORS[sku]
    return MIX_PRIORS["default"]


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
