---
applyTo: "**/*.py"
---

## Imports

Always use the full package path from the repo root:
```python
from tools.aic_token_estimator.catalog import USD_PER_CREDIT, get_pricing
from tools.aic_token_estimator.estimator import estimate_tokens, MIX_PRIORS
```

The package is installed in editable mode (`pip install -e .`), so this works as long as the venv is active.

## Standard library only

This package has **no third-party dependencies** — only the Python standard library. Do not add `pip install` requirements or import packages outside stdlib. If a new dependency is truly necessary, document the reason explicitly in `requirements.txt` and `pyproject.toml`.

## File header

Every module starts with this pattern:
```python
"""
module_name.py — one-line description of purpose.

Longer explanation if needed (optional).
"""

from __future__ import annotations
```

`from __future__ import annotations` is required at the top of every source file.

## Type hints

Use Python 3.10+ union syntax throughout — `requires-python = ">=3.10"` is set in `pyproject.toml`:
```python
# Correct
def foo(x: int | None) -> str | list[str]: ...

# Wrong — do not use
from typing import Optional, Union
def foo(x: Optional[int]) -> Union[str, list]: ...
```

## Data structures

Use `@dataclass(frozen=True)` for all new value/result objects:
```python
from dataclasses import dataclass, field

@dataclass(frozen=True)
class MyResult:
    value: float
    label: str
    tags: tuple[str, ...] = field(default_factory=tuple)
```

## Section dividers

Use Unicode em-dash + hyphens to separate logical sections within a module:
```python
# ── Section Name ────────────────────────────────────────────────────────────
```

## Private helpers

Prefix module-internal functions and variables with `_`:
```python
def _parse_date(s: str) -> date | None: ...   # internal
def _pick(row: dict, names: list[str]) -> str | None: ...   # internal
```

Public API functions have no prefix.

## Domain constants

- `USD_PER_CREDIT = 0.01` in `catalog.py` is the **only** credit↔dollar conversion. Never hardcode `0.01` elsewhere — always import and use `USD_PER_CREDIT`.
- `AUTO_DISCOUNT = 0.90` in `estimator.py` is the single configurable auto-select discount constant. Do not inline `0.90` or `0.10` in calculations.
