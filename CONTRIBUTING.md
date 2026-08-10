# Contributing

## Development environment

The supported development environment is Windows 10/11, PowerShell 7 and Python 3.11.

```powershell
uv sync --frozen --extra dev --python 3.11
```

Use fake mode for normal development. Contributions must not require real model downloads,
private voices, an LLM credential or a GPU to run their ordinary tests.

## Before opening a pull request

```powershell
uv run ruff check src tests
uv run mypy src workers
node --check src/voice_pipeline/webui/app.js
uv run pytest -q -m "not gpu and not gpu_residency and not quality_model"
uv build --wheel
```

Add or update tests before changing behavior. Preserve the single GPU consumer, immutable artifact
publication, optimistic version selection and loopback-only boundary.

## Repository hygiene

Never commit model weights, trained voices, reference audio, generated audio, SQLite files,
`.local.yaml`, API keys, runtime logs or environment directories. Tests must use synthetic inputs
and temporary directories. Issue and PR diagnostics must be sanitized according to `PRIVACY.md`.

## Third-party reuse

Prefer maintained upstream packages and thin adapters over copied inference code. Update
`config/open-source-reuse.yaml`, the appropriate lock file and `THIRD_PARTY_NOTICES.md` when adding
or replacing a dependency. Do not label IndexTTS2 as MIT; it has a separate model-use agreement.

