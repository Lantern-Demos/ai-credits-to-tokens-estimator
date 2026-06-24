---
applyTo: "**/*.toml"
---

## Package structure

The package uses setuptools with auto-discovery scoped to the `tools/` subtree:

```toml
[tool.setuptools.packages.find]
where = ["."]
include = ["tools*"]
```

**Any new Python subpackage must live under `tools/`** and include an
`__init__.py`. Packages outside `tools/` will not be installed by
`pip install -e .` and imports will fail at runtime.

## Python version

`requires-python = ">=3.10"` is intentional. Do not lower this floor — the
codebase uses `X | Y` union syntax and other 3.10+ features throughout.

## Editable install

This package is installed in editable mode from the repo root. After any change
to `pyproject.toml` (e.g. adding a dependency or new package), re-run:

```bash
pip install -e .
```

from the repo root with the venv activated.

## Dependencies

The package has **no third-party runtime dependencies** — stdlib only. If a
dependency is added, add it to both `[project] dependencies` in `pyproject.toml`
and `tools/aic_token_estimator/requirements.txt`.
