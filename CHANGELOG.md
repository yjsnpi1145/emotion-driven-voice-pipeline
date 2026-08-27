# Changelog

All notable user-facing changes are documented here. The project follows semantic versioning once
the first public tag is created.

## [Unreleased]

### Added

- Open-source release documentation, repository policy checks and Windows CI.
- Portable setup scripts with explicit model-license acceptance.
- Persistent multi-role Director Mode with narrator detection, speaker review, drag/drop assignment,
  translation review and reusable managed role voice presets.
- Source-preserving script chunk analysis with bounded parallel OpenAI-compatible LLM requests and
  retryable restart recovery.
- Fault-isolated two-phase multi-role synthesis: all IndexTTS2 references first, then GPT-SoVITS
  jobs grouped by model profile, followed by source-ordered composition.
- Director generation resume/recompose controls, path-free progress APIs and operational health
  counters.
- Director script analysis now classifies deterministic local slice IDs instead of asking the LLM
  to reproduce source text and calculate Python character offsets.

## [0.1.0] - 2026-08-10

### Added

- OpenAI-compatible multilingual chapter planning and translation.
- IndexTTS2 emotional reference generation and GPT-SoVITS target-language synthesis.
- Persistent SQLite jobs, immutable artifact versions, retention and restart recovery.
- Self-trained GPT-SoVITS model import and activation.
- Local dark WebUI with segment editing, local regeneration, history, recomposition and LLM activity.
