## Summary

- What user-visible or internal behavior changes?
- Why is this the smallest appropriate change?

## Verification

- [ ] Tests were written or updated before behavior changes.
- [ ] `uv run ruff check src tests`
- [ ] `uv run mypy src workers`
- [ ] `node --check src/voice_pipeline/webui/app.js`
- [ ] `uv run pytest -q -m "not gpu and not gpu_residency and not quality_model"`
- [ ] `uv build --wheel`

## Repository hygiene

- [ ] No model weights, audio, database, `.local.yaml`, credentials or private paths are included.
- [ ] New third-party code/model use is recorded in the reuse inventory and notices.
- [ ] Logs and screenshots are sanitized.

