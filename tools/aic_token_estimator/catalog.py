"""
catalog.py — GitHub Copilot per-model token pricing catalog.

All rates are USD per 1,000,000 tokens, taken from the official GitHub Copilot
"Models and pricing" reference (verified 2026-06-19):
https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing

Pricing columns:
  in     = input (uncached) tokens
  cache  = cached-input (cache-read) tokens
  cwrite = cache-write tokens (Anthropic only; None for others)
  out    = output tokens
  long   = optional long-context tier overrides {threshold_tokens, in, cache, out}

The constant 1 AI Credit = $0.01 USD is fixed by GitHub and is the only
credit<->USD conversion used anywhere in this toolkit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

USD_PER_CREDIT = 0.01  # 1 AI Credit = $0.01 USD (GitHub, fixed)


@dataclass(frozen=True)
class LongTier:
    threshold_tokens: int
    inp: float
    cache: float
    out: float


@dataclass(frozen=True)
class ModelPricing:
    id: str
    provider: str  # openai | anthropic | google | github | microsoft | xai
    inp: float          # USD / 1M input (uncached) tokens
    cache: float        # USD / 1M cache-read tokens
    out: float          # USD / 1M output tokens
    cwrite: float | None = None   # USD / 1M cache-write tokens (Anthropic only)
    long: LongTier | None = None  # optional long-context overrides
    aliases: tuple[str, ...] = field(default_factory=tuple)


# ── Catalog (per 1M tokens, USD) ────────────────────────────────────────────
# Source: docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing
_MODELS: list[ModelPricing] = [
    # ── OpenAI ──────────────────────────────────────────────────────────────
    ModelPricing("gpt-5-mini", "openai", 0.25, 0.025, 2.00,
                 aliases=("gpt-5 mini",)),
    ModelPricing("gpt-5.3-codex", "openai", 1.75, 0.175, 14.00,
                 aliases=("gpt-5.3 codex", "codex")),
    ModelPricing("gpt-5.4", "openai", 2.50, 0.25, 15.00,
                 long=LongTier(272_000, 5.00, 0.50, 22.50)),
    ModelPricing("gpt-5.4-mini", "openai", 0.75, 0.075, 4.50,
                 aliases=("gpt-5.4 mini",)),
    ModelPricing("gpt-5.4-nano", "openai", 0.20, 0.02, 1.25,
                 aliases=("gpt-5.4 nano", "gpt-5 nano", "gpt-5.4-nano")),
    ModelPricing("gpt-5.5", "openai", 5.00, 0.50, 30.00,
                 long=LongTier(272_000, 10.00, 1.00, 45.00)),
    # GPT-5.2 not in current GitHub Copilot pricing docs (legacy/deprecated);
    # proxy: GPT-5.4-mini rate (mid-tier model, similar generation).
    ModelPricing("gpt-5.2", "openai", 0.75, 0.075, 4.50),
    # GPT-4.1 not in current GitHub Copilot pricing docs (legacy/deprecated);
    # proxy: OpenAI public API rate as of April 2026.
    ModelPricing("gpt-4.1", "openai", 2.00, 0.20, 8.00),
    # GPT-4o not in current GitHub Copilot pricing docs (legacy/deprecated);
    # legacy multiplier = 0.33x (lightweight tier, same as Haiku/Gemini Flash).
    # proxy: OpenAI public API rate as of April 2026.
    ModelPricing("gpt-4o", "openai", 2.50, 0.25, 10.00),
    # ── Anthropic (note: cwrite is charged) ────────────────────────────────
    ModelPricing("claude-haiku-4.5", "anthropic", 1.00, 0.10, 5.00, cwrite=1.25,
                 aliases=("claude haiku 4.5", "haiku 4.5")),
    ModelPricing("claude-sonnet-4", "anthropic", 3.00, 0.30, 15.00, cwrite=3.75),
    ModelPricing("claude-sonnet-4.5", "anthropic", 3.00, 0.30, 15.00, cwrite=3.75),
    ModelPricing("claude-sonnet-4.6", "anthropic", 3.00, 0.30, 15.00, cwrite=3.75,
                 aliases=("claude sonnet 4.6", "sonnet 4.6")),
    ModelPricing("claude-opus-4.5", "anthropic", 5.00, 0.50, 25.00, cwrite=6.25),
    ModelPricing("claude-opus-4.6", "anthropic", 5.00, 0.50, 25.00, cwrite=6.25),
    ModelPricing("claude-opus-4.7", "anthropic", 5.00, 0.50, 25.00, cwrite=6.25),
    ModelPricing("claude-opus-4.8", "anthropic", 5.00, 0.50, 25.00, cwrite=6.25,
                 aliases=("claude opus 4.8", "opus 4.8")),
    ModelPricing("claude-fable-5", "anthropic", 10.00, 1.00, 50.00, cwrite=12.50),
    # ── Google ──────────────────────────────────────────────────────────────
    ModelPricing("gemini-2.5-pro", "google", 1.25, 0.125, 10.00),
    ModelPricing("gemini-3-flash", "google", 0.50, 0.05, 3.00),
    ModelPricing("gemini-3-flash-preview", "google", 0.50, 0.05, 3.00,
                 aliases=("gemini 3 flash (preview)",)),
    ModelPricing("gemini-3.1-pro", "google", 2.00, 0.20, 12.00,
                 long=LongTier(200_000, 4.00, 0.40, 18.00)),
    ModelPricing("gemini-3.5-flash", "google", 1.50, 0.15, 9.00),
    # ── GitHub / Microsoft ──────────────────────────────────────────────────
    ModelPricing("raptor-mini", "github", 0.25, 0.025, 2.00),
    ModelPricing("mai-code-1-flash", "microsoft", 0.75, 0.075, 4.50),
    # ── xAI ─────────────────────────────────────────────────────────────────
    # Grok Code Fast 1 not in current GitHub Copilot pricing docs;
    # proxy: GPT-5.4 rate (mid-tier model).
    ModelPricing("grok-code-fast-1", "xai", 2.50, 0.25, 15.00,
                 aliases=("grok code fast 1",)),
    # ── Special models (auto-selected, underlying model not disclosed) ────────
    # Code Review: GitHub docs state "model is selected automatically and not
    # disclosed". Proxy: Claude Sonnet 4.6 rate (most common code-review model).
    ModelPricing("code-review-model", "github", 3.00, 0.30, 15.00,
                 aliases=("code review model",)),
    # Coding Agent: auto-selected for agentic tasks; proxy: Gemini 3.5 Flash rate.
    ModelPricing("coding-agent-model", "github", 1.50, 0.15, 9.00,
                 aliases=("coding agent model",)),
]

_BY_ID: dict[str, ModelPricing] = {}
for _m in _MODELS:
    _BY_ID[_m.id] = _m
    for _a in _m.aliases:
        _BY_ID[_a] = _m


def normalize_model(raw: str) -> tuple[str, bool]:
    """Return (canonical_key, is_auto_selected).

    Handles the AI Usage Report 'Auto:' prefix (e.g. 'Auto: Claude Sonnet 4.6'),
    case, spaces, and provider path prefixes (e.g. 'anthropic/claude-sonnet-4.6').
    The returned key is the lowercased, simplified string used for lookup.
    """
    s = (raw or "").strip()
    is_auto = False
    if s.lower().startswith("auto:"):
        is_auto = True
        s = s.split(":", 1)[1].strip()
    if "/" in s:
        s = s.split("/")[-1].strip()
    return s.lower(), is_auto


def get_pricing(raw: str) -> ModelPricing | None:
    """Resolve a (possibly messy) model string to a ModelPricing, or None."""
    key, _ = normalize_model(raw)
    if not key:
        return None
    if key in _BY_ID:
        return _BY_ID[key]
    # space/hyphen normalisation
    alt = key.replace(" ", "-")
    if alt in _BY_ID:
        return _BY_ID[alt]
    alt2 = key.replace("-", " ")
    if alt2 in _BY_ID:
        return _BY_ID[alt2]
    # longest-prefix match (handles version/deployment suffixes)
    for mid in sorted(_BY_ID, key=len, reverse=True):
        if key == mid or key.startswith(mid + "-") or key.startswith(mid + " "):
            return _BY_ID[mid]
    return None


def all_models() -> list[ModelPricing]:
    return list(_MODELS)
